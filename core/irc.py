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


def _unescape_tag_value(value: str) -> str:
    # IRCv3 message-tag escaping: \\: backslash, \: semicolon, \s space, \r CR, \n LF
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue

        i += 1
        if i >= len(value):
            break

        esc = value[i]
        if esc == ":":
            out.append(";")
        elif esc == "s":
            out.append(" ")
        elif esc == "r":
            out.append("\r")
        elif esc == "n":
            out.append("\n")
        elif esc == "\\":
            out.append("\\")
        else:
            # Unknown escape: keep the character (drop the backslash).
            out.append(esc)
        i += 1
    return "".join(out)


def _escape_tag_value(value: str) -> str:
    # IRCv3 message-tag escaping: backslash, semicolon, space, CR, LF
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\:")
        .replace(" ", "\\s")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def format_message_tags(tags: dict[str, Optional[str]]) -> str:
    """Format IRCv3 message-tags prefix (without trailing space).

    Invalid keys are skipped.
    """
    if not tags:
        return ""

    parts: list[str] = []
    for key, value in tags.items():
        if not key or any(ch in key for ch in (" ", ";", "=")):
            continue
        if value is None:
            parts.append(key)
        else:
            parts.append(f"{key}={_escape_tag_value(value)}")
    return "@" + ";".join(parts) if parts else ""


def parse_message_tags(tag_blob: str) -> dict[str, Optional[str]]:
    """Parse IRCv3 message-tags (the part after '@' up to the first space).

    Returns a dict mapping tag keys to values (or None if no '=value' was present).
    """
    tags: dict[str, Optional[str]] = {}
    if not tag_blob:
        return tags

    for item in tag_blob.split(";"):
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
            tags[key] = _unescape_tag_value(value)
        else:
            tags[item] = None
    return tags


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
        self.enable_bot_mode = True

        self.server_host = None
        self.server_port = 6667
        self.server_password = None

        self.nickserv_password = None
        self.nickserv_name = DEFAULT_NICKSERV_NAME
        self.nickserv_command = DEFAULT_NICKSERV_COMMAND

        self.channels = []
        self.admins = []
        self.censored_strings = []

        # ISUPPORT (005) feature tokens.
        # Keys map to either None (flag token) or a string value (KEY=VALUE).
        self.isupport: dict[str, Optional[str]] = {}

        # IRCv3 capability negotiation (minimal): request message-tags by default.
        self.requested_caps: set[str] = set()
        self.enabled_caps: set[str] = set()
        self._cap_pending: set[str] = set()
        self._cap_available: set[str] = set()
        self._cap_end_sent = False

        self.out: "queue.Queue[list[Any]]" = queue.Queue()  # responses from the server are placed here
        # format: [rawline, prefix, command, params,
        # nick, user, host, paramlist, msg, tags]

        self.set_conf(conf)

        self.connect()

        threading.Thread(target=self.parse_loop, daemon=True).start()

    def supports_isupport(self, token: str) -> bool:
        return token in self.isupport

    def supports_whox(self) -> bool:
        # WHOX is not negotiated via CAP; servers advertise it in RPL_ISUPPORT.
        return self.supports_isupport("WHOX")

    def supports_monitor(self) -> bool:
        # MONITOR is not negotiated via CAP; servers advertise it in RPL_ISUPPORT.
        return self.supports_isupport("MONITOR")

    def supports_utf8only(self) -> bool:
        # UTF8ONLY is not negotiated via CAP; servers advertise it in RPL_ISUPPORT.
        return self.supports_isupport("UTF8ONLY")

    def supports_bot_mode(self) -> bool:
        # bot-mode is advertised via ISUPPORT (e.g. BOT=B) and implemented as a user mode.
        return self.supports_isupport("BOT")

    def bot_mode_letter(self) -> Optional[str]:
        """Return the bot user mode letter (e.g. 'B') if available."""
        if not self.supports_bot_mode():
            return None

        value = self.isupport.get("BOT")
        if value is None:
            return "B"

        value = value.strip()
        if not value:
            return "B"

        # Some servers may advertise multiple letters; pick the first.
        return value[0]

    def set_conf(self, conf: dict[str, Any]) -> None:
        self.nick = conf.get("nick", DEFAULT_NAME)
        self.user = conf.get("user", DEFAULT_NAME)
        self.realname = conf.get("realname", DEFAULT_REALNAME)
        self.user_mode = conf.get("mode", None)
        self.enable_bot_mode = conf.get("bot_mode", True)

        self.server_host = conf["server"]
        self.server_port = conf.get("port", 6667)
        self.server_password = conf.get("server_password", None)

        self.nickserv_password = conf.get("nickserv_password", None)
        self.nickserv_name = conf.get("nickserv_name", DEFAULT_NICKSERV_NAME)
        self.nickserv_command = conf.get("nickserv_command", DEFAULT_NICKSERV_COMMAND)

        self.channels = conf.get("channels", [])
        self.admins = conf.get("admins", [])
        self.censored_strings = conf.get("censored_strings", [])

        # CHATHISTORY suppression (server-driven history on join)
        # - If allow_chathistory_channels is non-empty: only those targets are allowed.
        # - Else if ignore_chathistory_channels is non-empty: only those targets are suppressed.
        # - Else: ignore_chathistory_batches boolean controls global suppression.
        self.ignore_chathistory_batches = bool(conf.get("ignore_chathistory_batches", True))
        self.ignore_chathistory_channels = [
            c.lower() for c in (conf.get("ignore_chathistory_channels", []) or [])
        ]
        self.allow_chathistory_channels = [
            c.lower() for c in (conf.get("allow_chathistory_channels", []) or [])
        ]

        ircv3 = conf.get("ircv3", {}) or {}
        caps = ircv3.get("caps")
        if caps is None:
            caps = conf.get("caps")
        if caps is None:
            # Safe defaults: all are optional and will only be requested if the
            # server advertises them in CAP LS.
            caps = [
                "message-tags",
                "batch",
                "cap-notify",
                "labeled-response",
                "away-notify",
                "server-time",
                "echo-message",
                "setname",
                "account-tag",
                "account-notify",
                "chghost",
                "extended-join",
                "invite-notify",
                "inspircd.org/stats-tags",
                "multi-prefix",
                "userhost-in-names",
                "standard-replies",
                "extended-monitor",
                # Some networks use draft names for message IDs.
                "msgid",
                "draft/msgid",
            ]
        self.requested_caps = set(caps)

        if self.conn is not None:
            self.join_channels()

    def create_connection(self) -> crlf_tcp:
        return crlf_tcp(self.server_host, self.server_port)

    def connect(self) -> None:
        self.conn = self.create_connection()
        threading.Thread(target=self.conn.run, daemon=True).start()

        # CAP negotiation should start before registration (NICK/USER).
        self._cap_end_sent = False
        self._cap_pending = set()
        self._cap_available = set()
        if self.requested_caps:
            self.cmd("CAP", ["LS", "302"])

        if self.server_password:
            # PASS should be sent before NICK/USER on most networks.
            self.cmd("PASS", [self.server_password])
        self.cmd("NICK", [self.nick])
        self.cmd("USER", [self.user, "3", "*", self.realname])

    def _handle_cap(self, paramlist: list[str]) -> None:
        if not self.requested_caps or len(paramlist) < 2:
            return

        subcmd = paramlist[1].upper()
        caps_blob = paramlist[-1] if paramlist else ""
        caps = set(caps_blob.split()) if caps_blob else set()

        # CAP LS can be multi-line with '*' continuation.
        has_more = "*" in paramlist[2:-1]

        if subcmd == "LS":
            self._cap_available |= caps
            if has_more:
                return

            to_request = sorted(self.requested_caps & self._cap_available)
            if to_request:
                self._cap_pending = set(to_request)
                self.cmd("CAP", ["REQ", " ".join(to_request)])
                return

            if not self._cap_end_sent:
                self.cmd("CAP", ["END"])
                self._cap_end_sent = True
            return

        if subcmd in {"ACK", "NAK"}:
            if subcmd == "ACK":
                self.enabled_caps |= caps
            self._cap_pending -= caps
            if not self._cap_pending and not self._cap_end_sent:
                self.cmd("CAP", ["END"])
                self._cap_end_sent = True
            return

    def parse_loop(self) -> None:
        while True:
            msg = self.conn.iqueue.get()

            if msg == StopIteration:
                self.connect()
                continue

            rawline = msg
            tags: dict[str, Optional[str]] = {}
            if msg.startswith("@"):
                tag_part, msg = msg.split(" ", 1)
                tags = parse_message_tags(tag_part[1:])

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
                [rawline, prefix, command, params, nick, user, host, paramlist, lastparam, tags]
            )

            if command == "005":
                # RPL_ISUPPORT: <nick> <tokens...> :are supported by this server
                # Tokens can be "KEY" or "KEY=VALUE".
                for item in paramlist[1:-1]:
                    if not item or item == ":":
                        continue
                    if "=" in item:
                        key, value = item.split("=", 1)
                        self.isupport[key] = value
                    else:
                        self.isupport[item] = None

            if command == "CAP":
                self._handle_cap(paramlist)

            if command == "PING":
                self.cmd("PONG", paramlist)

    def who(self, mask: str, fields: Optional[str] = None, token: Optional[int] = None) -> None:
        """Send WHO (and WHOX if supported).

        If the server advertises WHOX via ISUPPORT (005) and `fields` is provided,
        this will send an extended WHO in the form:

            WHO <mask> %<fields>[,<token>]

        If `token` is provided, the `t` field is automatically included so the
        server returns the token in 354 replies.
        """
        if not fields or not self.supports_whox():
            self.cmd("WHO", [mask])
            return

        requested_fields = fields
        suffix = ""
        if token is not None:
            # WHOX tokens must be 1-3 digits.
            if token < 0 or token > 999:
                raise ValueError("WHOX token must be between 0 and 999")
            if "t" not in requested_fields:
                requested_fields += "t"
            suffix = "," + str(token)

        self.cmd("WHO", [mask, f"%{requested_fields}{suffix}"])

    # --- MONITOR (online/offline notifications) ---

    def monitor_add(self, targets: list[str]) -> None:
        """Add nicks to the server-side monitor list (if supported)."""
        if not targets:
            return
        self.cmd("MONITOR", ["+", ",".join(targets)])

    def monitor_remove(self, targets: list[str]) -> None:
        """Remove nicks from the server-side monitor list (if supported)."""
        if not targets:
            return
        self.cmd("MONITOR", ["-", ",".join(targets)])

    def monitor_clear(self) -> None:
        """Clear the server-side monitor list (if supported)."""
        self.cmd("MONITOR", ["C"])

    def monitor_list(self) -> None:
        """Request the current monitor list (replies 732/733)."""
        self.cmd("MONITOR", ["L"])

    def monitor_status(self) -> None:
        """Request online/offline status for the whole list (replies 730/731)."""
        self.cmd("MONITOR", ["S"])

    def join(self, channel: str) -> None:
        self.cmd("JOIN", channel.split(" "))  # [chan, password]

    def join_channels(self) -> None:
        if self.channels:
            # TODO: send multiple join commands for large channel lists
            self.cmd("JOIN", zip_channels(self.channels))

    def msg(self, target: str, text: str) -> None:
        self.cmd("PRIVMSG", [target, text])

    def setname(self, realname: str) -> None:
        """Change this connection's realname (IRCv3 setname).

        Servers SHOULD accept SETNAME even if the capability is not negotiated,
        but if setname *is* negotiated the change is only confirmed when the
        server sends back a SETNAME message.
        """
        self.cmd("SETNAME", [realname])

    # --- IRCv3 chat history helpers ---
    # These simply send CHATHISTORY requests. Servers reply using BATCH and
    # message-tags (`@batch=`) when the negotiated capability is enabled.
    # Plugins can hook on BATCH events and/or filter by `input.tags.get("batch")`.

    def chathistory_latest(self, target: str, limit: int = 50) -> None:
        self.cmd("CHATHISTORY", ["LATEST", target, "*", str(limit)])

    def chathistory_before(self, target: str, reference: str, limit: int = 50) -> None:
        self.cmd("CHATHISTORY", ["BEFORE", target, reference, str(limit)])

    def chathistory_after(self, target: str, reference: str, limit: int = 50) -> None:
        self.cmd("CHATHISTORY", ["AFTER", target, reference, str(limit)])

    def cmd(self, command: str, params: Optional[list[str]] = None) -> None:
        self.cmdv3(command, params=params, tags=None)

    def cmdv3(
        self,
        command: str,
        params: Optional[list[str]] = None,
        tags: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        if params:
            params[-1] = ":" + params[-1]

            params = [censor(p, self.censored_strings) for p in params]

            line = command + " " + " ".join(params)
        else:
            line = command

        tag_prefix = format_message_tags(tags or {})
        if tag_prefix:
            line = tag_prefix + " " + line

        self.send(line)

    def cmd_labeled(self, command: str, params: Optional[list[str]] = None) -> str:
        """Send a command tagged with @label=... for labeled-response.

        Returns the label so callers can correlate responses.
        """
        # Keep labels simple and ASCII.
        now_ms = int(time.time() * 1000)
        label = f"skybot-{now_ms:x}"  # hex timestamp
        self.cmdv3(command, params=params, tags={"label": label})
        return label

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

            rawline = msg
            tags: dict[str, Optional[str]] = {}
            if msg.startswith("@"):
                tag_part, msg = msg.split(" ", 1)
                tags = parse_message_tags(tag_part[1:])

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
                [rawline, prefix, command, params, nick, user, host, paramlist, lastparam, tags]
            )
            if command == "PING":
                self.cmd("PONG", [params])

            if command == "CAP":
                self._handle_cap(paramlist)

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
