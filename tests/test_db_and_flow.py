"""Persistence, duplicate prevention, classification audit, CSV import."""

import pandas as pd
import pytest

from ahmon import config, db, metrics
from ahmon.sources import csv_import
from ahmon.sources import SchemaChangeError, check_schema


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def seed_company(conn, ticker="2628.HK"):
    cid = db.upsert_company(conn, "China Life", ticker, "601628.SS",
                            "Financials", "中国人寿")
    db.set_classification(conn, cid, config.FOCUS, source="test")
    return cid


class TestDuplicatePrevention:
    def test_daily_upsert_is_idempotent(self, conn):
        cid = seed_company(conn)
        row = {"company_id": cid, "date": "2026-07-14", "a_close": 38.5,
               "h_close": 16.2, "fx": 1.082, "premium_calc": 157.1,
               "premium_src": 157.0, "a_div_yield": 1.5, "h_div_yield": 3.4,
               "quality": "ok", "updated_at": db.now_iso()}
        db.insert_daily(conn, [row])
        db.insert_daily(conn, [row | {"a_close": 38.6}])
        got = db.daily_df(conn, cid)
        assert len(got) == 1                       # no duplicate row
        assert got.iloc[0]["a_close"] == 38.6      # refreshed, not doubled

    def test_hsahp_unique_by_date(self, conn):
        r = {"date": "2026-07-14", "close": 141.2, "source": "test",
             "updated_at": db.now_iso()}
        db.insert_hsahp(conn, [r])
        db.insert_hsahp(conn, [r | {"close": 141.5}])
        s = db.hsahp_series(conn)
        assert len(s) == 1 and s.iloc[0] == 141.5


class TestClassification:
    def test_change_writes_audit(self, conn):
        cid = seed_company(conn)
        db.set_classification(conn, cid, config.WATCHLIST, source="test")
        audit = db.audit_df(conn)
        assert list(audit["new_classification"]) == [config.WATCHLIST,
                                                     config.FOCUS]
        assert audit.iloc[0]["old_classification"] == config.FOCUS

    def test_focus_implies_owned(self, conn):
        cid = seed_company(conn)
        row = db.companies_df(conn).iloc[0]
        assert row["focus"] == 1 and row["owned"] == 1

    def test_unknown_label_rejected(self, conn):
        cid = seed_company(conn)
        with pytest.raises(ValueError):
            db.set_classification(conn, cid, "VIP list")


class TestSchemaGuard:
    def test_missing_column_raises(self):
        df = pd.DataFrame({"a": [1]})
        with pytest.raises(SchemaChangeError):
            check_schema(df, ["a", "b"], "unit-test")


class TestCsvImport:
    def test_import_computes_premium_and_converts_convention(self, conn,
                                                             tmp_path):
        seed_company(conn)
        p = tmp_path / "import.csv"
        p.write_text(
            "date,h_ticker,a_price_cny,h_price_hkd,hkd_per_cny,"
            "premium_src_pct,convention\n"
            "2026-07-14,2628.HK,10.0,9.0,1.08,-16.666667,h_premium\n")
        res = csv_import.import_csv(conn, p)
        assert res["imported"] == 1
        got = db.daily_df(conn).iloc[0]
        assert got["premium_calc"] == pytest.approx(20.0)
        # -16.67% H premium == +20% A premium after conversion
        assert got["premium_src"] == pytest.approx(20.0, abs=1e-4)
        assert got["quality"] == "manual_import"

    def test_unknown_ticker_skipped(self, conn, tmp_path):
        seed_company(conn)
        p = tmp_path / "import.csv"
        p.write_text("date,h_ticker,a_price_cny,h_price_hkd,hkd_per_cny\n"
                     "2026-07-14,9999.HK,1,1,1.08\n")
        res = csv_import.import_csv(conn, p)
        assert res["imported"] == 0 and res["skipped"] == ["9999.HK"]


class TestMonitorTable:
    def test_builds_from_sample_slice(self, conn):
        cid = seed_company(conn)
        now = db.now_iso()
        rows = []
        for i, d in enumerate(pd.bdate_range("2024-01-01", periods=300)):
            rows.append({"company_id": cid, "date": d.strftime("%Y-%m-%d"),
                         "a_close": 10 + i * 0.01, "h_close": 9.0,
                         "fx": 1.08, "premium_calc": 20 + i * 0.01,
                         "premium_src": 20 + i * 0.01,
                         "a_div_yield": 1.5, "h_div_yield": 3.4,
                         "quality": "sample", "updated_at": now})
        db.insert_daily(conn, rows)
        t = metrics.monitor_table(conn)
        assert len(t) == 1
        r = t.iloc[0]
        assert r["Premium calc (%)"] == pytest.approx(20 + 299 * 0.01)
        assert r["Δ1d (pp)"] == pytest.approx(0.01)
        assert r["52w percentile"] == pytest.approx(100.0)
        assert r["Quality"] == "sample"
