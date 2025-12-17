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


@hook.regex(r".*")
def crowdcontrol(
    inp,
    kick=None,
    ban=None,
    unban=None,
    reply=None,
    bot=None,
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

        key = (chan, _flood_key())
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
                ban()
            if should_kick:
                kick(reason=reason)
            elif "msg" in rule:
                reply(reason)
            if ban_length > 0:
                time.sleep(ban_length)
                unban()
