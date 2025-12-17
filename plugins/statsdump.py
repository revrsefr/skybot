"""Admin-only helper to inspect IRCv3 message-tags on STATS output.

Useful on InspIRCd networks with the vendor capability `inspircd.org/stats-tags`.

Usage (admin-only):
  .statsdump [letter] [limit]

Example:
  .statsdump u 20
"""

from __future__ import annotations

import time
import weakref

from util import hook


_MAX_SECONDS = 15
_DEFAULT_LETTER = "u"
_DEFAULT_LIMIT = 25

# Per-connection state so multiple networks don't collide.
# conn -> {"target": str, "letter": str, "limit": int, "seen": int, "tagged": int, "started": float}
_state: "weakref.WeakKeyDictionary[object, dict]" = weakref.WeakKeyDictionary()


def _notice(conn, target: str, msg: str) -> None:
    conn.cmd("NOTICE", [target, msg])


def _parse_args(inp: str) -> tuple[str, int]:
    parts = (inp or "").strip().split()
    letter = parts[0] if parts else _DEFAULT_LETTER
    try:
        limit = int(parts[1]) if len(parts) > 1 else _DEFAULT_LIMIT
    except ValueError:
        limit = _DEFAULT_LIMIT

    letter = letter[:1] if letter else _DEFAULT_LETTER
    limit = max(1, min(limit, 200))
    return letter, limit


@hook.command("statsdump", adminonly=True)
def statsdump(inp, conn=None, nick=None, notice=None, **_):
    letter, limit = _parse_args(inp)

    if "inspircd.org/stats-tags" not in conn.enabled_caps:
        notice(
            "inspircd.org/stats-tags is not enabled (no CAP ACK). "
            "Add it to ircv3.caps and reconnect."
        )
        return

    _state[conn] = {
        "target": nick,
        "letter": letter,
        "limit": limit,
        "seen": 0,
        "tagged": 0,
        "started": time.time(),
    }

    # STATS reply numerics will be picked up by the event hook below.
    conn.cmd("STATS", [letter])
    notice(f"STATS {letter} requested; dumping tagged lines (limit={limit}).")


@hook.event("*")
def _statsdump_events(inp, **_):
    conn = inp.conn
    state = _state.get(conn)
    if not state:
        return

    now = time.time()
    if now - state["started"] > _MAX_SECONDS:
        _notice(conn, state["target"], "statsdump timed out; stopping.")
        _state.pop(conn, None)
        return

    # Stats replies are numerics. End of STATS is typically 219.
    if not inp.command.isdigit():
        return

    # Only dump lines that actually have tags to avoid noise.
    if inp.tags:
        state["seen"] += 1
        state["tagged"] += 1
        _notice(
            conn,
            state["target"],
            f"STATS {state['letter']} {inp.command} tags={inp.tags} params={inp.paraml}",
        )

    # Stop conditions:
    if inp.command == "219":
        _notice(
            conn,
            state["target"],
            f"statsdump done (tagged_lines={state['tagged']}).",
        )
        _state.pop(conn, None)
        return

    if state["seen"] >= state["limit"]:
        _notice(
            conn,
            state["target"],
            f"statsdump hit limit (tagged_lines={state['tagged']}); stopping.",
        )
        _state.pop(conn, None)
