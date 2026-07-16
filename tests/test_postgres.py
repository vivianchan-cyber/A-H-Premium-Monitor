"""PostgreSQL integration tests — run only when AHMON_TEST_PG_URL is set
(CI/local with a disposable database; every test drops and recreates the
schema). Covers the dialect-sensitive pieces: upsert syntax, boolean CASE
in source_health, the NULL-safe alert dedupe, advisory writer locks, the
full stubbed refresh cycle, and the SQLite→PostgreSQL migration."""

from __future__ import annotations

import os

import pytest

from ahmon import config, db

PG_URL = os.environ.get("AHMON_TEST_PG_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="AHMON_TEST_PG_URL not set")


@pytest.fixture
def pg():
    conn = db.connect(PG_URL)
    db.metadata.drop_all(conn.engine)
    db.metadata.create_all(conn.engine)
    yield conn
    conn.close()


class TestPostgresBasics:
    def test_company_upsert_and_autoincrement(self, pg):
        cid = db.upsert_company(pg, "CCB", "939.HK", "601939.SS",
                                "Financials", "建设银行")
        cid2 = db.upsert_company(pg, "CCB renamed", "939.HK", "601939.SS",
                                 "Financials")
        assert cid == cid2                       # upsert, not duplicate
        cid3 = db.upsert_company(pg, "Other", "1.HK", "600000.SS", "Other")
        assert cid3 != cid                       # sequence advances

    def test_daily_upsert_idempotent(self, pg):
        cid = db.upsert_company(pg, "Co", "1.HK", "600000.SS", "F")
        row = {"company_id": cid, "date": "2026-07-16", "a_close": 10.0,
               "h_close": 8.0, "fx": 1.1, "premium_calc": 37.5,
               "premium_src": 37.4, "a_div_yield": None,
               "h_div_yield": None, "quality": "live",
               "updated_at": db.now_iso()}
        db.insert_daily(pg, [row])
        db.insert_daily(pg, [dict(row, a_close=10.5)])
        d = db.daily_df(pg)
        assert len(d) == 1 and d.iloc[0]["a_close"] == pytest.approx(10.5)

    def test_source_health_boolean_case(self, pg):
        db.set_source_health(pg, "src", "ok", "fine")
        ok = db.source_health_df(pg).iloc[0]
        assert ok["last_success"] and not ok["last_error"]
        db.set_source_health(pg, "src", "failed", "boom")
        bad = db.source_health_df(pg).iloc[0]
        assert bad["last_error"] and bad["last_success"]  # success kept

    def test_alert_dedupe_null_safe(self, pg):
        day = db.now_iso()[:10]
        db.log_alert(pg, "r", "m", None)
        assert db.alert_exists_on_day(pg, "r", None, day)
        assert not db.alert_exists_on_day(pg, "r", "SomeCo", day)

    def test_classification_audit(self, pg):
        cid = db.upsert_company(pg, "Co", "1.HK", "600000.SS", "F")
        db.set_classification(pg, cid, config.FOCUS, source="test")
        db.set_classification(pg, cid, config.WATCHLIST, source="test")
        audit = db.audit_df(pg)
        assert len(audit) == 2
        assert audit.iloc[0]["new_classification"] == config.WATCHLIST

    def test_writer_lock_exclusive_across_connections(self, pg):
        other = db.connect(PG_URL)
        try:
            assert pg.try_writer_lock(424242)
            assert not other.try_writer_lock(424242)   # held elsewhere
        finally:
            other.close()


class TestPostgresRefreshCycle:
    def test_full_stubbed_cycle(self, pg):
        from ahmon.scheduler import run_refresh_cycle
        from tests.test_live_sources import (FakeAkshare, FakeHsahp,
                                             FakeYahoo)
        cid = db.upsert_company(pg, "China Construction Bank", "939.HK",
                                "601939.SS", "Financials", "建设银行")
        db.set_classification(pg, cid, config.FOCUS, source="test")
        s = run_refresh_cycle(pg, write_intraday=False,
                              ak_source=FakeAkshare(),
                              yahoo_source=FakeYahoo(),
                              hsahp_source=FakeHsahp())
        assert s["upserted"] == 3
        assert db.hsahp_series(pg).iloc[-1] == pytest.approx(125.04)
        health = db.source_health_df(pg)
        assert (health["status"] == "ok").all()


class TestMigration:
    def test_sqlite_to_postgres(self, pg, tmp_path):
        from ahmon.migrate import migrate
        src = db.connect(tmp_path / "src.db")
        cid = db.upsert_company(src, "CCB", "939.HK", "601939.SS",
                                "Financials", "建设银行")
        db.set_classification(src, cid, config.FOCUS, source="seed")
        db.insert_daily(src, [{
            "company_id": cid, "date": "2026-07-15", "a_close": 10.0,
            "h_close": 8.0, "fx": 1.1, "premium_calc": 37.5,
            "premium_src": 37.4, "a_div_yield": None, "h_div_yield": None,
            "quality": "live", "updated_at": db.now_iso()}])
        db.insert_hsahp(src, [{"date": "2026-07-15", "close": 125.04,
                               "source": "eastmoney_mirror(100.HSAHP)",
                               "updated_at": db.now_iso()}])
        db.log_alert(src, "rule", "msg", "CCB")
        db.set_source_health(src, "akshare (A-H universe & prices)", "ok")
        src.close()

        counts = migrate(str(tmp_path / "src.db"), PG_URL, replace=True)
        assert counts["companies"] == 1
        assert counts["daily_obs"] == 1

        comps = db.companies_df(pg)
        assert comps.iloc[0]["name_zh"] == "建设银行"     # unicode intact
        assert comps.iloc[0]["classification"] == config.FOCUS
        assert db.hsahp_series(pg).iloc[-1] == pytest.approx(125.04)
        # sequences advanced past migrated ids: inserts must not collide
        new_id = db.upsert_company(pg, "New", "2.HK", "600001.SS", "F")
        assert new_id == 2
        db.log_alert(pg, "post", "works", None)
        assert len(db.alerts_df(pg)) == 2
