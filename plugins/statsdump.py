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


def _is_active(state: dict) -> bool:
    return (time.time() - state.get("started", 0.0)) <= _MAX_SECONDS


def _notice(conn, target: str, msg: str) -> None:
    conn.cmd("NOTICE", [target, msg])


def _format_tags(tags: dict) -> str:
    if not tags:
        return ""

    parts = []
    for key in sorted(tags.keys()):
        value = tags[key]
        if value is None:
            parts.append(str(key))
        else:
            parts.append(f"{key}={value}")

    return " (" + ", ".join(parts) + ")" if parts else ""


def _stats_message(paraml: list[str]) -> str:
    # Stats numerics usually include our nick as paraml[0].
    if not paraml:
        return ""
    if len(paraml) == 1:
        return paraml[0]
    return " ".join(paraml[1:])


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


@hook.command("statsdump")
def statsdump(text, conn=None, nick=None, notice=None, admin=None):
    letter, limit = _parse_args(text)

    if not admin:
        notice(
            "statsdump: admin-only. Add your nick/host to this connection's "
            "admins list in config.json (e.g. \"admins\": [\"reverse\", \"your.host\"])."
        )
        return

    if "inspircd.org/stats-tags" not in conn.enabled_caps:
        notice(
            "inspircd.org/stats-tags is not enabled (no CAP ACK). "
            "Add it to ircv3.caps and reconnect."
        )
        return

    existing = _state.get(conn)
    if existing and _is_active(existing):
        notice("statsdump already running; wait a moment and try again.")
        return

    # If the user asks for a tiny limit, keep output minimal.
    quiet = limit <= 2

    _state[conn] = {
        "target": nick,
        "letter": letter,
        "limit": limit,
        "seen": 0,
        "tagged": 0,
        "started": time.time(),
        "quiet": quiet,
    }

    # STATS reply numerics will be picked up by the event hook below.
    conn.cmd("STATS", [letter])
    if not quiet:
        notice(f"STATS {letter}: requested (showing tagged lines only, limit={limit}).")


@hook.event("*")
def _statsdump_events(paraml, input=None):
    if input is None:
        return

    conn = input.conn
    state = _state.get(conn)
    if not state:
        return

    now = time.time()
    if now - state["started"] > _MAX_SECONDS:
        _notice(conn, state["target"], "statsdump timed out; stopping.")
        _state.pop(conn, None)
        return

    # Stats replies are numerics. End of STATS is typically 219.
    if not input.command.isdigit():
        return

    # End of STATS report (don't print it; it's just a terminator).
    if input.command == "219":
        if state["tagged"] == 0 and not state.get("quiet"):
            _notice(conn, state["target"], "statsdump: no tagged lines seen.")
        elif state["tagged"] == 0 and state.get("quiet"):
            _notice(conn, state["target"], "statsdump: no tagged lines seen.")

        if not state.get("quiet"):
            _notice(
                conn,
                state["target"],
                f"statsdump done (tagged lines: {state['tagged']}).",
            )
        _state.pop(conn, None)
        return

    # Only dump lines that actually have tags to avoid noise.
    if input.tags:
        state["tagged"] += 1

        message = _stats_message(input.paraml)
        tags = dict(input.tags)
        # The server-time tag is useful but very noisy; show it only if there
        # are other tags too.
        if set(tags.keys()) == {"time"}:
            tags_display = ""
        else:
            tags_display = _format_tags(tags)

        # Common friendly shortcuts.
        if input.command == "242" and message.lower().startswith("server up "):
            message = "Uptime: " + message[len("Server up ") :]

        if message:
            state["seen"] += 1
            _notice(conn, state["target"], f"STATS {state['letter']}: {message}{tags_display}")

    if state["seen"] >= state["limit"]:
        if not state.get("quiet"):
            _notice(
                conn,
                state["target"],
                f"statsdump hit limit (tagged lines: {state['tagged']}); stopping.",
            )
        _state.pop(conn, None)
