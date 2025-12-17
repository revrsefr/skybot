import time


# NOTE: These helpers intentionally accept a sqlite-like db handle (Skybot's
# SQLite connection or PostgresDB wrapper). SQL uses SQLite-style syntax.


# -----------------------------
# Scheduled unbans
# -----------------------------

def init_unbans(db) -> None:
    db.execute(
        "create table if not exists crowdcontrol_unbans(" "server, chan, mask, unban_at real, " "primary key(server, chan, mask))"
    )
    db.commit()


def schedule_unban(db, *, server: str, chan: str, mask: str, unban_at: int) -> None:
    # Upsert-like behavior across SQLite/Postgres wrapper.
    db.execute(
        "insert or replace into crowdcontrol_unbans(server, chan, mask, unban_at) values(?,?,?,?)",
        (server, chan.lower(), mask, float(unban_at)),
    )
    db.commit()


def due_unbans(db, *, server: str, now_ts: float, limit: int):
    return db.execute(
        "select chan, mask from crowdcontrol_unbans where server=? and unban_at<=? order by unban_at asc limit ?",
        (server, float(now_ts), int(limit)),
    ).fetchall()


def delete_unban(db, *, server: str, chan: str, mask: str) -> None:
    db.execute(
        "delete from crowdcontrol_unbans where server=? and chan=? and mask=?",
        (server, chan.lower(), mask),
    )


# -----------------------------
# Flood token bucket (DB backend)
# -----------------------------

def init_flood(db) -> None:
    db.execute(
        "create table if not exists crowdcontrol_flood(" "server, chan, ident, tokens real, last_ts real, last_seen real, " "primary key(server, chan, ident))"
    )


def flood_cleanup(db, *, server: str, now_ts: float, idle_ttl: float, last_cleanup_state: dict) -> None:
    # Run at most once per minute per process.
    last = float(last_cleanup_state.get("t", 0.0) or 0.0)
    if float(now_ts) - last < 60.0:
        return
    last_cleanup_state["t"] = float(now_ts)

    cutoff = float(now_ts) - float(idle_ttl)
    db.execute(
        "delete from crowdcontrol_flood where server=? and last_seen<?",
        (server, float(cutoff)),
    )


def flood_match(
    db,
    *,
    server: str,
    chan: str,
    ident: str,
    burst: float,
    rate: float,
    idle_ttl: float,
    last_cleanup_state: dict,
    last_commit_state: dict,
):
    """DB-backed token bucket.

    Returns (matched: bool, extra: dict).
    """
    init_flood(db)

    now_m = time.monotonic()
    now_ts = time.time()

    flood_cleanup(db, server=server, now_ts=now_ts, idle_ttl=idle_ttl, last_cleanup_state=last_cleanup_state)

    row = db.execute(
        "select tokens, last_ts from crowdcontrol_flood where server=? and chan=? and ident=?",
        (server, chan.lower(), ident),
    ).fetchone()

    if row is None:
        tokens = float(burst)
        last_ts = float(now_m)
    else:
        try:
            tokens = float(row[0])
            last_ts = float(row[1])
        except Exception:
            tokens = float(burst)
            last_ts = float(now_m)

    refill = (float(now_m) - float(last_ts)) * float(rate)
    tokens = min(float(burst), tokens + refill)
    last_ts = float(now_m)

    tokens -= 1.0
    matched = tokens < 0.0
    if matched:
        tokens = float(burst) - 1.0

    db.execute(
        "insert or replace into crowdcontrol_flood(server, chan, ident, tokens, last_ts, last_seen) values(?,?,?,?,?,?)",
        (server, chan.lower(), ident, float(tokens), float(last_ts), float(now_ts)),
    )

    # Commit at most once per second per process.
    last_commit = float(last_commit_state.get("t", 0.0) or 0.0)
    if float(now_m) - last_commit >= 1.0:
        last_commit_state["t"] = float(now_m)
        db.commit()

    approx_hits = int(max(0.0, float(burst) - tokens))
    extra = {
        "flood_backend": "db",
        "flood_count": int(burst),
        "flood_seconds": round(float(burst) / float(rate), 2) if rate > 0 else 0,
        "flood_hits": approx_hits,
        "flood_tokens": round(tokens, 2),
    }
    return matched, extra


# -----------------------------
# Flood strikes (DB backend)
# -----------------------------

def init_flood_strikes(db) -> None:
    db.execute(
        "create table if not exists crowdcontrol_flood_strikes(" "server, chan, ident, strikes real, last_ts real, last_seen real, " "primary key(server, chan, ident))"
    )


def flood_strikes_cleanup(db, *, server: str, now_ts: float, idle_ttl: float, last_cleanup_state: dict) -> None:
    last = float(last_cleanup_state.get("t", 0.0) or 0.0)
    if float(now_ts) - last < 60.0:
        return
    last_cleanup_state["t"] = float(now_ts)

    cutoff = float(now_ts) - float(idle_ttl)
    db.execute(
        "delete from crowdcontrol_flood_strikes where server=? and last_seen<?",
        (server, float(cutoff)),
    )


def flood_strike_bump(
    db,
    *,
    server: str,
    chan: str,
    ident: str,
    now_ts: float,
    window: float,
    idle_ttl: float,
    last_cleanup_state: dict,
) -> int:
    init_flood_strikes(db)
    flood_strikes_cleanup(db, server=server, now_ts=float(now_ts), idle_ttl=float(idle_ttl), last_cleanup_state=last_cleanup_state)

    row = db.execute(
        "select strikes, last_ts from crowdcontrol_flood_strikes where server=? and chan=? and ident=?",
        (server, chan.lower(), ident),
    ).fetchone()

    if row is None:
        strikes = 1
        last_ts = float(now_ts)
    else:
        try:
            old_strikes = int(float(row[0]))
            last_ts = float(row[1])
        except Exception:
            old_strikes = 0
            last_ts = float(now_ts)

        if float(now_ts) - float(last_ts) > float(window):
            strikes = 1
        else:
            strikes = old_strikes + 1
        last_ts = float(now_ts)

    db.execute(
        "insert or replace into crowdcontrol_flood_strikes(server, chan, ident, strikes, last_ts, last_seen) values(?,?,?,?,?,?)",
        (server, chan.lower(), ident, float(strikes), float(last_ts), float(now_ts)),
    )
    return int(strikes)
