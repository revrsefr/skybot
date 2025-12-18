import re
import threading
import traceback
from queue import Queue

threading.stack_size(1024 * 512)  # reduce vm size


class Input(dict):
    def __init__(
        self, conn, raw, prefix, command, params, nick, user, host, paraml, msg, tags
    ):

        server = conn.server_host

        chan = paraml[0].lower()
        if chan == conn.nick.lower():  # is a PM
            chan = nick

        def say(msg):
            conn.msg(chan, msg)

        def reply(msg):
            if chan == nick:  # PMs don't need prefixes
                self.say(msg)
            else:
                self.say(nick + ": " + msg)

        def pm(msg, nick=nick):
            conn.msg(nick, msg)

        def set_nick(nick):
            conn.set_nick(nick)

        def me(msg):
            self.say("\x01%s %s\x01" % ("ACTION", msg))

        def notice(msg):
            conn.cmd("NOTICE", [nick, msg])

        def kick(target=None, reason=None):
            conn.cmd("KICK", [chan, target or nick, reason or ""])

        def ban(target=None):
            conn.cmd("MODE", [chan, "+b", target or host])

        def unban(target=None):
            conn.cmd("MODE", [chan, "-b", target or host])

        dict.__init__(
            self,
            conn=conn,
            raw=raw,
            prefix=prefix,
            command=command,
            params=params,
            nick=nick,
            user=user,
            host=host,
            paraml=paraml,
            msg=msg,
            tags=tags,
            server=server,
            chan=chan,
            notice=notice,
            say=say,
            reply=reply,
            pm=pm,
            bot=bot,
            kick=kick,
            ban=ban,
            unban=unban,
            me=me,
            set_nick=set_nick,
            lastparam=paraml[-1],
        )

    # make dict keys accessible as attributes
    def __getattr__(self, key):
        return self[key]

    def __setattr__(self, key, value):
        self[key] = value


def run(func, input):
    args = func._args

    if "inp" not in input:
        input.inp = input.paraml

    if args:
        if "db" in args and "db" not in input:
            input.db = bot.get_db_connection(input.conn)
        if "input" in args:
            input.input = input
        if 0 in args:
            out = func(input.inp, **input)
        else:
            kw = dict((key, input[key]) for key in args if key in input)
            out = func(input.inp, **kw)
    else:
        out = func(input.inp)
    if out is not None:
        input.reply(str(out))


def do_sieve(sieve, bot, input, func, type, args):
    try:
        return sieve(bot, input, func, type, args)
    except Exception:
        print("sieve error", end=" ")
        traceback.print_exc()
        return None


class Handler:
    """Runs plugins in their own threads (ensures order)"""

    def __init__(self, func):
        self.func = func
        self.input_queue = Queue()
        self._stop_sentinel = object()
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()

    def start(self):
        uses_db = "db" in self.func._args
        db_conns = {}
        while True:
            inp = self.input_queue.get()

            if inp is self._stop_sentinel:
                break

            if uses_db:
                db = db_conns.get(inp.conn)
                if db is None:
                    db = bot.get_db_connection(inp.conn)
                    db_conns[inp.conn] = db
                inp.db = db

            try:
                run(self.func, inp)
            except Exception:
                traceback.print_exc()

    def stop(self):
        self.input_queue.put(self._stop_sentinel)

    def put(self, value):
        self.input_queue.put(value)


def dispatch(input, kind, func, args, autohelp=False):
    for (sieve,) in bot.plugs["sieve"]:
        input = do_sieve(sieve, bot, input, func, kind, args)
        if input is None:
            return

    if (
        autohelp
        and args.get("autohelp", True)
        and not input.inp
        and func.__doc__ is not None
    ):
        input.reply(func.__doc__)
        return

    if hasattr(func, "_apikeys"):
        bot_keys = bot.config.get("api_keys", {})
        keys = {key: bot_keys.get(key) for key in func._apikeys}

        missing = [keyname for keyname, value in keys.items() if value is None]
        if missing:
            input.reply("error: missing api keys - {}".format(missing))
            return

        # Return a single key as just the value, and multiple keys as a dict.
        if len(keys) == 1:
            input.api_key = list(keys.values())[0]
        else:
            input.api_key = keys

    if func._thread:
        bot.threads[func].put(input)
    else:
        threading.Thread(target=run, args=(func, input), daemon=True).start()


def match_command(command):
    commands = list(bot.commands)

    # do some fuzzy matching
    prefix = [x for x in commands if x.startswith(command)]
    if len(prefix) == 1:
        return prefix[0]
    elif prefix and command not in prefix:
        return prefix

    return command


def make_command_re(bot_prefix, is_private, bot_nick):
    if isinstance(bot_prefix, list):
        bot_prefix = list(bot_prefix)
    else:
        bot_prefix = [bot_prefix]
    if is_private:
        bot_prefix = bot_prefix + [""]  # empty prefix
    bot_prefix = "|".join(re.escape(p) for p in bot_prefix)
    bot_prefix += "|" + re.escape(bot_nick) + r"[:,]+\s+"
    command_re = r"(?:%s)(\w+)(?:$|\s+)(.*)" % bot_prefix
    return re.compile(command_re)


def test_make_command_re():
    match = make_command_re(".", False, "bot").match
    assert not match("foo")
    assert not match("bot foo")
    for _ in range(2):
        assert match(".test").groups() == ("test", "")
        assert match("bot: foo args").groups() == ("foo", "args")
        match = make_command_re(".", True, "bot").match
    assert match("foo").groups() == ("foo", "")
    match = make_command_re([".", "!"], False, "bot").match
    assert match("!foo args").groups() == ("foo", "args")


def main(conn, out):
    # --- Ignore server-driven CHATHISTORY batches ---
    # Some networks send a CHATHISTORY batch automatically on join.
    # We don't want the bot to react to (or log) history on connect.
    #
    # Behavior is configurable per connection:
    # - allow_chathistory_channels: list of targets allowed to pass through
    # - ignore_chathistory_channels: list of targets to suppress
    # - ignore_chathistory_batches: global boolean (default True)
    try:
        command = out[2]
        paraml = out[7]
        tags = out[9]
        nick = out[4]
    except Exception:
        command = None
        paraml = None
        tags = {}
        nick = ""

    # --- IRCv3 echo-message ---
    # If we negotiated echo-message, the server will send our own PRIVMSG/NOTICE
    # back to us. Treating those as inbound messages causes double-processing
    # (and can trigger command loops if the bot talks with its command prefix).
    if (
        command in {"PRIVMSG", "NOTICE"}
        and nick
        and nick.lower() == conn.nick.lower()
        and "echo-message" in getattr(conn, "enabled_caps", set())
    ):
        return

    def _should_ignore_chathistory_target(target: str) -> bool:
        target_l = (target or "").lower()
        allow = getattr(conn, "allow_chathistory_channels", []) or []
        if allow:
            return target_l not in allow

        ignore = getattr(conn, "ignore_chathistory_channels", []) or []
        if ignore:
            return target_l in ignore

        return bool(getattr(conn, "ignore_chathistory_batches", True))

    if not hasattr(conn, "_ignore_batches"):
        # batch_id -> (batch_type, target)
        conn._ignore_batches = {}

    if command == "BATCH" and paraml:
        ref = paraml[0]
        if ref.startswith("+") and len(paraml) >= 2:
            batch_id = ref[1:]
            batch_type = paraml[1]
            if batch_type == "chathistory":
                target = paraml[2] if len(paraml) >= 3 else ""
                if _should_ignore_chathistory_target(target):
                    conn._ignore_batches[batch_id] = (batch_type, target)
                    return
        elif ref.startswith("-"):
            batch_id = ref[1:]
            batch = conn._ignore_batches.pop(batch_id, None)
            if batch and batch[0] == "chathistory":
                return

    batch_id = None
    if isinstance(tags, dict):
        batch_id = tags.get("batch")
    if batch_id:
        batch = conn._ignore_batches.get(batch_id)
        if batch and batch[0] == "chathistory":
            return

    inp = Input(conn, *out)

    # EVENTS
    for func, args in bot.events[inp.command] + bot.events["*"]:
        dispatch(Input(conn, *out), "event", func, args)

    if inp.command == "PRIVMSG":
        # COMMANDS
        config_prefix = bot.config.get("prefix", ".")
        is_private = inp.chan == inp.nick  # no prefix required
        command_re = make_command_re(config_prefix, is_private, inp.conn.nick)

        m = command_re.match(inp.lastparam)

        if m:
            trigger = m.group(1).lower()
            command = match_command(trigger)

            if isinstance(command, list):  # multiple potential matches
                input = Input(conn, *out)
                input.reply(
                    "did you mean %s or %s?" % (", ".join(command[:-1]), command[-1])
                )
            elif command in bot.commands:
                input = Input(conn, *out)
                input.trigger = trigger
                input.inp_unstripped = m.group(2)
                input.inp = input.inp_unstripped.strip()

                func, args = bot.commands[command]
                dispatch(input, "command", func, args, autohelp=True)

        # REGEXES
        for func, args in bot.plugs["regex"]:
            m = args["re"].search(inp.lastparam)
            if m:
                input = Input(conn, *out)
                input.inp = m

                dispatch(input, "regex", func, args)
