# crowdcontrol.py by craisins in 2014
# Bot must have some sort of op or admin privileges to be useful

import re
import time
from collections import OrderedDict
from util import hook
from util.badness import badness
from util import crowdcontrol_db


# --- WHOX user info helper (command: .user info <nick>) ---
# Uses per-connection in-memory pending state; best-effort and safe to ignore
# if the server doesn't support WHOX.


_USERINFO_DEFAULT_FIELDS = "tcuhsnfar"  # token, chan, user, host, server, nick, flags, account, realname
_USERINFO_PENDING_TTL = 30.0


def _userinfo_get_state(conn):
    if conn is None:
        return None
    if not hasattr(conn, "_crowdcontrol_userinfo"):
        # token -> dict(reply_to, requester, target, created_m, lines)
        conn._crowdcontrol_userinfo = {"next_token": 1, "pending": {}}
    return conn._crowdcontrol_userinfo


def _userinfo_next_token(conn):
    st = _userinfo_get_state(conn)
    if st is None:
        return None

    pending = st["pending"]
    for _ in range(1000):
        token = int(st.get("next_token", 1)) % 1000
        st["next_token"] = (token + 1) % 1000
        if token not in pending:
            return token
    return None


def _userinfo_cleanup(conn, *, now_m=None):
    st = _userinfo_get_state(conn)
    if st is None:
        return
    if now_m is None:
        now_m = time.monotonic()
    pending = st.get("pending", {})
    expired = [
        tok
        for tok, req in pending.items()
        if (now_m - float(req.get("created_m", now_m))) > _USERINFO_PENDING_TTL
    ]
    for tok in expired:
        pending.pop(tok, None)


def _userinfo_send_reply(conn, reply_to: str, requester: str, text: str) -> None:
    if conn is None or not reply_to:
        return
    try:
        if requester and reply_to.lower() != requester.lower():
            conn.msg(reply_to, f"{requester}: {text}")
        else:
            conn.msg(reply_to, text)
    except Exception:
        return


def _userinfo_format_line(line: dict) -> str:
    nick = (line.get("nick") or "?").strip()
    user = (line.get("user") or "?").strip()
    host = (line.get("host") or "?").strip()
    server = (line.get("server") or "?").strip()
    flags = (line.get("flags") or "").strip()
    account = (line.get("account") or "").strip()
    realname = (line.get("realname") or "").strip()
    chan = (line.get("chan") or "").strip()

    parts = [f"{nick} ({user}@{host})", f"server={server}"]
    if account and account not in ("0", "*", "-"):
        parts.append(f"account={account}")
    if flags:
        parts.append(f"flags={flags}")
    if chan and chan not in ("0", "*", "-"):
        parts.append(f"chan={chan}")
    if realname:
        parts.append(f"realname={realname}")
    return " | ".join(parts)

# Use "crowdcontrol" array in config
# syntax
# rule:
#   re: RegEx. regular expression to match
#   msg: String. message to display either with kick or as a warning
#   kick: Integer. 1 for True, 0 for False on if to kick user
#   ban_length: Integer. (optional) Length of time (seconds) to ban user. (-1 to never unban, 0 to not ban, > 1 for time)


# In-memory flood tracking (per-process, resets on restart).
# Uses a bounded token-bucket per (channel, identity) to keep memory stable.
# Keyed by (channel, identity) -> (tokens: float, last_ts: float, last_seen: float)
_FLOOD_STATE = OrderedDict()
_FLOOD_LAST_CLEANUP = 0.0

# Flood strike tracking for escalation (kick first, ban on repeat).
# Keyed by (server, channel, identity) -> (strikes: int, last_ts: float, last_seen: float)
_FLOOD_STRIKES = OrderedDict()
_FLOOD_STRIKES_LAST_CLEANUP = 0.0


# DB-backed scheduled unbans (survives restarts, avoids time.sleep()).
_UNBAN_LAST_POLL = 0.0
_FLOOD_DB_CLEANUP_STATE = {"t": 0.0}
_FLOOD_DB_COMMIT_STATE = {"t": 0.0}
_FLOOD_STRIKES_DB_CLEANUP_STATE = {"t": 0.0}


@hook.regex(r".*")
def crowdcontrol(
    inp,
    kick=None,
    ban=None,
    unban=None,
    reply=None,
    bot=None,
    db=None,
    conn=None,
    chan="",
    nick="",
    user="",
    host="",
    server="",
    admin=False,
):
    msg_text = inp.group(0)

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + str(key) + "}"

    def _format_reason(template, *, extra=None):
        if template is None:
            return None
        s = str(template)
        # Support Ruby-style interpolation in configs: "#{channel}".
        s = s.replace("#{channel}", "{channel}")
        data = {
            "channel": chan,
            "chan": chan,
            "nick": nick,
            "user": user,
            "host": host,
            "server": server,
            "message": msg_text,
        }
        if isinstance(extra, dict):
            data.update(extra)
        try:
            return s.format_map(_SafeDict(data))
        except Exception:
            return s

    def _flood_key():
        # Prefer a stable identity; fall back to nick if needed.
        if user or host:
            return f"{nick}!{user}@{host}".strip("!@")
        return nick

    def _ban_mask():
        # core/main.py defaults to using only `host`, but most IRCds set bans as masks
        # (e.g. *!*@host). If we ban with a mask, we must unban with the same mask.
        if host:
            return f"*!*@{host}"
        if user:
            return f"*!{user}@*"
        return nick

    def _flood_cleanup(*, now, idle_ttl, max_keys):
        global _FLOOD_LAST_CLEANUP

        # Periodic cleanup (bounded work): drop least-recently-seen entries.
        # OrderedDict is maintained as LRU by _flood_match.
        if now - _FLOOD_LAST_CLEANUP < 60.0:
            return
        _FLOOD_LAST_CLEANUP = now

        cutoff = now - float(idle_ttl)
        while _FLOOD_STATE:
            _, state = next(iter(_FLOOD_STATE.items()))
            _tokens, _last_ts, last_seen = state
            if last_seen >= cutoff and len(_FLOOD_STATE) <= max_keys:
                break
            _FLOOD_STATE.popitem(last=False)

    def _flood_strikes_cleanup(*, now, idle_ttl, max_keys):
        global _FLOOD_STRIKES_LAST_CLEANUP

        if now - _FLOOD_STRIKES_LAST_CLEANUP < 60.0:
            return
        _FLOOD_STRIKES_LAST_CLEANUP = now

        cutoff = now - float(idle_ttl)
        while _FLOOD_STRIKES:
            _, state = next(iter(_FLOOD_STRIKES.items()))
            _strikes, _last_ts, last_seen = state
            if last_seen >= cutoff and len(_FLOOD_STRIKES) <= max_keys:
                break
            _FLOOD_STRIKES.popitem(last=False)

    def _flood_match(rule):
        flood = rule.get("flood")
        if flood is None:
            return False, None

        # Only apply flood control to channel traffic.
        if not chan:
            return False, None

        # Accept either a dict (preferred) or a truthy value + top-level keys.
        if isinstance(flood, dict):
            raw_count = flood.get("count", flood.get("lines"))
            raw_seconds = flood.get("seconds", flood.get("window"))
        else:
            raw_count = rule.get("flood_count")
            raw_seconds = rule.get("flood_seconds")

        try:
            count = int(raw_count)
            seconds = float(raw_seconds)
        except Exception:
            return False, None
        if count <= 0 or seconds <= 0:
            return False, None

        # Backend selection: default to in-process memory.
        backend = "mem"
        if isinstance(flood, dict):
            backend = str(flood.get("backend", "mem")).strip().lower() or "mem"

        # Token bucket: allow a burst of `count` messages, refilling at count/seconds.
        burst = count
        rate = float(count) / float(seconds)

        ident = _flood_key()
        now_m = time.monotonic()

        # Safety valves for large networks.
        if isinstance(flood, dict):
            idle_ttl = float(flood.get("idle_ttl", max(300.0, seconds * 10.0)))
        else:
            idle_ttl = max(300.0, seconds * 10.0)

        if backend in ("db", "postgres", "postgresql"):
            if db is None:
                return False, None
            matched, extra = crowdcontrol_db.flood_match(
                db,
                server=server,
                chan=chan,
                ident=ident,
                burst=float(burst),
                rate=float(rate),
                idle_ttl=float(idle_ttl),
                last_cleanup_state=_FLOOD_DB_CLEANUP_STATE,
                last_commit_state=_FLOOD_DB_COMMIT_STATE,
            )
            if matched:
                extra.update(
                    _flood_escalation(rule, backend="db", ident=ident)
                )
            return matched, extra

        # In-memory backend (default)
        if isinstance(flood, dict):
            max_keys = int(flood.get("max_keys", 50000))
            idle_ttl = float(idle_ttl)
        else:
            max_keys = 50000

        key = (server, chan, ident)

        # Opportunistic cleanup and LRU bounding.
        _flood_cleanup(now=now_m, idle_ttl=idle_ttl, max_keys=max_keys)

        state = _FLOOD_STATE.get(key)
        if state is None:
            tokens = float(burst)
            last_ts = now_m
        else:
            tokens, last_ts, _last_seen = state
            refill = (now_m - last_ts) * float(rate)
            tokens = min(float(burst), tokens + refill)
            last_ts = now_m

        tokens -= 1.0
        matched = tokens < 0.0

        # Avoid repeated actions on consecutive lines: once it triggers, reset the bucket.
        if matched:
            tokens = float(burst) - 1.0

        # Update LRU ordering and enforce max size.
        _FLOOD_STATE[key] = (tokens, last_ts, now_m)
        _FLOOD_STATE.move_to_end(key, last=True)
        while len(_FLOOD_STATE) > max_keys:
            _FLOOD_STATE.popitem(last=False)

        # Provide placeholders for templates.
        approx_hits = int(max(0.0, float(burst) - tokens))
        extra = {
            "flood_backend": "mem",
            "flood_count": count,
            "flood_seconds": seconds,
            "flood_hits": approx_hits,
            "flood_tokens": round(tokens, 2),
            "flood_max_keys": max_keys,
        }

        if matched:
            extra.update(_flood_escalation(rule, backend="mem", ident=ident, idle_ttl=idle_ttl, max_keys=max_keys))

        return matched, extra

    def _flood_escalation(rule, *, backend: str, ident: str, idle_ttl: float = 600.0, max_keys: int = 50000):
        """Compute strike count + action for a flood trigger.

        Default behavior: kick on first strike, ban on second strike (within window).
        Configure per-rule under flood:

          "flood": {
            "count": 5, "seconds": 8,
            "escalate": {"ban_after": 2, "window": 600, "ban_length": 300}
          }
        """

        flood = rule.get("flood") or {}
        if not isinstance(flood, dict):
            flood = {}

        esc = flood.get("escalate")
        if not isinstance(esc, dict):
            esc = {}

        try:
            ban_after = int(esc.get("ban_after", flood.get("ban_after", 2)))
        except Exception:
            ban_after = 2
        ban_after = max(1, ban_after)

        try:
            window = float(esc.get("window", flood.get("strike_window", 600)))
        except Exception:
            window = 600.0
        window = max(1.0, window)

        # Ban length used once strikes reach ban_after.
        try:
            ban_len = int(esc.get("ban_length", flood.get("ban_length", 300)))
        except Exception:
            ban_len = 300

        # Track strikes only for flood-trigger events.
        now_m = time.monotonic()
        now_ts = time.time()
        key = (server, chan, ident)

        if backend == "db" and db is not None:
            strikes = crowdcontrol_db.flood_strike_bump(
                db,
                server=server,
                chan=chan,
                ident=ident,
                now_ts=float(now_ts),
                window=float(window),
                idle_ttl=float(max(idle_ttl, window * 2)),
                last_cleanup_state=_FLOOD_STRIKES_DB_CLEANUP_STATE,
            )
        else:
            # Memory strikes: LRU bounded similarly to flood state.
            _flood_strikes_cleanup(now=now_m, idle_ttl=max(idle_ttl, window * 2), max_keys=max_keys)
            st = _FLOOD_STRIKES.get(key)
            if st is None:
                strikes = 1
                last_ts = now_ts
            else:
                old_strikes, last_ts, _last_seen = st
                if float(now_ts) - float(last_ts) > float(window):
                    strikes = 1
                else:
                    strikes = int(old_strikes) + 1
                last_ts = now_ts

            _FLOOD_STRIKES[key] = (int(strikes), float(last_ts), now_m)
            _FLOOD_STRIKES.move_to_end(key, last=True)
            while len(_FLOOD_STRIKES) > max_keys:
                _FLOOD_STRIKES.popitem(last=False)

        action = "ban" if strikes >= ban_after else "kick"
        return {
            "flood_strikes": int(strikes),
            "flood_ban_after": int(ban_after),
            "flood_strike_window": round(float(window), 2),
            "flood_action": action,
            "flood_ban_length": int(ban_len),
        }

    for rule in bot.config.get("crowdcontrol", []):
        # A rule matches either by regex (`re`) or by mojibake 'badness' score.
        # Example badness rule:
        #   {"badness": 2, "msg": "mojibake spam", "kick": 1, "ban_length": 60}
        score = None
        threshold = None
        flood_extra = None

        matched, flood_extra = _flood_match(rule)
        if not matched and rule.get("flood") is not None:
            # A flood rule only matches by flood; don't fall through to regex.
            continue

        if not matched:
            rule_badness = rule.get("badness")
            if rule_badness is not None:
                try:
                    score = badness(msg_text)
                    threshold = int(rule_badness)
                    matched = score >= threshold
                except Exception:
                    score = 0
                    threshold = None
                    matched = False
            else:
                rule_re = rule.get("re")
                if not rule_re:
                    continue
                matched = re.search(rule_re, msg_text) is not None

        if matched:
            # Never take moderation actions against configured admins.
            if admin:
                return

            should_kick = rule.get("kick", 0)
            ban_length = rule.get("ban_length", 0)

            # Flood escalation: kick first, ban on repeat.
            if rule.get("flood") is not None and isinstance(flood_extra, dict) and flood_extra.get("flood_action"):
                # Always kick on flood actions unless the rule explicitly disables kicking.
                if should_kick:
                    should_kick = 1

                if flood_extra.get("flood_action") == "ban":
                    try:
                        ban_length = int(flood_extra.get("flood_ban_length") or 0)
                    except Exception:
                        ban_length = 0
                else:
                    # First strike: ensure no ban.
                    ban_length = 0
            reason = _format_reason(
                rule.get("msg"),
                extra={
                    "badness": score,
                    "threshold": threshold,
                    "re": rule.get("re"),
                    **(flood_extra or {}),
                },
            )
            if ban_length != 0:
                ban_target = _ban_mask()
                ban(ban_target)
            if should_kick:
                kick(reason=reason)
            elif "msg" in rule:
                reply(reason)
            if ban_length > 0:
                # Schedule unban via DB so it survives restarts and doesn't block.
                try:
                    if db is not None and server and chan and ban_target:
                        crowdcontrol_db.init_unbans(db)
                        crowdcontrol_db.schedule_unban(
                            db,
                            server=server,
                            chan=chan,
                            mask=ban_target,
                            unban_at=int(time.time() + int(ban_length)),
                        )
                except Exception:
                    # Best-effort fallback: keep old behavior if DB isn't available.
                    try:
                        time.sleep(ban_length)
                        unban(ban_target)
                    except Exception:
                        pass


@hook.event("*")
def crowdcontrol_unban_sweeper(
    inp,
    bot=None,
    db=None,
    conn=None,
    server="",
):
    # Best-effort poller: unban due masks for this connection.
    if bot is None or db is None or conn is None or not server:
        return

    global _UNBAN_LAST_POLL
    cfg = bot.config.get("crowdcontrol_unban", {}) or {}
    try:
        poll_interval = float(cfg.get("poll_interval", 10))
        batch = int(cfg.get("batch", 50))
    except Exception:
        poll_interval = 10.0
        batch = 50

    now_m = time.monotonic()
    if now_m - _UNBAN_LAST_POLL < poll_interval:
        return
    _UNBAN_LAST_POLL = now_m

    try:
        crowdcontrol_db.init_unbans(db)
        due = crowdcontrol_db.due_unbans(db, server=server, now_ts=time.time(), limit=batch)
        for chan, mask in due:
            try:
                conn.cmd("MODE", [str(chan), "-b", str(mask)])
                crowdcontrol_db.delete_unban(db, server=server, chan=str(chan), mask=str(mask))
            except Exception:
                # Keep the row so it can be retried later.
                continue
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@hook.command("user")
def userinfo(inp, conn=None, chan="", nick=""):
    """ .user info <nick> -- fetch WHOX details for a nick """

    text = (inp or "").strip()
    if not text:
        return "usage: .user info <nick>"

    parts = text.split()
    sub = parts[0].lower()
    if sub not in ("info", "who", "whox"):
        return "usage: .user info <nick>"
    if len(parts) < 2:
        return "usage: .user info <nick>"

    target = parts[1].strip()
    if not target:
        return "usage: .user info <nick>"
    if conn is None:
        return "error: no connection"

    # Clean old requests so token allocation stays stable.
    _userinfo_cleanup(conn)

    if not getattr(conn, "supports_whox", lambda: False)():
        return "error: server does not advertise WHOX"

    token = _userinfo_next_token(conn)
    if token is None:
        return "error: too many pending WHOX requests"

    st = _userinfo_get_state(conn)
    st["pending"][int(token)] = {
        "reply_to": chan,
        "requester": nick,
        "target": target,
        "created_m": time.monotonic(),
        "lines": [],
    }

    try:
        # Ask for token first so parsing is deterministic.
        conn.who(target, fields=_USERINFO_DEFAULT_FIELDS, token=int(token))
    except Exception:
        st["pending"].pop(int(token), None)
        return "error: WHOX request failed"


@hook.event("354")
def userinfo_whox_reply(paraml, conn=None):
    # Expected WHOX (fields tcuhsnfar):
    # 354 <me> <token> <chan> <user> <host> <server> <nick> <flags> <account> :<realname>
    if conn is None or not paraml or len(paraml) < 3:
        return

    st = _userinfo_get_state(conn)
    if st is None:
        return
    pending = st.get("pending", {})

    try:
        token_s = str(paraml[1])
        if not token_s.isdigit():
            return
        token = int(token_s)
    except Exception:
        return

    req = pending.get(token)
    if not req:
        return

    # Parse as best-effort; tolerate missing fields.
    rest = list(paraml[2:])
    # The trailing realname is already de-":"'d by core parsing.
    while len(rest) < 8:
        rest.append("")
    chan, user, host, server, nick, flags, account = rest[:7]
    realname = rest[7] if len(rest) >= 8 else ""
    if len(rest) > 8:
        # Some servers may split realname/extra; re-join.
        realname = " ".join([r for r in rest[7:] if r is not None])

    req["lines"].append(
        {
            "token": token,
            "chan": chan,
            "user": user,
            "host": host,
            "server": server,
            "nick": nick,
            "flags": flags,
            "account": account,
            "realname": realname,
        }
    )


@hook.event("315")
def userinfo_endofwho(paraml, conn=None):
    # 315 <me> <mask> :End of WHO list.
    if conn is None or not paraml or len(paraml) < 2:
        return

    st = _userinfo_get_state(conn)
    if st is None:
        return

    now_m = time.monotonic()
    _userinfo_cleanup(conn, now_m=now_m)

    mask = str(paraml[1] or "").strip()
    if not mask:
        return

    pending = st.get("pending", {})
    # Flush any pending requests for this mask (typically just one).
    matches = [
        (tok, req)
        for tok, req in list(pending.items())
        if str(req.get("target", "")).lower() == mask.lower()
    ]
    for tok, req in matches:
        reply_to = req.get("reply_to") or ""
        requester = req.get("requester") or ""
        lines = req.get("lines") or []

        if not lines:
            _userinfo_send_reply(conn, reply_to, requester, f"no WHOX results for {mask}")
        else:
            # Usually one line for a nick; if multiple, show up to a few.
            for line in lines[:3]:
                _userinfo_send_reply(conn, reply_to, requester, _userinfo_format_line(line))
            if len(lines) > 3:
                _userinfo_send_reply(conn, reply_to, requester, f"(+{len(lines) - 3} more)")

        pending.pop(tok, None)
