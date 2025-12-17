# crowdcontrol.py by craisins in 2014
# Bot must have some sort of op or admin privileges to be useful

import re
import time
from collections import OrderedDict
from util import hook
from util.badness import badness

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


# DB-backed scheduled unbans (survives restarts, avoids time.sleep()).
_UNBAN_LAST_POLL = 0.0


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

        # Token bucket: allow a burst of `count` messages, refilling at count/seconds.
        burst = count
        rate = float(count) / float(seconds)

        # Safety valves for large networks: cap stored identities and evict idle entries.
        if isinstance(flood, dict):
            max_keys = int(flood.get("max_keys", 50000))
            idle_ttl = float(flood.get("idle_ttl", max(300.0, seconds * 10.0)))
        else:
            max_keys = 50000
            idle_ttl = max(300.0, seconds * 10.0)

        key = (server, chan, _flood_key())
        now = time.monotonic()

        # Opportunistic cleanup and LRU bounding.
        _flood_cleanup(now=now, idle_ttl=idle_ttl, max_keys=max_keys)

        state = _FLOOD_STATE.get(key)
        if state is None:
            tokens = float(burst)
            last_ts = now
        else:
            tokens, last_ts, _last_seen = state
            refill = (now - last_ts) * rate
            tokens = min(float(burst), tokens + refill)
            last_ts = now

        tokens -= 1.0
        matched = tokens < 0.0

        # Avoid repeated actions on consecutive lines: once it triggers, reset the bucket.
        if matched:
            tokens = float(burst) - 1.0

        # Update LRU ordering and enforce max size.
        _FLOOD_STATE[key] = (tokens, last_ts, now)
        _FLOOD_STATE.move_to_end(key, last=True)
        while len(_FLOOD_STATE) > max_keys:
            _FLOOD_STATE.popitem(last=False)

        # Provide placeholders for templates.
        approx_hits = int(max(0.0, float(burst) - tokens))
        return matched, {
            "flood_count": count,
            "flood_seconds": seconds,
            "flood_hits": approx_hits,
            "flood_tokens": round(tokens, 2),
            "flood_max_keys": max_keys,
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
            should_kick = rule.get("kick", 0)
            ban_length = rule.get("ban_length", 0)
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
                        _db_init_unbans(db)
                        _db_schedule_unban(
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


def _db_init_unbans(db) -> None:
    db.execute(
        "create table if not exists crowdcontrol_unbans(" "server, chan, mask, unban_at real, " "primary key(server, chan, mask))"
    )
    db.commit()


def _db_schedule_unban(db, *, server: str, chan: str, mask: str, unban_at: int) -> None:
    # Upsert-like behavior across SQLite/Postgres wrapper.
    db.execute(
        "insert or replace into crowdcontrol_unbans(server, chan, mask, unban_at) values(?,?,?,?)",
        (server, chan.lower(), mask, float(unban_at)),
    )
    db.commit()


def _db_due_unbans(db, *, server: str, now_ts: float, limit: int):
    return db.execute(
        "select chan, mask from crowdcontrol_unbans where server=? and unban_at<=? order by unban_at asc limit ?",
        (server, float(now_ts), int(limit)),
    ).fetchall()


def _db_delete_unban(db, *, server: str, chan: str, mask: str) -> None:
    db.execute(
        "delete from crowdcontrol_unbans where server=? and chan=? and mask=?",
        (server, chan.lower(), mask),
    )


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
        _db_init_unbans(db)
        due = _db_due_unbans(db, server=server, now_ts=time.time(), limit=batch)
        for chan, mask in due:
            try:
                conn.cmd("MODE", [str(chan), "-b", str(mask)])
                _db_delete_unban(db, server=server, chan=str(chan), mask=str(mask))
            except Exception:
                # Keep the row so it can be retried later.
                continue
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
