"""SQLite persistence layer.

All access goes through this module so the storage engine can later be
swapped (e.g. Postgres/TimescaleDB) by reimplementing these functions —
callers never build SQL themselves. Uniqueness constraints on the
observation tables make repeated ingestion idempotent (duplicate-record
prevention).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name_en TEXT NOT NULL,
    name_zh TEXT,
    h_ticker TEXT NOT NULL UNIQUE,
    a_ticker TEXT NOT NULL,
    sector TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classifications (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    classification TEXT NOT NULL,
    owned INTEGER NOT NULL DEFAULT 0,
    focus INTEGER NOT NULL DEFAULT 0,
    weight REAL,
    shares_held REAL,
    ref_premium_jul2024 REAL,
    notes TEXT,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    old_classification TEXT,
    new_classification TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_obs (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    date TEXT NOT NULL,
    a_close REAL, h_close REAL, fx REAL,
    premium_calc REAL, premium_src REAL,
    a_div_yield REAL, h_div_yield REAL,
    quality TEXT NOT NULL DEFAULT 'ok',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (company_id, date)
);
CREATE TABLE IF NOT EXISTS intraday_obs (
    company_id INTEGER NOT NULL REFERENCES companies(id),
    ts TEXT NOT NULL,
    a_price REAL, h_price REAL, fx REAL,
    premium_calc REAL, premium_src REAL,
    a_is_close INTEGER NOT NULL DEFAULT 0,   -- mainland closed, price = A close
    PRIMARY KEY (company_id, ts)
);
CREATE TABLE IF NOT EXISTS hsahp_daily (
    date TEXT PRIMARY KEY,
    close REAL NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    rule TEXT NOT NULL,
    company TEXT,
    severity TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- ok | degraded | failed | stale | sample
    last_success TEXT,
    last_error TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS constituent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,          -- added | removed
    h_ticker TEXT NOT NULL,
    detail TEXT
);
"""


def connect(db_path: Path | str = config.DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit caches the connection once
    # (st.cache_resource) but runs each rerun in a fresh thread. Access is
    # effectively single-user and SQLite serialises writes internally.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------- companies

def upsert_company(conn, name_en, h_ticker, a_ticker, sector, name_zh=None):
    conn.execute(
        """INSERT INTO companies (name_en, name_zh, h_ticker, a_ticker, sector)
           VALUES (?,?,?,?,?)
           ON CONFLICT(h_ticker) DO UPDATE SET
             name_en=excluded.name_en, name_zh=excluded.name_zh,
             a_ticker=excluded.a_ticker, sector=excluded.sector""",
        (name_en, name_zh, h_ticker, a_ticker, sector))
    return conn.execute("SELECT id FROM companies WHERE h_ticker=?",
                        (h_ticker,)).fetchone()["id"]


def companies_df(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT c.*, k.classification, k.owned, k.focus, k.weight,
                  k.shares_held, k.ref_premium_jul2024, k.notes, k.changed_at
           FROM companies c LEFT JOIN classifications k ON k.company_id=c.id
           ORDER BY c.name_en""", conn)


# ------------------------------------------------------------ classification

def set_classification(conn, company_id: int, classification: str,
                       source: str = "ui", **fields):
    if classification not in config.CLASSIFICATIONS:
        raise ValueError(f"unknown classification: {classification}")
    row = conn.execute(
        "SELECT classification FROM classifications WHERE company_id=?",
        (company_id,)).fetchone()
    old = row["classification"] if row else None
    owned = 1 if classification in (config.FOCUS, config.PORTFOLIO) else \
        int(fields.get("owned", 0))
    focus = 1 if classification == config.FOCUS else 0
    conn.execute(
        """INSERT INTO classifications
             (company_id, classification, owned, focus, weight, shares_held,
              ref_premium_jul2024, notes, changed_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(company_id) DO UPDATE SET
             classification=excluded.classification, owned=excluded.owned,
             focus=excluded.focus,
             weight=COALESCE(excluded.weight, classifications.weight),
             shares_held=COALESCE(excluded.shares_held, classifications.shares_held),
             ref_premium_jul2024=COALESCE(excluded.ref_premium_jul2024,
                                          classifications.ref_premium_jul2024),
             notes=COALESCE(excluded.notes, classifications.notes),
             changed_at=excluded.changed_at""",
        (company_id, classification, owned, focus, fields.get("weight"),
         fields.get("shares_held"), fields.get("ref_premium_jul2024"),
         fields.get("notes"), now_iso()))
    if old != classification:
        conn.execute(
            """INSERT INTO classification_audit
               (company_id, old_classification, new_classification, changed_at, source)
               VALUES (?,?,?,?,?)""",
            (company_id, old, classification, now_iso(), source))
    conn.commit()


def audit_df(conn, limit: int = 200) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT a.changed_at, c.name_en, a.old_classification,
                  a.new_classification, a.source
           FROM classification_audit a JOIN companies c ON c.id=a.company_id
           ORDER BY a.id DESC LIMIT ?""", conn, params=(limit,))


# ------------------------------------------------------------- observations

def insert_daily(conn, rows: list[dict]):
    """Idempotent bulk insert; the PK (company_id, date) rejects duplicates
    and the upsert refreshes values on re-ingestion of the same day."""
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
    conn.commit()


def daily_df(conn, company_id: int | None = None) -> pd.DataFrame:
    q = "SELECT * FROM daily_obs"
    params: tuple = ()
    if company_id is not None:
        q += " WHERE company_id=?"
        params = (company_id,)
    df = pd.read_sql_query(q + " ORDER BY date", conn, params=params or None)
    df["date"] = pd.to_datetime(df["date"])
    return df


def insert_hsahp(conn, rows: list[dict]):
    conn.executemany(
        """INSERT INTO hsahp_daily (date, close, source, updated_at)
           VALUES (:date,:close,:source,:updated_at)
           ON CONFLICT(date) DO UPDATE SET close=excluded.close,
             source=excluded.source, updated_at=excluded.updated_at""", rows)
    conn.commit()


def hsahp_series(conn) -> pd.Series:
    df = pd.read_sql_query("SELECT date, close FROM hsahp_daily ORDER BY date",
                           conn)
    if df.empty:
        return pd.Series(dtype=float)
    return pd.Series(df["close"].values,
                     index=pd.to_datetime(df["date"]), name="HSAHP")


# ------------------------------------------------------------------- alerts

def log_alert(conn, rule: str, message: str, company: str | None = None,
              severity: str = "warning"):
    conn.execute(
        "INSERT INTO alerts_log (ts, rule, company, severity, message) "
        "VALUES (?,?,?,?,?)", (now_iso(), rule, company, severity, message))
    conn.commit()


def alerts_df(conn, limit: int = 500) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT ts, rule, company, severity, message FROM alerts_log "
        "ORDER BY id DESC LIMIT ?", conn, params=(limit,))


# ------------------------------------------------------------ source health

def set_source_health(conn, source: str, status: str, message: str = ""):
    ts = now_iso()
    ok = status in ("ok", "sample")
    conn.execute(
        """INSERT INTO source_health (source, status, last_success, last_error, message)
           VALUES (?,?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET status=excluded.status,
             last_success=CASE WHEN ? THEN excluded.last_success
                               ELSE source_health.last_success END,
             last_error=CASE WHEN ? THEN source_health.last_error
                             ELSE excluded.last_error END,
             message=excluded.message""",
        (source, status, ts if ok else None, None if ok else ts, message,
         ok, ok))
    conn.commit()


def source_health_df(conn) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM source_health ORDER BY source", conn)
