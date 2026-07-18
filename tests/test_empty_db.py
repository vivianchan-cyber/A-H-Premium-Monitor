"""Empty-database regression tests.

A freshly provisioned deployment (new Railway PostgreSQL) has the schema
but zero rows. Every derived table must come back with its full column
set and zero rows — the dashboard shows a friendly first-run page, it
never KeyErrors. Runs on SQLite always and additionally on real
PostgreSQL when AHMON_TEST_PG_URL is set."""

from __future__ import annotations

import os

import pytest

from ahmon import alerts, commentary, db, metrics, signals

PG_URL = os.environ.get("AHMON_TEST_PG_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(params=BACKENDS)
def empty_conn(request, tmp_path):
    if request.param == "sqlite":
        conn = db.connect(tmp_path / "empty.db")
    else:
        conn = db.connect(PG_URL)
        db.metadata.drop_all(conn.engine)
        db.metadata.create_all(conn.engine)
    yield conn
    conn.close()


class TestEmptyDatabase:
    def test_monitor_table_keeps_columns(self, empty_conn):
        t = metrics.monitor_table(empty_conn)
        assert len(t) == 0
        assert list(t.columns) == metrics.MONITOR_COLUMNS
        # the exact expression that crashed on Railway (app.py):
        assert int((t["Quality"] == "sample").sum()) == 0

    def test_attribution_and_sector_tables(self, empty_conn):
        t = metrics.monitor_table(empty_conn)
        att = metrics.attribution_table(empty_conn, t)
        assert len(att) == 0
        assert list(att.columns) == metrics.ATTRIBUTION_COLUMNS
        sect = metrics.sector_stats(empty_conn, t)
        assert len(sect) == 0 and "Median premium (%)" in sect.columns
        hist = metrics.sector_history(empty_conn)
        assert len(hist) == 0

    def test_rankings_and_extremes(self, empty_conn):
        t = metrics.monitor_table(empty_conn)
        for key in metrics.RANKINGS:
            rk = metrics.rankings(t, key)
            assert len(rk) == 0
        assert len(metrics.extremes_52w(t)) == 0

    def test_buy_watch_and_alerts(self, empty_conn):
        t = metrics.monitor_table(empty_conn)
        watch = signals.buy_watch(t)
        assert watch.empty and "Why flagged" in watch.columns
        assert signals.log_signals(empty_conn, watch, "2026-07-17") == 0
        fired = alerts.evaluate(empty_conn, t)
        assert len(fired) == 0

    def test_commentary_degrades_to_messages(self, empty_conn):
        t = metrics.monitor_table(empty_conn)
        att = metrics.attribution_table(empty_conn, t)
        assert commentary.daily_commentary(t, att)
        assert commentary.period_commentary(t, "1w")
        assert commentary.closing_summary(
            t, att, metrics.sector_stats(empty_conn, t)) \
            == "No observations today."

    def test_hsahp_and_health_empty(self, empty_conn):
        assert db.hsahp_series(empty_conn).empty
        assert len(db.source_health_df(empty_conn)) == 0
        assert len(db.commentary_df(empty_conn)) == 0
        assert len(db.alerts_df(empty_conn)) == 0


class TestColumnContractStaysInSync:
    def test_populated_table_matches_constant(self, tmp_path):
        """Guard against MONITOR_COLUMNS drifting from the real row dict."""
        from ahmon import config
        conn = db.connect(tmp_path / "one.db")
        cid = db.upsert_company(conn, "Co", "1.HK", "600000.SS",
                                "Financials")
        db.set_classification(conn, cid, config.FOCUS, source="test")
        db.insert_daily(conn, [{
            "company_id": cid, "date": "2026-07-16", "a_close": 10.0,
            "h_close": 8.0, "fx": 1.1, "premium_calc": 37.5,
            "premium_src": 37.4, "a_div_yield": None, "h_div_yield": None,
            "quality": "live", "updated_at": db.now_iso()}])
        t = metrics.monitor_table(conn)
        assert list(t.columns) == metrics.MONITOR_COLUMNS
        conn.close()
