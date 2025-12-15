import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple


# Core modules are eval()'d with `bot` injected from bot.py.
# Provide a stub for static analyzers.
try:
    bot  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    bot = None  # type: ignore[assignment]


_IDENT_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize_ident(value: str, *, prefix: str = "") -> str:
    value = (value or "").strip().lower()
    value = _IDENT_RE.sub("_", value)
    value = value.strip("_")
    if not value:
        value = "default"
    if value[0].isdigit():
        value = "s_" + value
    if prefix:
        value = f"{prefix}_{value}"
    # PostgreSQL identifier limit is 63 bytes (ASCII safe here)
    return value[:63]


def _quote_ident(ident: str) -> str:
    ident = str(ident)
    return '"' + ident.replace('"', '""') + '"'


def _qmark_to_percent_s(sql: str) -> str:
    """Convert SQLite qmark placeholders to psycopg %s, avoiding quotes."""

    out: list[str] = []
    quote: Optional[str] = None
    for ch in sql:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        out.append("%s" if ch == "?" else ch)
    return "".join(out)


def _split_sql_csv(inner: str) -> list[str]:
    """Split a comma-separated SQL list while respecting quotes/parens."""

    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: Optional[str] = None

    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue

        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue

        if ch == "(":
            depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            continue

        if ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            continue

        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _pg_add_types_to_create_table(sql: str) -> str:
    """Best-effort conversion of SQLite-style CREATE TABLE to PostgreSQL.

    Existing Skybot plugins often omit column types (valid in SQLite).
    This function fills in sensible defaults.
    """

    m = re.match(
        r"^\s*create\s+table\s+(if\s+not\s+exists\s+)?(?P<name>[\w\"\.]+)\s*\((?P<body>.*)\)\s*$",
        sql,
        flags=re.I | re.S,
    )
    if not m:
        return sql

    name = m.group("name")
    body = m.group("body")
    items = _split_sql_csv(body)

    typed_items: list[str] = []
    for item in items:
        s = item.strip()
        if not s:
            continue

        s_l = s.lower()
        if s_l.startswith(
            (
                "primary key",
                "unique",
                "constraint",
                "foreign key",
                "check",
            )
        ):
            typed_items.append(s)
            continue

        tokens = s.split()
        # Column with no type: "chan" or "deleted default 0" etc.
        if len(tokens) == 1 or tokens[1].lower() in (
            "default",
            "primary",
            "unique",
            "not",
            "references",
            "check",
            "collate",
        ):
            col = tokens[0]
            colname = col.strip('"').lower()
            if colname == "time":
                coltype = "double precision"
            elif colname in ("deleted", "enabled", "active"):
                coltype = "integer"
            else:
                coltype = "text"
            rest = " ".join(tokens[1:])
            typed_items.append(f"{col} {coltype}" + (f" {rest}" if rest else ""))
            continue

        # SQLite REAL -> Postgres double precision
        if tokens[1].lower() == "real":
            typed_items.append(s.replace(tokens[1], "double precision", 1))
            continue

        typed_items.append(s)

    # Emulate SQLite rowid for plugins that query rowid (quote.py).
    has_id = any(re.match(r"^\s*id\b", it, flags=re.I) for it in typed_items)
    if not has_id:
        typed_items.insert(0, "id bigserial")

    prefix = "create table "
    if m.group(1):
        prefix += "if not exists "
    return f"{prefix}{name} (" + ", ".join(typed_items) + ")"


@dataclass
class _PgResult:
    _cursor: Any
    rowcount: int

    def fetchone(self):
        if self._cursor is None:
            return None
        try:
            return self._cursor.fetchone()
        finally:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None

    def fetchall(self):
        if self._cursor is None:
            return []
        try:
            return self._cursor.fetchall()
        finally:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None

    def __del__(self):
        try:
            if self._cursor is not None:
                self._cursor.close()
        except Exception:
            pass


class PostgresDB:
    """Compatibility wrapper exposing sqlite-like `execute()` on PostgreSQL."""

    def __init__(self, conn, *, schema: str):
        self._conn = conn
        self._schema = schema
        self._pk_cache: dict[str, Tuple[str, ...]] = {}

        # Plugins commonly catch `db.IntegrityError`.
        try:
            self.IntegrityError = getattr(__import__("psycopg"), "IntegrityError")
        except Exception:
            self.IntegrityError = Exception

    def close(self):
        return self._conn.close()

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def _get_pk_cols(self, table: str) -> Tuple[str, ...]:
        table = table.strip('"')
        cached = self._pk_cache.get(table)
        if cached is not None:
            return cached

        q = """
            select kcu.column_name
            from information_schema.table_constraints tc
            join information_schema.key_column_usage kcu
              on tc.constraint_name = kcu.constraint_name
             and tc.table_schema = kcu.table_schema
            where tc.constraint_type = 'PRIMARY KEY'
              and tc.table_schema = current_schema()
              and tc.table_name = %s
            order by kcu.ordinal_position
        """
        cur = self._conn.cursor()
        try:
            cur.execute(q, (table,))
            rows = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass

        cols = tuple(r[0] for r in rows) if rows else ()
        self._pk_cache[table] = cols
        return cols

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        if sql is None:
            raise ValueError("sql must not be None")

        s = str(sql).strip()
        s_l = s.lower()

        # Minimal dialect fixes used by existing plugins.
        s = re.sub(r"\bcount\(\)\b", "count(*)", s, flags=re.I)
        s = re.sub(r"\browid\b", "id", s, flags=re.I)

        if s_l.startswith("create table"):
            s = _pg_add_types_to_create_table(s)

        # SQLite INSERT OR ... variants.
        if re.match(r"^\s*insert\s+or\s+ignore\s+into\s+", s, flags=re.I):
            s = re.sub(
                r"\binsert\s+or\s+ignore\s+into\b",
                "insert into",
                s,
                flags=re.I,
            )
            s = _qmark_to_percent_s(s)
            s += " on conflict do nothing"

        elif re.match(r"^\s*insert\s+or\s+fail\s+into\s+", s, flags=re.I):
            s = re.sub(
                r"\binsert\s+or\s+fail\s+into\b",
                "insert into",
                s,
                flags=re.I,
            )
            s = _qmark_to_percent_s(s)

        elif re.match(r"^\s*insert\s+or\s+replace\s+into\s+", s, flags=re.I):
            # Convert to UPSERT using the table primary key.
            m = re.match(
                r"^\s*insert\s+or\s+replace\s+into\s+(?P<table>[\w\"\.]+)\s*\((?P<cols>[^\)]*)\)\s*values\s*\((?P<vals>.*)\)\s*$",
                s,
                flags=re.I | re.S,
            )
            base = re.sub(
                r"\binsert\s+or\s+replace\s+into\b",
                "insert into",
                s,
                flags=re.I,
            )
            base = _qmark_to_percent_s(base)

            if m:
                table = m.group("table")
                cols = [c.strip() for c in _split_sql_csv(m.group("cols")) if c.strip()]
                pk_cols = self._get_pk_cols(table.split(".")[-1])
                if pk_cols:
                    non_pk = [c for c in cols if c.strip('"') not in pk_cols]
                    conflict = ", ".join(pk_cols)
                    if non_pk:
                        set_clause = ", ".join(f"{c}=excluded.{c}" for c in non_pk)
                        s = f"{base} on conflict ({conflict}) do update set {set_clause}"
                    else:
                        s = f"{base} on conflict ({conflict}) do nothing"
                else:
                    # Best-effort fallback.
                    s = f"{base} on conflict do nothing"
            else:
                s = f"{base} on conflict do nothing"

        else:
            s = _qmark_to_percent_s(s)

        cur = self._conn.cursor()
        try:
            cur.execute(s, params or ())
        except Exception as e:
            # Many plugins run table init statements frequently.
            # If a CREATE TABLE races or runs without IF NOT EXISTS for any
            # reason, avoid crashing the plugin thread.
            if s_l.startswith("create table"):
                code = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
                name = e.__class__.__name__
                if code == "42P07" or name == "DuplicateTable":
                    try:
                        cur.close()
                    except Exception:
                        pass
                    return _PgResult(None, 0)
            try:
                cur.close()
            except Exception:
                pass
            raise
        rowcount = getattr(cur, "rowcount", -1)

        # Avoid cursor leaks for non-SELECT statements.
        if not s_l.startswith("select"):
            try:
                cur.close()
            except Exception:
                pass
            cur = None

        return _PgResult(cur, rowcount)


def get_db_connection(conn, name=""):
    """Return a persistent DB connection.

    Single backend selection via config.json (sqlite is default):

      "database": {
        "type": "sqlite" | "postgres",
        "postgres": {
          "dsn": "postgresql://user:pass@host:5432/dbname",
          "schema_prefix": "skybot"
        }
      }
    """

    cfg = getattr(bot, "config", {}) or {}
    db_cfg = cfg.get("database", {}) or {}
    db_type = str(db_cfg.get("type") or "sqlite").strip().lower()

    if db_type in ("sqlite", "sqlite3"):
        if not name:
            name = "%s.%s.db" % (conn.nick, conn.server_host)
        filename = os.path.join(bot.persist_dir, name)
        return sqlite3.connect(filename, timeout=10)

    if db_type in ("postgres", "postgresql", "pg"):
        pg_cfg = db_cfg.get("postgres", {}) or {}
        dsn = pg_cfg.get("dsn")
        if not dsn:
            raise RuntimeError("database.type=postgres but database.postgres.dsn is missing")

        try:
            psycopg = __import__("psycopg")
        except Exception as e:
            raise RuntimeError(
                "PostgreSQL support requires psycopg (pip install 'psycopg[binary]')"
            ) from e

        schema_prefix = _sanitize_ident(str(pg_cfg.get("schema_prefix") or "skybot"))

        # Emulate old behavior (one DB file per connection) via per-connection schema.
        schema = _sanitize_ident(f"{conn.nick}_{conn.server_host}", prefix=schema_prefix)
        if name:
            schema = _sanitize_ident(name, prefix=schema_prefix)

        pg_conn = psycopg.connect(dsn)
        cur = pg_conn.cursor()
        try:
            cur.execute(f"create schema if not exists {_quote_ident(schema)}")
            cur.execute(f"set search_path to {_quote_ident(schema)}")
        finally:
            try:
                cur.close()
            except Exception:
                pass
        pg_conn.commit()

        return PostgresDB(pg_conn, schema=schema)

    raise RuntimeError(f"unknown database.type: {db_type}")


if bot is not None:
    bot.get_db_connection = get_db_connection
