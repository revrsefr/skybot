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

Temporary ban caveat: when `ban_length > 0`, the plugin currently uses `time.sleep()` before unbanning, which blocks the crowdcontrol handler for that duration. Prefer kick-only (`ban_length: 0`) unless you explicitly want that behavior.

### Flood control example

    "crowdcontrol": [
      {
        "flood": {"count": 5, "seconds": 8},
        "msg": "Flood in #{channel} (lines>{flood_count} in {flood_seconds}s).",
        "kick": 1,
        "ban_length": 0
      }
    ]

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
* `{flood_count}` `{flood_seconds}` `{flood_hits}`

## License
Skybot is public domain. If you find a way to make money using it, I'll be very impressed.

See LICENSE for precise terms.
