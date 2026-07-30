"""Dividend buy-level watch: classifier branches (especially CHECK),
trailing-DPS windowing/specials, distance sign, seed semantics."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import db, divwatch


class TestClassifier:
    def test_buy_needs_both_yields_over_threshold(self):
        s, why = divwatch.classify("stable", 5.6, 5.2, 10)
        assert s == "BUY" and "5.60%" in why

    def test_check_when_forward_below_threshold(self):
        s, why = divwatch.classify("stable", 6.5, 4.4, 10)
        assert s == "CHECK"
        assert "expects a cut" in why

    def test_check_when_consensus_missing(self):
        s, why = divwatch.classify("cyclical", 7.0, None, None)
        assert s == "CHECK"
        assert "no forward consensus" in why

    def test_check_when_consensus_stale(self):
        s, why = divwatch.classify("cyclical", 7.0, 6.5, 120)
        assert s == "CHECK"
        assert "120 days" in why

    def test_wait_below_threshold(self):
        s, _ = divwatch.classify("stable", 4.2, 5.5, 10)
        assert s == "WAIT"

    def test_cyclical_threshold_is_six(self):
        assert divwatch.classify("cyclical", 5.5, 6.5, 10)[0] == "WAIT"
        assert divwatch.classify("stable", 5.5, 6.5, 10)[0] == "BUY"

    def test_no_dividend_is_no_policy(self):
        s, _ = divwatch.classify("no_dividend", None, None, None)
        assert s == "NO POLICY"

    def test_no_dividend_that_starts_paying_promotes_to_wait(self):
        s, why = divwatch.classify("no_dividend", 1.2, None, None)
        assert s == "WAIT" and "initiated" in why

    def test_no_data_is_explicit_not_wait(self):
        s, _ = divwatch.classify("stable", None, None, None)
        assert s == "NO DATA"


class TestTrailingDps:
    def events(self, rows):
        return pd.DataFrame(rows, columns=["ticker", "ex_date",
                                           "amount_hkd", "special"])

    def test_sums_regulars_in_365d_window(self):
        ev = self.events([
            ("1.HK", "2026-03-01", 0.30, 0),
            ("1.HK", "2025-09-01", 0.25, 0),
            ("1.HK", "2025-06-01", 0.20, 0),   # 423 days back — out
        ])
        assert divwatch.trailing_dps(ev, "2026-07-28") == pytest.approx(0.55)

    def test_specials_excluded(self):
        ev = self.events([
            ("1.HK", "2026-03-01", 0.30, 0),
            ("1.HK", "2026-03-01", 1.00, 1),   # capital return
        ])
        assert divwatch.trailing_dps(ev, "2026-07-28") == pytest.approx(0.30)

    def test_none_when_no_events(self):
        assert divwatch.trailing_dps(self.events([]), "2026-07-28") is None


class TestDistance:
    def test_in_range_shows_positive_headroom(self):
        # dps 0.60, threshold 5% → threshold price 12.0; price 10 → +20%
        d = divwatch.distance_to_threshold_pct(10.0, 0.60, 5.0)
        assert d == pytest.approx(20.0)

    def test_below_range_is_negative(self):
        # price 15 → must fall 20% to reach 12.0
        d = divwatch.distance_to_threshold_pct(15.0, 0.60, 5.0)
        assert d == pytest.approx(-20.0)

    def test_none_without_dps(self):
        assert divwatch.distance_to_threshold_pct(10.0, None, 5.0) is None


class TestSeedAndTable:
    @pytest.fixture
    def conn(self, tmp_path):
        c = db.connect(tmp_path / "t.db")
        yield c
        c.close()

    def test_seed_idempotent_and_never_drops(self, conn, tmp_path):
        r1 = divwatch.seed_watchlist(conn)
        assert r1["total"] == 20 and len(r1["added"]) == 20
        # a smaller CSV must never remove names
        small = tmp_path / "small.csv"
        pd.DataFrame({"ticker": ["883.HK"], "name": ["CNOOC"],
                      "profile": ["cyclical"]}).to_csv(small, index=False)
        r2 = divwatch.seed_watchlist(conn, small)
        assert r2["total"] == 20 and r2["added"] == []

    def test_seed_rejects_unknown_profile(self, conn, tmp_path):
        bad = tmp_path / "bad.csv"
        pd.DataFrame({"ticker": ["1.HK"], "name": ["X"],
                      "profile": ["growth"]}).to_csv(bad, index=False)
        with pytest.raises(ValueError, match="unknown profiles"):
            divwatch.seed_watchlist(conn, bad)

    def test_table_from_manual_rows_classifies_and_sorts(self, conn):
        divwatch.seed_watchlist(conn)
        today = db.now_iso()[:10]
        db.insert_dps_events(conn, [
            {"ticker": "941.HK", "ex_date": today, "amount_hkd": 4.60,
             "source": "manual"},
            {"ticker": "857.HK", "ex_date": today, "amount_hkd": 0.50,
             "source": "manual"},
        ])
        db.upsert_yield_daily(conn, [
            # China Mobile: 4.60/80 = 5.75% trailing, forward 5.5% → BUY
            {"ticker": "941.HK", "date": today, "price": 80.0,
             "forward_dps_consensus": 4.40, "consensus_asof": today,
             "as_of": today, "source": "manual", "quality": "ok"},
            # PetroChina (cyclical): 0.50/6 = 8.3% but no consensus → CHECK
            {"ticker": "857.HK", "date": today, "price": 6.0,
             "as_of": today, "source": "manual", "quality": "ok"},
        ])
        t = divwatch.build_watch_table(conn).set_index("Ticker")
        assert t.loc["941.HK", "Status"] == "BUY"
        assert t.loc["857.HK", "Status"] == "CHECK"
        assert t.loc["1211.HK", "Status"] == "NO POLICY"     # BYD
        assert t.loc["1398.HK", "Status"] == "NO DATA"       # ICBC, no row
        # sort: BUY first, then CHECK, WAIT/NO POLICY/NO DATA after
        statuses = list(t["Status"])
        assert statuses[0] == "BUY" and statuses[1] == "CHECK"
        # days in range counts stored in-range days
        assert t.loc["941.HK", "Days in range"] == 1

    def test_vendor_fallback_is_labelled(self, conn):
        divwatch.seed_watchlist(conn)
        today = db.now_iso()[:10]
        db.upsert_yield_daily(conn, [
            {"ticker": "939.HK", "date": today, "price": 8.0,
             "vendor_yield": 6.1, "as_of": today,
             "source": "vendor_yield", "quality": "ok"},
        ])
        t = divwatch.build_watch_table(conn).set_index("Ticker")
        assert t.loc["939.HK", "Status"] == "CHECK"   # in range, no fwd
        assert t.loc["939.HK", "Yield source"] == "vendor_yield"
        assert t.loc["939.HK", "Trailing yield (%)"] == pytest.approx(6.1)
