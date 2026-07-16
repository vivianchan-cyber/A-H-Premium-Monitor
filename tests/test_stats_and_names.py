"""Fundamentals (P/E, P/B, market cap, dividend yield) and English-name
enrichment — offline."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import config, db, enrich, metrics
from ahmon.sources.akshare_src import parse_ulist_stats


class TestParseUlistStats:
    # Raw rows exactly as the ulist endpoint returns them (fltt=1),
    # values from the verified 2026-07-15 probe for CCB.
    H_ROW = {"f12": "00939", "f13": 116, "f14": "建设银行", "f9": 561,
             "f20": 2147739131778, "f23": 53, "f133": 5.35}
    A_ROW = {"f12": "601939", "f13": 1, "f14": "建设银行", "f9": 746,
             "f20": 2574147753557, "f23": 74, "f133": 3.95}

    def test_scaling_and_legs(self):
        df = parse_ulist_stats([self.H_ROW, self.A_ROW]).set_index("leg")
        assert df.loc["H", "pe_ttm"] == pytest.approx(5.61)
        assert df.loc["H", "pb"] == pytest.approx(0.53)
        assert df.loc["H", "mktcap"] == pytest.approx(2147739131778)
        assert df.loc["H", "div_yield_pct"] == pytest.approx(5.35)
        assert df.loc["A", "pe_ttm"] == pytest.approx(7.46)

    def test_dividend_identity_with_premium(self):
        # Same dividend per share ⇒ H yield / A yield ≈ 1 + premium.
        # (CCB premium was ~35.4% at probe time.)
        df = parse_ulist_stats([self.H_ROW, self.A_ROW]).set_index("leg")
        ratio = df.loc["H", "div_yield_pct"] / df.loc["A", "div_yield_pct"]
        assert 1.3 < ratio < 1.4

    def test_missing_values_stay_none(self):
        row = dict(self.H_ROW, f9="-", f133=None)
        df = parse_ulist_stats([row])
        assert df.iloc[0]["pe_ttm"] is None
        assert df.iloc[0]["div_yield_pct"] is None


class TestEnglishNameEnrichment:
    def test_needs_english_name(self):
        assert enrich.needs_english_name("建设银行")
        assert enrich.needs_english_name("XD辽港股")
        assert not enrich.needs_english_name("China Construction Bank")
        assert not enrich.needs_english_name(None)
        assert not enrich.needs_english_name("")

    def test_enrich_updates_only_cjk_names(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        cid_zh = db.upsert_company(conn, "建设银行", "939.HK", "601939.SS",
                                   "Financials", "建设银行")
        cid_en = db.upsert_company(conn, "Ping An", "2318.HK", "601318.SS",
                                   "Financials", "中国平安")

        class FakeYahoo:
            def fetch_english_name(self, h_ticker):
                assert h_ticker == "939.HK"   # EN-named company not touched
                return "China Construction Bank Corporation"

        s = enrich.enrich_english_names(conn, FakeYahoo(),
                                        log=lambda *_: None)
        assert s == {"candidates": 1, "updated": 1, "failed": []}
        comps = db.companies_df(conn).set_index("h_ticker")
        assert comps.loc["939.HK", "name_en"] == \
            "China Construction Bank Corporation"
        assert comps.loc["939.HK", "name_zh"] == "建设银行"   # ZH kept
        assert comps.loc["2318.HK", "name_en"] == "Ping An"
        conn.close()

    def test_failed_lookup_keeps_chinese_name(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        db.upsert_company(conn, "建设银行", "939.HK", "601939.SS",
                          "Financials", "建设银行")

        class BrokenYahoo:
            def fetch_english_name(self, h_ticker):
                raise RuntimeError("offline")

        s = enrich.enrich_english_names(conn, BrokenYahoo(),
                                        log=lambda *_: None)
        assert s["updated"] == 0 and len(s["failed"]) == 1
        assert db.companies_df(conn).iloc[0]["name_en"] == "建设银行"
        conn.close()


class TestStatsInMonitorTable:
    def test_stats_joined_and_yields_fall_back(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        cid = db.upsert_company(conn, "Co", "1.HK", "600000.SS", "Financials")
        db.set_classification(conn, cid, config.FOCUS, source="test")
        db.insert_daily(conn, [{
            "company_id": cid, "date": "2026-07-15", "a_close": 10.0,
            "h_close": 8.0, "fx": 1.1, "premium_calc": 37.5,
            "premium_src": 37.5, "a_div_yield": None, "h_div_yield": None,
            "quality": "eod", "updated_at": db.now_iso()}])
        db.upsert_stats(conn, [{
            "company_id": cid, "h_mktcap_hkd": 2.0e12, "a_mktcap_cny": 2.5e12,
            "h_pe": 5.61, "a_pe": 7.46, "h_pb": 0.53, "a_pb": 0.74,
            "h_div_yield": 5.35, "a_div_yield": 3.95,
            "updated_at": db.now_iso()}])
        t = metrics.monitor_table(conn).iloc[0]
        assert t["Mkt cap H (HKD bn)"] == pytest.approx(2000.0)
        assert t["Mkt cap A (CNY bn)"] == pytest.approx(2500.0)
        assert t["P/E (H)"] == pytest.approx(5.61)
        assert t["P/B (A)"] == pytest.approx(0.74)
        # daily row had no yields -> falls back to fundamentals
        assert t["H div yield (%)"] == pytest.approx(5.35)
        assert t["A div yield (%)"] == pytest.approx(3.95)
        conn.close()

    def test_no_stats_row_keeps_columns_nan(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        cid = db.upsert_company(conn, "Co", "1.HK", "600000.SS", "Financials")
        db.set_classification(conn, cid, config.FOCUS, source="test")
        db.insert_daily(conn, [{
            "company_id": cid, "date": "2026-07-15", "a_close": 10.0,
            "h_close": 8.0, "fx": 1.1, "premium_calc": 37.5,
            "premium_src": None, "a_div_yield": None, "h_div_yield": None,
            "quality": "eod", "updated_at": db.now_iso()}])
        t = metrics.monitor_table(conn).iloc[0]
        assert pd.isna(t["Mkt cap H (HKD bn)"])
        assert pd.isna(t["P/E (H)"])
        conn.close()
