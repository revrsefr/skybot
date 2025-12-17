from __future__ import annotations

import logging
import re
import socket
import time
import threading
import queue
from typing import Any, Iterable, Optional

from ssl import create_default_context, CERT_NONE, CERT_REQUIRED, SSLError


log = logging.getLogger(__name__)


DEFAULT_NAME = "skybot"
DEFAULT_REALNAME = "Python bot - http://github.com/revrsefr/skybot"
DEFAULT_NICKSERV_NAME = "nickserv"
DEFAULT_NICKSERV_COMMAND = "IDENTIFY %s"


def decode(txt: bytes) -> str:
    for codec in ("utf-8", "iso-8859-1", "shift_jis", "cp1252"):
        try:
            return txt.decode(codec)
        except UnicodeDecodeError:
            continue

    return txt.decode("utf-8", "ignore")


def censor(text: str, censored_strings: Optional[Iterable[str]] = None) -> str:
    text = re.sub(r"[\n\r]+", " ", text)

    if not censored_strings:
        return text

    words = [re.escape(s) for s in censored_strings]
    pattern = "(%s)" % "|".join(words)

    text = re.sub(pattern, "[censored]", text)

    return text


class crlf_tcp:

    "Handles tcp connections that consist of utf-8 lines ending with crlf"

    def __init__(self, host: str, port: int, timeout: int = 300):
        self.ibuffer = b""
        self.obuffer = b""
        self.oqueue: "queue.Queue[str]" = queue.Queue()  # lines to be sent out
        self.iqueue: "queue.Queue[Any]" = queue.Queue()  # lines that were received
        self.socket = self.create_socket()
        self.host = host
        self.port = port
        self.timeout = timeout

    def create_socket(self) -> socket.socket:
        # Historical code used `socket.TCP_NODELAY` as the socket type, which
        # happened to be `1` and thus looked like `SOCK_STREAM` by accident.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def run(self) -> None:
        while True:
            try:
                self.socket.connect((self.host, self.port))
            except (socket.timeout, OSError):
                log.warning("timed out connecting to %s:%s", self.host, self.port)
                time.sleep(60)
            else:
                break
        threading.Thread(target=self.recv_loop, daemon=True).start()
        threading.Thread(target=self.send_loop, daemon=True).start()

    def recv_from_socket(self, nbytes: int) -> bytes:
        return self.socket.recv(nbytes)

    def get_timeout_exception_type(self):
        return socket.timeout

    def handle_receive_exception(self, error: BaseException, last_timestamp: float) -> bool:
        if time.time() - last_timestamp > self.timeout:
            self.iqueue.put(StopIteration)
            self.socket.close()
            return True
        return False

    def recv_loop(self) -> None:
        last_timestamp = time.time()
        while True:
            try:
                data = self.recv_from_socket(4096)
                self.ibuffer += data
                if data:
                    last_timestamp = time.time()
                else:
                    if time.time() - last_timestamp > self.timeout:
                        self.iqueue.put(StopIteration)
                        self.socket.close()
                        return
                    time.sleep(1)
            except (self.get_timeout_exception_type(), socket.error) as e:
                if self.handle_receive_exception(e, last_timestamp):
                    return
                continue

            while b"\r\n" in self.ibuffer:
                line, self.ibuffer = self.ibuffer.split(b"\r\n", 1)
                self.iqueue.put(decode(line))

    def send_loop(self) -> None:
        while True:
            line = self.oqueue.get().splitlines()[0][:500]
            log.debug(">>> %s", line)
            self.obuffer += line.encode("utf-8", "replace") + b"\r\n"
            while self.obuffer:
                sent = self.socket.send(self.obuffer)
                self.obuffer = self.obuffer[sent:]


class crlf_ssl_tcp(crlf_tcp):

    "Handles ssl tcp connections that consist of utf-8 lines ending with crlf"

    def __init__(self, host: str, port: int, ignore_cert_errors: bool, timeout: int = 300):
        self.ignore_cert_errors = ignore_cert_errors
        self.host = host
        crlf_tcp.__init__(self, host, port, timeout)

    def create_socket(self):
        ctx = create_default_context()
        # Python refuses CERT_NONE if hostname checking is enabled.
        ctx.check_hostname = not self.ignore_cert_errors
        ctx.verify_mode = CERT_NONE if self.ignore_cert_errors else CERT_REQUIRED
        return ctx.wrap_socket(
            crlf_tcp.create_socket(self),
            server_side=False,
            server_hostname=self.host,
        )

    def recv_from_socket(self, nbytes: int) -> bytes:
        return self.socket.read(nbytes)

    def get_timeout_exception_type(self):
        return SSLError

    def handle_receive_exception(self, error, last_timestamp):
        return crlf_tcp.handle_receive_exception(self, error, last_timestamp)


def zip_channels(channels: list[str]) -> list[str]:
    channels.sort(key=lambda x: " " not in x)  # keyed channels first
    chans = []
    keys = []
    for channel in channels:
        if " " in channel:
            chan, key = channel.split(" ")
            chans.append(chan)
            keys.append(key)
        else:
            chans.append(channel)
    chans = ",".join(chans)
    if keys:
        return [chans, ",".join(keys)]
    else:
        return [chans]


def test_zip_channels():
    assert zip_channels(["#a", "#b c", "#d"]) == ["#b,#a,#d", "c"]
    assert zip_channels(["#a", "#b"]) == ["#a,#b"]


class IRC:
    IRC_PREFIX_REM = re.compile(r"(.*?) (.*?) (.*)").match
    IRC_NOPROFEIX_REM = re.compile(r"()(.*?) (.*)").match
    IRC_NETMASK_REM = re.compile(r":?([^!@]*)!?([^@]*)@?(.*)").match
    IRC_PARAM_REF = re.compile(r"(?:^|(?<= ))(:.*|[^ ]+)").findall

    "handles the IRC protocol"
    # see the docs/ folder for more information on the protocol

    def __init__(self, conf: dict[str, Any]):
        self.conn = None

        self.nick = DEFAULT_NAME
        self.user = DEFAULT_NAME
        self.realname = DEFAULT_REALNAME
        self.user_mode = None

        self.server_host = None
        self.server_port = 6667
        self.server_password = None

        self.nickserv_password = None
        self.nickserv_name = DEFAULT_NICKSERV_NAME
        self.nickserv_command = DEFAULT_NICKSERV_COMMAND

        self.channels = []
        self.admins = []
        self.censored_strings = []

        self.out: "queue.Queue[list[Any]]" = queue.Queue()  # responses from the server are placed here
        # format: [rawline, prefix, command, params,
        # nick, user, host, paramlist, msg]

        self.set_conf(conf)

        self.connect()

        threading.Thread(target=self.parse_loop, daemon=True).start()

    def set_conf(self, conf: dict[str, Any]) -> None:
        self.nick = conf.get("nick", DEFAULT_NAME)
        self.user = conf.get("user", DEFAULT_NAME)
        self.realname = conf.get("realname", DEFAULT_REALNAME)
        self.user_mode = conf.get("mode", None)

        self.server_host = conf["server"]
        self.server_port = conf.get("port", 6667)
        self.server_password = conf.get("server_password", None)

        self.nickserv_password = conf.get("nickserv_password", None)
        self.nickserv_name = conf.get("nickserv_name", DEFAULT_NICKSERV_NAME)
        self.nickserv_command = conf.get("nickserv_command", DEFAULT_NICKSERV_COMMAND)

        self.channels = conf.get("channels", [])
        self.admins = conf.get("admins", [])
        self.censored_strings = conf.get("censored_strings", [])

        if self.conn is not None:
            self.join_channels()

    def create_connection(self) -> crlf_tcp:
        return crlf_tcp(self.server_host, self.server_port)

    def connect(self) -> None:
        self.conn = self.create_connection()
        threading.Thread(target=self.conn.run, daemon=True).start()
        if self.server_password:
            # PASS should be sent before NICK/USER on most networks.
            self.cmd("PASS", [self.server_password])
        self.cmd("NICK", [self.nick])
        self.cmd("USER", [self.user, "3", "*", self.realname])

    def parse_loop(self) -> None:
        while True:
            msg = self.conn.iqueue.get()

            if msg == StopIteration:
                self.connect()
                continue

            if msg.startswith(":"):  # has a prefix
                prefix, command, params = self.IRC_PREFIX_REM(msg).groups()
            else:
                prefix, command, params = self.IRC_NOPROFEIX_REM(msg).groups()
            nick, user, host = self.IRC_NETMASK_REM(prefix).groups()
            paramlist = self.IRC_PARAM_REF(params)
            lastparam = ""
            if paramlist:
                if paramlist[-1].startswith(":"):
                    paramlist[-1] = paramlist[-1][1:]
                lastparam = paramlist[-1]
            self.out.put(
                [msg, prefix, command, params, nick, user, host, paramlist, lastparam]
            )

            if command == "PING":
                self.cmd("PONG", paramlist)

    def join(self, channel: str) -> None:
        self.cmd("JOIN", channel.split(" "))  # [chan, password]

    def join_channels(self) -> None:
        if self.channels:
            # TODO: send multiple join commands for large channel lists
            self.cmd("JOIN", zip_channels(self.channels))

    def msg(self, target: str, text: str) -> None:
        self.cmd("PRIVMSG", [target, text])

    def cmd(self, command: str, params: Optional[list[str]] = None) -> None:
        if params:
            params[-1] = ":" + params[-1]

            params = [censor(p, self.censored_strings) for p in params]

            self.send(command + " " + " ".join(params))
        else:
            self.send(command)

    def send(self, line: str) -> None:
        self.conn.oqueue.put(line)


class FakeIRC(IRC):
    def __init__(self, conf: dict[str, Any], fn: str):
        self.set_conf(conf)
        self.out: "queue.Queue[list[Any]]" = queue.Queue()  # responses from the server are placed here

        self.f = open(fn, "rb")

        threading.Thread(target=self.parse_loop, daemon=True).start()

    def parse_loop(self):
        while True:
            msg = decode(self.f.readline()[9:])

            if msg == "":
                print("!!!!DONE READING FILE!!!!")
                return

            if msg.startswith(":"):  # has a prefix
                prefix, command, params = self.IRC_PREFIX_REM(msg).groups()
            else:
                prefix, command, params = self.IRC_NOPROFEIX_REM(msg).groups()
            nick, user, host = self.IRC_NETMASK_REM(prefix).groups()
            paramlist = self.IRC_PARAM_REF(params)
            lastparam = ""
            if paramlist:
                if paramlist[-1].startswith(":"):
                    paramlist[-1] = paramlist[-1][1:]
                lastparam = paramlist[-1]
            self.out.put(
                [msg, prefix, command, params, nick, user, host, paramlist, lastparam]
            )
            if command == "PING":
                self.cmd("PONG", [params])

    def cmd(self, command, params=None):
        pass


class SSLIRC(IRC):
    def __init__(self, conf):
        super().__init__(conf=conf)

        self.server_port = 6697
        self.server_ignore_cert = False

    def set_conf(self, conf):
        super().set_conf(conf)

        self.server_port = conf.get("port", 6697)
        self.server_ignore_cert = conf.get("ignore_cert", False)

    def create_connection(self):
        return crlf_ssl_tcp(self.server_host, self.server_port, self.server_ignore_cert)
