"""Persistence layer — SQLAlchemy Core over PostgreSQL or SQLite.

All storage access goes through this module (project rule), which is what
makes the engine swappable: callers never build SQL themselves.

Backend selection (ahmon/config.py):
- `DATABASE_URL` set (Railway PostgreSQL, `postgres://…`)  → PostgreSQL
  with connection pooling (pool_pre_ping so dropped connections are
  replaced, not surfaced to users).
- otherwise → the local SQLite file (AHMON_DB / data/ahmon.db), exactly
  as before — SQLite remains the local-development fallback.

Uniqueness constraints on the observation tables make repeated ingestion
idempotent in both dialects (INSERT … ON CONFLICT works verbatim on
PostgreSQL and SQLite ≥3.24). Timestamps are stored as ISO text in
Asia/Singapore, unchanged from Phase 1, so the two backends hold
byte-identical data — which is also what makes `python -m ahmon.migrate`
a straight table copy.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text

from . import config

# ------------------------------------------------------------------ schema

metadata = sa.MetaData()

sa.Table(
    "companies", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("name_en", sa.Text, nullable=False),
    sa.Column("name_zh", sa.Text),
    sa.Column("h_ticker", sa.Text, nullable=False, unique=True),
    sa.Column("a_ticker", sa.Text, nullable=False),
    sa.Column("sector", sa.Text, nullable=False))

sa.Table(
    "classifications", metadata,
    sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
              primary_key=True),
    sa.Column("classification", sa.Text, nullable=False),
    sa.Column("owned", sa.Integer, nullable=False, server_default="0"),
    sa.Column("focus", sa.Integer, nullable=False, server_default="0"),
    sa.Column("weight", sa.Float),
    sa.Column("shares_held", sa.Float),
    sa.Column("ref_premium_jul2024", sa.Float),
    sa.Column("notes", sa.Text),
    sa.Column("changed_at", sa.Text, nullable=False))

sa.Table(
    "classification_audit", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
              nullable=False),
    sa.Column("old_classification", sa.Text),
    sa.Column("new_classification", sa.Text, nullable=False),
    sa.Column("changed_at", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False))

sa.Table(
    "daily_obs", metadata,
    sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
              primary_key=True),
    sa.Column("date", sa.Text, primary_key=True),
    sa.Column("a_close", sa.Float), sa.Column("h_close", sa.Float),
    sa.Column("fx", sa.Float),
    sa.Column("premium_calc", sa.Float), sa.Column("premium_src", sa.Float),
    sa.Column("a_div_yield", sa.Float), sa.Column("h_div_yield", sa.Float),
    sa.Column("quality", sa.Text, nullable=False, server_default="ok"),
    sa.Column("updated_at", sa.Text, nullable=False))

sa.Table(
    "intraday_obs", metadata,
    sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
              primary_key=True),
    sa.Column("ts", sa.Text, primary_key=True),
    sa.Column("a_price", sa.Float), sa.Column("h_price", sa.Float),
    sa.Column("fx", sa.Float),
    sa.Column("premium_calc", sa.Float), sa.Column("premium_src", sa.Float),
    sa.Column("a_is_close", sa.Integer, nullable=False, server_default="0"))

sa.Table(
    "hsahp_daily", metadata,
    sa.Column("date", sa.Text, primary_key=True),
    sa.Column("close", sa.Float, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False))

sa.Table(
    "company_stats", metadata,
    sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
              primary_key=True),
    sa.Column("h_mktcap_hkd", sa.Float), sa.Column("a_mktcap_cny", sa.Float),
    sa.Column("h_pe", sa.Float), sa.Column("a_pe", sa.Float),
    sa.Column("h_pb", sa.Float), sa.Column("a_pb", sa.Float),
    sa.Column("h_div_yield", sa.Float), sa.Column("a_div_yield", sa.Float),
    sa.Column("updated_at", sa.Text, nullable=False))

sa.Table(
    "commentary_log", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False))

sa.Table(
    "alerts_log", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("rule", sa.Text, nullable=False),
    sa.Column("company", sa.Text),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("message", sa.Text, nullable=False))

sa.Table(
    "source_health", metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("last_success", sa.Text),
    sa.Column("last_error", sa.Text),
    sa.Column("message", sa.Text))

sa.Table(
    "constituent_log", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("h_ticker", sa.Text, nullable=False),
    sa.Column("detail", sa.Text))

TABLES_IN_FK_ORDER = ["companies", "classifications", "classification_audit",
                      "daily_obs", "intraday_obs", "hsahp_daily",
                      "company_stats", "commentary_log", "alerts_log",
                      "source_health", "constituent_log"]
SERIAL_TABLES = ["companies", "classification_audit", "commentary_log",
                 "alerts_log", "constituent_log"]


# --------------------------------------------------------------- connection

class _Row:
    """sqlite3.Row-compatible view over a SQLAlchemy row: index access,
    name access, keys() (so dict(row) works) and value iteration."""

    __slots__ = ("_row", "_map")

    def __init__(self, row):
        self._row = tuple(row)
        self._map = dict(row._mapping)

    def __getitem__(self, key):
        return self._row[key] if isinstance(key, int) else self._map[key]

    def keys(self):
        return self._map.keys()

    def __iter__(self):
        return iter(self._row)

    def __len__(self):
        return len(self._row)

    def __repr__(self):
        return f"Row({self._map!r})"


class _Result:
    def __init__(self, rows):
        self._rows = [_Row(r) for r in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class DB:
    """Thin engine wrapper with a sqlite3-like execute() surface so the
    rest of the code (and the tests) read the same on both backends.
    Every execute is its own transaction; commit() is a compatibility
    no-op."""

    def __init__(self, engine: sa.Engine):
        self.engine = engine
        self.dialect = engine.dialect.name
        self._lock_conn = None

    def execute(self, sql: str, params=None) -> _Result:
        with self.engine.begin() as c:
            res = c.execute(text(sql), params or {})
            rows = res.fetchall() if res.returns_rows else []
        return _Result(rows)

    def executemany(self, sql: str, rows: list[dict]):
        if not rows:
            return
        with self.engine.begin() as c:
            c.execute(text(sql), rows)

    def commit(self):   # compatibility with the old sqlite3 call sites
        pass

    def read_df(self, sql: str, params=None) -> pd.DataFrame:
        with self.engine.connect() as c:
            return pd.read_sql_query(text(sql), c, params=params or {})

    def try_writer_lock(self, key: int = 911_001) -> bool:
        """Cross-process exclusive-writer lock so only one scheduled
        process writes market updates at a time. PostgreSQL: a session
        advisory lock held for this object's lifetime. SQLite: the file
        is single-host, the database's own locking suffices → True."""
        if self.dialect != "postgresql":
            return True
        if self._lock_conn is None:
            self._lock_conn = self.engine.connect()
        got = self._lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        return bool(got)

    def close(self):
        if self._lock_conn is not None:
            self._lock_conn.close()
            self._lock_conn = None
        self.engine.dispose()


def _to_url(target) -> str:
    if target is None:
        if config.DATABASE_URL:
            target = config.DATABASE_URL
        else:
            target = config.DB_PATH
    s = str(target)
    if s.startswith("postgres://"):          # Railway's legacy scheme
        return "postgresql+psycopg2://" + s[len("postgres://"):]
    if s.startswith("postgresql://"):
        return "postgresql+psycopg2://" + s[len("postgresql://"):]
    if "://" in s:
        return s
    Path(s).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{s}"


def connect(db_path=None) -> DB:
    """Open the configured database (see module docstring), create any
    missing tables, and return the wrapper. `db_path` may be a SQLite
    file path (local dev / tests) or any SQLAlchemy URL; default follows
    DATABASE_URL, then AHMON_DB."""
    url = _to_url(db_path)
    if url.startswith("sqlite"):
        engine = sa.create_engine(
            url, connect_args={"check_same_thread": False})
    else:
        engine = sa.create_engine(
            url, pool_pre_ping=True, pool_size=5, max_overflow=5,
            pool_recycle=1800)
    metadata.create_all(engine)
    return DB(engine)


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- companies

def upsert_company(conn, name_en, h_ticker, a_ticker, sector, name_zh=None):
    conn.execute(
        """INSERT INTO companies (name_en, name_zh, h_ticker, a_ticker, sector)
           VALUES (:name_en,:name_zh,:h_ticker,:a_ticker,:sector)
           ON CONFLICT(h_ticker) DO UPDATE SET
             name_en=excluded.name_en, name_zh=excluded.name_zh,
             a_ticker=excluded.a_ticker, sector=excluded.sector""",
        {"name_en": name_en, "name_zh": name_zh, "h_ticker": h_ticker,
         "a_ticker": a_ticker, "sector": sector})
    return conn.execute("SELECT id FROM companies WHERE h_ticker=:h",
                        {"h": h_ticker}).fetchone()["id"]


def update_company_name_en(conn, company_id: int, name_en: str):
    conn.execute("UPDATE companies SET name_en=:n WHERE id=:i",
                 {"n": name_en, "i": company_id})


def companies_df(conn) -> pd.DataFrame:
    return conn.read_df(
        """SELECT c.*, k.classification, k.owned, k.focus, k.weight,
                  k.shares_held, k.ref_premium_jul2024, k.notes, k.changed_at
           FROM companies c LEFT JOIN classifications k ON k.company_id=c.id
           ORDER BY c.name_en""")


# ------------------------------------------------------------ classification

def set_classification(conn, company_id: int, classification: str,
                       source: str = "ui", **fields):
    if classification not in config.CLASSIFICATIONS:
        raise ValueError(f"unknown classification: {classification}")
    row = conn.execute(
        "SELECT classification FROM classifications WHERE company_id=:c",
        {"c": company_id}).fetchone()
    old = row["classification"] if row else None
    owned = 1 if classification in (config.FOCUS, config.PORTFOLIO) else \
        int(fields.get("owned", 0))
    focus = 1 if classification == config.FOCUS else 0
    conn.execute(
        """INSERT INTO classifications
             (company_id, classification, owned, focus, weight, shares_held,
              ref_premium_jul2024, notes, changed_at)
           VALUES (:cid,:cls,:owned,:focus,:weight,:shares,:refp,:notes,:ts)
           ON CONFLICT(company_id) DO UPDATE SET
             classification=excluded.classification, owned=excluded.owned,
             focus=excluded.focus,
             weight=COALESCE(excluded.weight, classifications.weight),
             shares_held=COALESCE(excluded.shares_held,
                                  classifications.shares_held),
             ref_premium_jul2024=COALESCE(excluded.ref_premium_jul2024,
                                          classifications.ref_premium_jul2024),
             notes=COALESCE(excluded.notes, classifications.notes),
             changed_at=excluded.changed_at""",
        {"cid": company_id, "cls": classification, "owned": owned,
         "focus": focus, "weight": fields.get("weight"),
         "shares": fields.get("shares_held"),
         "refp": fields.get("ref_premium_jul2024"),
         "notes": fields.get("notes"), "ts": now_iso()})
    if old != classification:
        conn.execute(
            """INSERT INTO classification_audit
               (company_id, old_classification, new_classification,
                changed_at, source)
               VALUES (:cid,:old,:new,:ts,:src)""",
            {"cid": company_id, "old": old, "new": classification,
             "ts": now_iso(), "src": source})


def audit_df(conn, limit: int = 200) -> pd.DataFrame:
    return conn.read_df(
        """SELECT a.changed_at, c.name_en, a.old_classification,
                  a.new_classification, a.source
           FROM classification_audit a JOIN companies c ON c.id=a.company_id
           ORDER BY a.id DESC LIMIT :lim""", {"lim": limit})


# ------------------------------------------------------------- observations

def insert_daily(conn, rows: list[dict]):
    """Idempotent bulk upsert keyed (company_id, date)."""
    conn.executemany(
        """INSERT INTO daily_obs (company_id, date, a_close, h_close, fx,
              premium_calc, premium_src, a_div_yield, h_div_yield, quality,
              updated_at)
           VALUES (:company_id,:date,:a_close,:h_close,:fx,:premium_calc,
                   :premium_src,:a_div_yield,:h_div_yield,:quality,:updated_at)
           ON CONFLICT(company_id, date) DO UPDATE SET
             a_close=excluded.a_close, h_close=excluded.h_close,
             fx=excluded.fx, premium_calc=excluded.premium_calc,
             premium_src=excluded.premium_src,
             a_div_yield=excluded.a_div_yield,
             h_div_yield=excluded.h_div_yield,
             quality=excluded.quality, updated_at=excluded.updated_at""",
        rows)


def daily_df(conn, company_id: int | None = None) -> pd.DataFrame:
    if company_id is None:
        df = conn.read_df("SELECT * FROM daily_obs ORDER BY date")
    else:
        df = conn.read_df(
            "SELECT * FROM daily_obs WHERE company_id=:c ORDER BY date",
            {"c": company_id})
    df["date"] = pd.to_datetime(df["date"])
    return df


def insert_intraday(conn, ticks: list[dict]):
    conn.executemany(
        """INSERT INTO intraday_obs
           (company_id, ts, a_price, h_price, fx, premium_calc,
            premium_src, a_is_close)
           VALUES (:company_id,:ts,:a_price,:h_price,:fx,:premium_calc,
                   :premium_src,:a_is_close)
           ON CONFLICT(company_id, ts) DO UPDATE SET
             a_price=excluded.a_price, h_price=excluded.h_price,
             fx=excluded.fx, premium_calc=excluded.premium_calc,
             premium_src=excluded.premium_src,
             a_is_close=excluded.a_is_close""", ticks)


def has_sample_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM daily_obs "
                        "WHERE quality='sample'").fetchone()["n"]


def history_row_count(conn, company_id: int, before_date: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM daily_obs WHERE company_id=:c "
        "AND date<:d", {"c": company_id, "d": before_date}).fetchone()["n"]


def insert_hsahp(conn, rows: list[dict]):
    conn.executemany(
        """INSERT INTO hsahp_daily (date, close, source, updated_at)
           VALUES (:date,:close,:source,:updated_at)
           ON CONFLICT(date) DO UPDATE SET close=excluded.close,
             source=excluded.source, updated_at=excluded.updated_at""", rows)


def hsahp_series(conn) -> pd.Series:
    df = conn.read_df("SELECT date, close FROM hsahp_daily ORDER BY date")
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(df["close"].values,
                     index=pd.to_datetime(df["date"]), name="HSAHP")


def last_hsahp_date(conn, source: str) -> str | None:
    return conn.execute(
        "SELECT MAX(date) AS d FROM hsahp_daily WHERE source=:s",
        {"s": source}).fetchone()["d"]


def log_constituent(conn, action: str, h_ticker: str, detail: str):
    conn.execute(
        "INSERT INTO constituent_log (ts, action, h_ticker, detail) "
        "VALUES (:ts,:a,:h,:d)",
        {"ts": now_iso(), "a": action, "h": h_ticker, "d": detail})


# -------------------------------------------------------------- fundamentals

def upsert_stats(conn, rows: list[dict]):
    conn.executemany(
        """INSERT INTO company_stats (company_id, h_mktcap_hkd, a_mktcap_cny,
             h_pe, a_pe, h_pb, a_pb, h_div_yield, a_div_yield, updated_at)
           VALUES (:company_id,:h_mktcap_hkd,:a_mktcap_cny,:h_pe,:a_pe,
                   :h_pb,:a_pb,:h_div_yield,:a_div_yield,:updated_at)
           ON CONFLICT(company_id) DO UPDATE SET
             h_mktcap_hkd=excluded.h_mktcap_hkd,
             a_mktcap_cny=excluded.a_mktcap_cny,
             h_pe=excluded.h_pe, a_pe=excluded.a_pe,
             h_pb=excluded.h_pb, a_pb=excluded.a_pb,
             h_div_yield=excluded.h_div_yield,
             a_div_yield=excluded.a_div_yield,
             updated_at=excluded.updated_at""", rows)


def stats_df(conn) -> pd.DataFrame:
    return conn.read_df("SELECT * FROM company_stats")


# --------------------------------------------------------------- commentary

def insert_commentary(conn, kind: str, body: str):
    conn.execute("INSERT INTO commentary_log (ts, kind, body) "
                 "VALUES (:ts,:k,:b)", {"ts": now_iso(), "k": kind,
                                        "b": body})


def commentary_df(conn, limit: int = 100) -> pd.DataFrame:
    return conn.read_df(
        "SELECT ts, kind, body FROM commentary_log ORDER BY id DESC "
        "LIMIT :lim", {"lim": limit})


# ------------------------------------------------------------------- alerts

def log_alert(conn, rule: str, message: str, company: str | None = None,
              severity: str = "warning"):
    conn.execute(
        "INSERT INTO alerts_log (ts, rule, company, severity, message) "
        "VALUES (:ts,:r,:c,:s,:m)",
        {"ts": now_iso(), "r": rule, "c": company, "s": severity,
         "m": message})


def alerts_df(conn, limit: int = 500) -> pd.DataFrame:
    return conn.read_df(
        "SELECT ts, rule, company, severity, message FROM alerts_log "
        "ORDER BY id DESC LIMIT :lim", {"lim": limit})


def alert_exists_on_day(conn, rule: str, company: str | None,
                        day: str) -> bool:
    row = conn.execute(
        """SELECT 1 AS x FROM alerts_log WHERE rule=:r
           AND (company = CAST(:c AS TEXT)
                OR (company IS NULL AND CAST(:c AS TEXT) IS NULL))
           AND ts LIKE :day LIMIT 1""",
        {"r": rule, "c": company, "day": f"{day}%"}).fetchone()
    return row is not None


# ------------------------------------------------------------ source health

def set_source_health(conn, source: str, status: str, message: str = ""):
    ts = now_iso()
    ok = status in ("ok", "sample")
    conn.execute(
        """INSERT INTO source_health (source, status, last_success,
                                      last_error, message)
           VALUES (:src,:st,:ls,:le,:msg)
           ON CONFLICT(source) DO UPDATE SET status=excluded.status,
             last_success=CASE WHEN :ok THEN excluded.last_success
                               ELSE source_health.last_success END,
             last_error=CASE WHEN :ok THEN source_health.last_error
                             ELSE excluded.last_error END,
             message=excluded.message""",
        {"src": source, "st": status, "ls": ts if ok else None,
         "le": None if ok else ts, "msg": message, "ok": ok})


def source_health_df(conn) -> pd.DataFrame:
    return conn.read_df("SELECT * FROM source_health ORDER BY source")
