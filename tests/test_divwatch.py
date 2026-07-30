"""Yield-based top-up monitor: top-up arithmetic, status boundaries,
manual DPS approval, not-yield-based names, seed semantics."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import db, divwatch


class TestArithmetic:
    def test_topup_price(self):
        # DPS 0.50 at a 5% target → top-up price 10.00
        assert divwatch.topup_price(0.50, 5.0) == pytest.approx(10.0)
        # cyclical: same DPS at 6% → lower entry price
        assert divwatch.topup_price(0.50, 6.0) == pytest.approx(8.3333,
                                                                abs=1e-3)

    def test_status_boundaries(self):
        # top-up price 10.00
        assert divwatch.classify_topup(10.60, 10.0)[0] == "WAIT"      # +6%
        s, d = divwatch.classify_topup(10.50, 10.0)                   # +5%
        assert s == "NEAR TOP-UP" and d == pytest.approx(5.0)
        assert divwatch.classify_topup(10.01, 10.0)[0] == "NEAR TOP-UP"
        s, d = divwatch.classify_topup(10.0, 10.0)                    # 0%
        assert s == "TOP-UP REVIEW" and d == pytest.approx(0.0)
        assert divwatch.classify_topup(9.0, 10.0)[0] == "TOP-UP REVIEW"

    def test_distance_sign(self):
        _, d = divwatch.classify_topup(12.0, 10.0)
        assert d == pytest.approx(20.0)      # 20% above top-up price
        _, d = divwatch.classify_topup(8.0, 10.0)
        assert d == pytest.approx(-20.0)     # 20% below


class TestShippedList:
    def test_twenty_names_with_expected_profiles(self):
        lst = pd.read_csv(divwatch.WATCHLIST_CSV)
        assert len(lst) == 20 and lst["ticker"].is_unique
        counts = lst["profile"].value_counts()
        assert counts["stable"] == 9
        assert counts["cyclical"] == 8
        assert counts["not_yield_based"] == 3
        by = lst.set_index("ticker")["profile"]
        assert by["2628.HK"] == "not_yield_based"      # China Life
        assert by["338.HK"] == "cyclical"              # Shanghai Petrochem
        assert by["38.HK"] == "cyclical"               # First Tractor


class TestTable:
    @pytest.fixture
    def conn(self, tmp_path):
        c = db.connect(tmp_path / "t.db")
        divwatch.seed_watchlist(c)
        # price rows for a few names via the normal company path
        for name, h, a, price in [
            ("ICBC", "1398.HK", "601398.SS", 7.0),
            ("CNOOC", "883.HK", "600938.SS", 24.0),
            ("BYD", "1211.HK", "002594.SZ", 90.0),
        ]:
            cid = db.upsert_company(c, name, h, a, "Financials")
            db.insert_daily(c, [{
                "company_id": cid, "date": db.now_iso()[:10],
                "a_close": 1.0, "h_close": price, "fx": 1.1,
                "premium_calc": 0.0, "premium_src": 0.0,
                "a_div_yield": None, "h_div_yield": None,
                "quality": "live", "updated_at": db.now_iso()}])
        yield c
        c.close()

    def test_statuses_and_manual_dps(self, conn):
        # ICBC: DPS 0.36 @5% → top-up 7.20; price 7.00 ≤ 7.20 → REVIEW
        db.set_approved_dps(conn, "1398.HK", 0.36, source="test")
        # CNOOC: DPS 1.20 @6% → top-up 20.0; price 24 = +20% → WAIT
        db.set_approved_dps(conn, "883.HK", 1.20, source="test")
        t = divwatch.build_topup_table(conn).set_index("Ticker")
        assert t.loc["1398.HK", "Status"] == "TOP-UP REVIEW"
        assert t.loc["1398.HK", "Top-up price (HKD)"] == pytest.approx(7.2)
        assert t.loc["1398.HK", "Current yield (%)"] == \
            pytest.approx(0.36 / 7.0 * 100)
        assert t.loc["883.HK", "Status"] == "WAIT"
        assert t.loc["883.HK", "Distance to top-up (%)"] == \
            pytest.approx(20.0)
        # review rows sort first
        assert list(t["Status"])[0] == "TOP-UP REVIEW"

    def test_unset_dps_is_explicit(self, conn):
        t = divwatch.build_topup_table(conn).set_index("Ticker")
        assert t.loc["1398.HK", "Status"] == "SET DPS"
        assert pd.isna(t.loc["1398.HK", "Top-up price (HKD)"])

    def test_not_yield_based_gets_no_signal(self, conn):
        db.set_approved_dps(conn, "1211.HK", 1.0, source="test")
        t = divwatch.build_topup_table(conn).set_index("Ticker")
        # shown, priced, but no target / top-up / status even with a DPS
        assert t.loc["1211.HK", "Status"] == "—"
        assert pd.isna(t.loc["1211.HK", "Target yield (%)"])
        assert pd.isna(t.loc["1211.HK", "Top-up price (HKD)"])
        assert t.loc["1211.HK", "Price (HKD)"] == pytest.approx(90.0)

    def test_dps_update_overwrites_and_stamps(self, conn):
        db.set_approved_dps(conn, "1398.HK", 0.30, source="a@x")
        db.set_approved_dps(conn, "1398.HK", 0.36, source="b@x")
        d = db.approved_dps_df(conn).set_index("ticker")
        assert d.loc["1398.HK", "dps_hkd"] == pytest.approx(0.36)
        assert d.loc["1398.HK", "updated_by"] == "b@x"

    def test_seed_updates_profiles_never_drops(self, conn, tmp_path):
        small = tmp_path / "small.csv"
        pd.DataFrame({"ticker": ["883.HK"], "name": ["CNOOC"],
                      "profile": ["stable"]}).to_csv(small, index=False)
        r = divwatch.seed_watchlist(conn, small)
        w = db.watchlist_df(conn).set_index("ticker")
        assert r["total"] == 20                    # nothing dropped
        assert w.loc["883.HK", "profile"] == "stable"   # updated
