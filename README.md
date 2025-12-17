# Skybot (will rename it when find something).

## Goals
* simplicity
  * little boilerplate
  *  minimal magic
* power
  * multithreading
  * automatic reloading
  * extensibility

# Features
* Multithreaded dispatch and the ability to connect to multiple networks at a time.
* Easy plugin development with automatic reloading and a simple hooking API.

# Requirements
To install dependencies, run:

    pip install -r requirements.txt

## Database

Skybot defaults to SQLite (stored under `persist/`).

To use PostgreSQL instead, add this to `config.json`:

    "database": {
      "type": "postgres",
      "postgres": {
        "dsn": "postgresql://USER:PASSWORD@HOST:5432/DBNAME",
        "schema_prefix": "skybot"
      }
    }

Install the driver:

    pip install "psycopg[binary]"

Skybot runs on Python 2.7, 3.7 and Python 3.13.(WIP in some areas to full update code to 3.13, for now partial support.)

## IRCv3 support (message-tags, etc)

Skybot supports a small but useful subset of IRCv3:

* `message-tags` — parses incoming IRCv3 message tags (`@key=value;flag`) and exposes them to plugins
* `batch` — enables related-message groups (used by features like chat history)
* `cap-notify` — server can notify clients about new/removed capabilities
* `labeled-response` — lets clients label commands and correlate server responses via `@label=...`
* `server-time` — enables a `time` tag on servers that support it
* `account-tag` — enables an `account` tag on servers that support it
* `account-notify` — sends `ACCOUNT` messages when a user's account status changes
* `extended-join` — JOIN messages may include account/realname fields
* `userhost-in-names` — NAMES replies may include `nick!user@host` entries
* `chathistory` / `draft/chathistory` — allows requesting chat history via the `CHATHISTORY` command (server support varies)
* `msgid` — when supported, servers attach a `msgid` tag to messages (often delivered via `message-tags`)

### Configuration

IRCv3 capability configuration is per-connection (inside the `connections` object).

Default behavior: Skybot requests `message-tags`, `batch`, `cap-notify`, `labeled-response`, `server-time`, `account-tag`, `account-notify`, `extended-join`, `userhost-in-names`, and (if advertised) `chathistory`/`draft/chathistory`.

To override the requested capabilities:

    "connections": {
      "network name": {
        "server": "irc.example.net",
        "nick": "Skybot",
        "ircv3": {
          "caps": ["message-tags", "batch", "cap-notify", "labeled-response", "server-time", "account-tag", "account-notify", "extended-join", "userhost-in-names", "chathistory", "draft/chathistory", "msgid", "draft/msgid"]
        }
      }
    }

(`"caps": [...]` at the top level of the connection is also accepted for compatibility.)

### Plugin access

For every incoming line, plugins receive a `tags` dict:

* `input.tags` — mapping of tag name to value
  * tags without a value (flag tags) map to `None`
  * tags with an explicit empty value (`key=`) map to `""`

Example:

    @hook.regex(r"^\\.whoami$")
    def whoami(inp, reply=None, tags=None, **kwargs):
        reply("account=%s time=%s" % (tags.get("account"), tags.get("time")))

  If you want to use labeled-response from a plugin:

    label = inp.conn.cmd_labeled("WHO", [inp.nick])
    inp.reply("sent WHO with label=%s" % label)

  ### Chat history notes

  Skybot does not automatically fetch history on connect; it only provides the plumbing.

  If the server supports `CHATHISTORY`, a plugin can request history using the connection object:

  * `conn.chathistory_latest("#channel", limit=50)`
  * `conn.chathistory_before("#channel", reference="2025-12-17T00:00:00.000Z", limit=50)`
  * `conn.chathistory_after("#channel", reference="2025-12-17T00:00:00.000Z", limit=50)`

  Servers deliver results using `BATCH` plus `@batch=...` message tags on the enclosed lines.
  You can read the batch association from `input.tags.get("batch")`.

## Feeds plugin

The feeds plugin ([plugins/feeds.py](plugins/feeds.py)) can watch RSS/Atom feeds and announce new items into IRC channels.

### Configuration

Top-level `feeds` settings:

    "feeds": {
      "poll_interval": 300,
      "max_items_per_poll": 3
    }

Notes:

* Watches are stored in the database, so they survive restarts and work with both SQLite and PostgreSQL.
* The plugin polls on normal bot activity (event-driven), so in a totally idle network it may not poll until some messages/events are seen.

### Commands

* `.feed add <url> [#channel]` — start watching a feed (defaults to the current channel)
* `.feed remove <url> [#channel]` — stop watching a feed
* `.feed list` — list watched feeds
* `.feed info` — show plugin status

## Crowdcontrol plugin

Crowdcontrol ([plugins/crowdcontrol.py](plugins/crowdcontrol.py)) applies moderation rules to channel messages.

### Rule types

Rules are configured under the top-level `crowdcontrol` array. A rule can match by:

* `re` — a regular expression
* `badness` — mojibake/spam heuristic score (matches when `badness(message) >= threshold`)
* `flood` — per-user flood control (matches when a user sends more than `count` lines within `seconds`)

Common fields:

* `msg` — message used either as a warning (`reply`) or as a kick reason
* `kick` — `1` to kick, `0` to only warn
* `ban_length` — `0` no ban, `-1` ban without unbanning, `>0` ban then unban after N seconds

Temporary bans: when `ban_length > 0`, the plugin schedules an unban in the database and unbans asynchronously (no `time.sleep()` blocking). This means unbans survive bot restarts.

Optional tuning (defaults are fine):

    "crowdcontrol_unban": {
      "poll_interval": 10,
      "batch": 50
    }

### Flood control example

    "crowdcontrol": [
      {
        "flood": {
          "count": 5,
          "seconds": 8,
          "escalate": {"ban_after": 2, "window": 600, "ban_length": 300}
        },
        "msg": "Flood in #{channel} ({flood_strikes}/{flood_ban_after}). Action={flood_action}.",
        "kick": 1,
        "ban_length": 0
      }
    ]

Flood escalation (kick first, ban on repeat):

* `escalate.ban_after` — strike count to start banning (default `2`)
* `escalate.window` — seconds in which strikes accumulate before resetting (default `600`)
* `escalate.ban_length` — seconds to ban once `ban_after` is reached (default `300`)

Notes:

* Keep the rule-level `ban_length` as `0` for flood rules. Escalation controls bans.
* Flood escalation uses the same identity key as the flood limiter (`nick!user@host`), scoped per server+channel.

Flood backend:

* Default is in-memory (fast, bounded with LRU).
* Optional DB-backed mode exists if you really want *no per-user state in the bot process*, but it does a DB read+write per message and may not scale well on very busy channels.

    "crowdcontrol": [
      {
        "flood": {"backend": "db", "count": 5, "seconds": 8},
        "msg": "Flood in #{channel} (>{flood_count}/{flood_seconds}s via {flood_backend}).",
        "kick": 1,
        "ban_length": 0
      }
    ]

Flood tuning keys (in-memory backend only):

* `flood.max_keys` — maximum tracked identities (default `50000`)
* `flood.idle_ttl` — evict state for idle identities after N seconds (default `max(300, seconds*10)`)

Flood tuning keys (DB backend):

* `flood.idle_ttl` — controls cleanup of old DB rows

### Mojibake/badness example

    "crowdcontrol": [
      {
        "badness": 2,
        "msg": "Mojibake/spam in #{channel} (score={badness} threshold={threshold}).",
        "kick": 1,
        "ban_length": 60
      }
    ]

### Message placeholders

In `msg`, you can use placeholders (unknown placeholders are left as-is):

* `{channel}` / `{chan}` (also supports Ruby-style `#{channel}`)
* `{nick}` `{user}` `{host}` `{server}` `{message}`
* `{badness}` `{threshold}`
* `{flood_backend}` `{flood_count}` `{flood_seconds}` `{flood_hits}` `{flood_tokens}` `{flood_max_keys}`
* `{flood_strikes}` `{flood_ban_after}` `{flood_strike_window}` `{flood_action}` `{flood_ban_length}`

### Database tables

Crowdcontrol creates the following tables automatically:

* `crowdcontrol_unbans` — scheduled unbans (used when `ban_length > 0`)
* `crowdcontrol_flood` — flood token buckets (only when using `flood.backend: "db"`)
* `crowdcontrol_flood_strikes` — strike counters for flood escalation (only when using `flood.backend: "db"`)

## License
Skybot is public domain. If you find a way to make money using it, I'll be very impressed.

See LICENSE for precise terms.
