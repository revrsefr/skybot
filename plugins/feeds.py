import time
from typing import Dict, List, Optional, Tuple

from util import hook, http


DEFAULT_POLL_INTERVAL = 300
DEFAULT_MAX_ITEMS_PER_POLL = 3

_last_poll_by_conn: dict[tuple[int, Optional[str], Optional[str]], int] = {}


def _now() -> int:
    return int(time.time())


def _cfg(bot) -> dict:
    return bot.config.get("feeds", {}) if bot and getattr(bot, "config", None) else {}


def _poll_interval(bot) -> int:
    cfg = _cfg(bot)
    try:
        return max(30, int(cfg.get("poll_interval", DEFAULT_POLL_INTERVAL)))
    except Exception:
        return DEFAULT_POLL_INTERVAL


def _max_items_per_poll(bot) -> int:
    cfg = _cfg(bot)
    try:
        return max(1, int(cfg.get("max_items_per_poll", DEFAULT_MAX_ITEMS_PER_POLL)))
    except Exception:
        return DEFAULT_MAX_ITEMS_PER_POLL


def _configured_watches(bot) -> List[Tuple[str, str]]:
    """Return list of (channel, url) watches from config.

    config.json example:

      "feeds": {
        "poll_interval": 300,
        "channels": ["#irc"],
        "urls": ["https://feeds.feedburner.com/TheHackersNews"]
      }

    Optionally you can provide explicit mappings:

      "feeds": {
        "poll_interval": 300,
        "watches": [
          {"channel": "#irc", "url": "https://feeds.feedburner.com/TheHackersNews"}
        ]
      }
    """

    cfg = _cfg(bot)

    watches_cfg = cfg.get("watches")
    if isinstance(watches_cfg, list) and watches_cfg:
        out: List[Tuple[str, str]] = []
        for item in watches_cfg:
            if not isinstance(item, dict):
                continue
            channel = str(item.get("channel") or "").strip()
            url = str(item.get("url") or "").strip()
            if channel and url:
                out.append((channel.lower(), url))
        return out

    channels = cfg.get("channels")
    urls = cfg.get("urls")

    if not isinstance(channels, list) or not isinstance(urls, list):
        return []

    out = []
    for chan in channels:
        chan_s = str(chan).strip()
        if not chan_s:
            continue
        for url in urls:
            url_s = str(url).strip()
            if not url_s:
                continue
            out.append((chan_s.lower(), url_s))
    return out


def _db_init(db) -> None:
    db.execute(
        "create table if not exists feed_watches("
        "chan, url, last_id, "
        "primary key(chan, url))"
    )
    db.commit()


def _db_get_watches(db) -> List[Tuple[str, str]]:
    _db_init(db)
    rows = db.execute("select chan, url from feed_watches order by chan, url").fetchall()
    out: List[Tuple[str, str]] = []
    for chan, url in rows:
        try:
            out.append((str(chan).strip().lower(), str(url).strip()))
        except Exception:
            continue
    return out


def _db_add_watch(db, *, chan: str, url: str) -> None:
    _db_init(db)
    db.execute(
        "insert into feed_watches(chan, url, last_id) values(?,?,?)",
        (chan.lower(), url, ""),
    )
    db.commit()


def _db_remove_watch(db, *, chan: str, url: str) -> int:
    _db_init(db)
    res = db.execute(
        "delete from feed_watches where chan=? and url=?",
        (chan.lower(), url),
    )
    db.commit()
    return int(getattr(res, "rowcount", 0) or 0)


def _ensure_watch_rows(db, watches: List[Tuple[str, str]]) -> None:
    _db_init(db)
    for chan, url in watches:
        row = db.execute(
            "select last_id from feed_watches where chan=? and url=?", (chan, url)
        ).fetchone()
        if row is None:
            db.execute(
                "insert into feed_watches(chan, url, last_id) values(?,?,?)",
                (chan, url, ""),
            )
    db.commit()


def _strip(s: str, limit: int = 360) -> str:
    s = " ".join((s or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _parse_feed(xml_bytes: bytes) -> Tuple[str, List[Dict[str, str]]]:
    """Parse RSS/Atom XML into (feed_title, items).

    Items are returned in the order they appear in the feed (typically newest-first).
    Each item has keys: id, title, link.
    """

    # Prefer lxml via util.http (already a dependency). If parsing fails, raise.
    root = http.etree.fromstring(xml_bytes)

    def local(tag: str) -> str:
        return tag.split("}")[-1].lower() if tag else ""

    feed_title = ""
    items: List[Dict[str, str]] = []

    if local(root.tag) == "rss" or local(root.tag) == "rdf":
        channel = None
        for child in root.iterchildren():
            if local(child.tag) == "channel":
                channel = child
                break
        if channel is None:
            channel = root

        for child in channel.iterchildren():
            if local(child.tag) == "title" and not feed_title:
                feed_title = (child.text or "").strip()

        for item in channel.iterchildren():
            if local(item.tag) != "item":
                continue
            title = ""
            link = ""
            guid = ""
            for child in item.iterchildren():
                t = local(child.tag)
                if t == "title" and not title:
                    title = (child.text or "").strip()
                elif t == "link" and not link:
                    link = (child.text or "").strip()
                elif t == "guid" and not guid:
                    guid = (child.text or "").strip()

            item_id = guid or link or title
            if item_id:
                items.append({"id": item_id, "title": title or item_id, "link": link})

        return feed_title, items

    # Atom
    if local(root.tag) == "feed":
        for child in root.iterchildren():
            if local(child.tag) == "title" and not feed_title:
                feed_title = (child.text or "").strip()

        for entry in root.iterchildren():
            if local(entry.tag) != "entry":
                continue
            title = ""
            link = ""
            entry_id = ""

            for child in entry.iterchildren():
                t = local(child.tag)
                if t == "title" and not title:
                    title = (child.text or "").strip()
                elif t == "id" and not entry_id:
                    entry_id = (child.text or "").strip()
                elif t == "link" and not link:
                    href = child.get("href")
                    rel = (child.get("rel") or "alternate").lower()
                    if rel in ("alternate", ""):
                        link = (href or "").strip()

            item_id = entry_id or link or title
            if item_id:
                items.append({"id": item_id, "title": title or item_id, "link": link})

        return feed_title, items

    # Unknown format; treat as empty.
    return feed_title, []


def _fetch_feed(url: str) -> Tuple[str, List[Dict[str, str]]]:
    # FeedBurner and some endpoints behave better with an explicit UA.
    resp = http.open(
        url,
        headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"
        },
    )
    body = resp.read()
    return _parse_feed(body)


def _poll_due(conn, bot) -> bool:
    key = (id(conn), getattr(conn, "server_host", None), getattr(conn, "nick", None))
    now = _now()
    last = int(_last_poll_by_conn.get(key, 0) or 0)
    if now - last < _poll_interval(bot):
        return False
    _last_poll_by_conn[key] = now
    return True


def _format_item(feed_title: str, item: Dict[str, str]) -> str:
    title = http.unescape(item.get("title") or "")
    link = (item.get("link") or "").strip()
    prefix = (feed_title or "Feed").strip()

    if link:
        return _strip(f"[{prefix}] {title} - {link}")
    return _strip(f"[{prefix}] {title}")


@hook.command("feed", autohelp=True)
def feed(inp, bot=None, db=None, chan=""):
    """Manage feed announcements.

    This plugin is primarily configured via config.json (feeds.*). Command is informational:

      .feed info

    """

    parts = (inp or "").strip().split()
    sub = (parts[0].lower() if parts else "info")

    if sub in ("", "info", "help"):
        interval = _poll_interval(bot)
        cfg_watches = _configured_watches(bot)
        db_watches: List[Tuple[str, str]] = []
        if db is not None:
            db_watches = _db_get_watches(db)
        total = len({*cfg_watches, *db_watches})
        return f"feeds: {total} watch(es), poll_interval={interval}s (use: .feed add <url> [#channel], .feed list, .feed remove <url> [#channel])"

    if sub in ("add", "watch"):
        if db is None:
            return "feeds: database is required for .feed add"
        if len(parts) < 2:
            return "usage: .feed add <url> [#channel]"
        url = parts[1].strip()
        target_chan = (parts[2].strip() if len(parts) >= 3 else chan).lower()
        if not target_chan or not target_chan.startswith("#"):
            return "feeds: channel must look like #channel (or run in-channel)"
        if not (url.startswith("http://") or url.startswith("https://")):
            return "feeds: url must start with http:// or https://"
        try:
            _db_add_watch(db, chan=target_chan, url=url)
        except Exception:
            return "feeds: already watching (or database error)"
        return f"feeds: added {url} -> {target_chan}"

    if sub in ("remove", "rm", "del", "unwatch"):
        if db is None:
            return "feeds: database is required for .feed remove"
        if len(parts) < 2:
            return "usage: .feed remove <url> [#channel]"
        url = parts[1].strip()
        target_chan = (parts[2].strip() if len(parts) >= 3 else chan).lower()
        if not target_chan or not target_chan.startswith("#"):
            return "feeds: channel must look like #channel (or run in-channel)"
        removed = _db_remove_watch(db, chan=target_chan, url=url)
        if removed:
            return f"feeds: removed {url} from {target_chan}"
        return "feeds: not found"

    if sub in ("list", "ls"):
        cfg_watches = _configured_watches(bot)
        db_watches: List[Tuple[str, str]] = []
        if db is not None:
            db_watches = _db_get_watches(db)
        watches = sorted({*cfg_watches, *db_watches})
        if not watches:
            return "feeds: no watches"
        # Keep the response short to avoid flooding.
        preview = ", ".join([f"{c} {u}" for (c, u) in watches[:8]])
        more = "" if len(watches) <= 8 else f" (+{len(watches) - 8} more)"
        return "feeds: " + preview + more

    return "unknown subcommand (try: info, add, remove, list)"


@hook.singlethread
@hook.event("*")
def feeds_poll(inp, conn=None, db=None, bot=None):
    # Poll periodically on incoming server traffic.
    if conn is None or db is None or bot is None:
        return

    cfg_watches = _configured_watches(bot)
    db_watches: List[Tuple[str, str]] = []
    if db is not None:
        db_watches = _db_get_watches(db)
    watches = list({*cfg_watches, *db_watches})
    if not watches:
        return

    if not _poll_due(conn, bot):
        return

    _ensure_watch_rows(db, watches)
    max_items = _max_items_per_poll(bot)

    for chan, url in watches:
        try:
            row = db.execute(
                "select last_id from feed_watches where chan=? and url=?", (chan, url)
            ).fetchone()
            last_id = (row[0] if row else "") or ""

            feed_title, items = _fetch_feed(url)
            if not items:
                continue

            newest_id = items[0].get("id") or ""

            # First time: set cursor but don't spam the channel.
            if not last_id:
                db.execute(
                    "update feed_watches set last_id=? where chan=? and url=?",
                    (newest_id, chan, url),
                )
                db.commit()
                continue

            # Collect new items after last_id (chronological).
            new_items: List[Dict[str, str]] = []
            collecting = False
            for it in reversed(items):
                it_id = it.get("id") or ""
                if it_id == last_id:
                    collecting = True
                    continue
                if collecting:
                    new_items.append(it)

            # If last_id fell out of the window, just move the cursor.
            if not collecting:
                db.execute(
                    "update feed_watches set last_id=? where chan=? and url=?",
                    (newest_id, chan, url),
                )
                db.commit()
                continue

            if not new_items:
                db.execute(
                    "update feed_watches set last_id=? where chan=? and url=?",
                    (newest_id, chan, url),
                )
                db.commit()
                continue

            overflow = max(0, len(new_items) - max_items)
            if overflow:
                new_items = new_items[-max_items:]

            for it in new_items:
                conn.msg(chan, _format_item(feed_title, it))

            if overflow:
                conn.msg(chan, f"[{feed_title or 'Feed'}] (+{overflow} more items)")

            db.execute(
                "update feed_watches set last_id=? where chan=? and url=?",
                (newest_id, chan, url),
            )
            db.commit()

        except Exception:
            # Avoid spamming on errors; try again next cycle.
            continue
