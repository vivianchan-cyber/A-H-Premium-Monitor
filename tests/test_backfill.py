"""History backfill — offline. Pins the FX-alignment/premium arithmetic of
build_history_rows, the Yahoo history verification, and the 5-year
valuation metrics that the backfill exists to enable."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ahmon import backfill, calc, config, db, metrics
from ahmon.sources import tickers

FX = 1.10


def fx_series(dates, rate=FX):
    return pd.Series(rate, index=pd.DatetimeIndex(dates))


class TestBuildHistoryRows:
    def test_premium_computed_per_day_with_ffilled_fx(self):
        # FX known Mon + Wed only; Tue must use Monday's rate (ffill),
        # never interpolate or guess.
        hist = pd.DataFrame({
            "date": pd.to_datetime(["2026-07-06", "2026-07-07",
                                    "2026-07-08"]),
            "a_close": [10.0, 10.5, 11.0], "h_close": [8.0, 8.0, 8.0]})
        fx = pd.Series([1.10, 1.20],
                       index=pd.to_datetime(["2026-07-06", "2026-07-08"]))
        rows, skipped = backfill.build_history_rows(
            hist, fx, company_id=7, now="t", today="2026-07-15")
        assert skipped == 0 and len(rows) == 3
        assert rows[1]["fx"] == 1.10                       # Tuesday ffilled
        assert rows[1]["premium_calc"] == pytest.approx(
            calc.a_share_premium(10.5, 8.0, 1.10))
        assert all(r["premium_src"] is None for r in rows)
        assert all(r["quality"] == "eod" for r in rows)

    def test_today_and_pre_fx_days_skipped(self):
        hist = pd.DataFrame({
            "date": pd.to_datetime(["2026-07-01", "2026-07-10",
                                    "2026-07-15"]),
            "a_close": [10.0] * 3, "h_close": [8.0] * 3})
        fx = fx_series(["2026-07-09", "2026-07-10"])
        rows, skipped = backfill.build_history_rows(
            hist, fx, 1, "t", today="2026-07-15")
        # 07-01 predates FX history; 07-15 belongs to the live refresh
        assert [r["date"] for r in rows] == ["2026-07-10"]
        assert skipped == 2

    def test_out_of_band_fx_skipped_not_stored(self):
        hist = pd.DataFrame({"date": pd.to_datetime(["2026-07-10"]),
                             "a_close": [10.0], "h_close": [8.0]})
        rows, skipped = backfill.build_history_rows(
            hist, fx_series(["2026-07-10"], rate=3.0), 1, "t", "2026-07-15")
        assert rows == [] and skipped == 1


class TestVerifyAgainstYahoo:
    def test_agreeing_series_near_zero(self):
        dates = pd.date_range("2026-01-01", periods=30, freq="B")
        hist = pd.DataFrame({"date": dates, "h_close": 8.0, "a_close": 10.0})
        assert backfill.verify_against_yahoo(
            hist, pd.Series(8.0, index=dates)) == pytest.approx(0.0)

    def test_divergent_series_measured(self):
        dates = pd.date_range("2026-01-01", periods=30, freq="B")
        hist = pd.DataFrame({"date": dates, "h_close": 8.0, "a_close": 10.0})
        div = backfill.verify_against_yahoo(
            hist, pd.Series(8.0 * 1.05, index=dates))
        assert div == pytest.approx(100 * (1 - 1 / 1.05), abs=0.01)

    def test_no_overlap_returns_none(self):
        hist = pd.DataFrame({"date": pd.to_datetime(["2026-01-05"]),
                             "h_close": [8.0], "a_close": [10.0]})
        other = pd.Series(8.0, index=pd.to_datetime(["2020-01-06"]))
        assert backfill.verify_against_yahoo(hist, other) is None


class TestTencentTickers:
    def test_tx_symbols(self):
        assert tickers.tx_from_a("601939.SS") == "sh601939"
        assert tickers.tx_from_a("000776.SZ") == "sz000776"
        with pytest.raises(ValueError):
            tickers.tx_from_a("601939.XX")


class TestFiveYearValuationMetrics:
    @pytest.fixture
    def conn_with_history(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        cid = db.upsert_company(conn, "Co", "1.HK", "600000.SS", "Financials")
        db.set_classification(conn, cid, config.FOCUS, source="test")
        # 5.5y of deterministic history: premium ramps 0 -> 55, so the
        # latest value is the maximum (100th percentile) and well above
        # the median.
        dates = pd.bdate_range(end="2026-07-15", periods=1400)
        now = db.now_iso()
        rows = []
        for i, d in enumerate(dates):
            prem = i / 25.0
            h = 8.0
            a = h * (1 + prem / 100) / FX
            rows.append({"company_id": cid, "date": d.strftime("%Y-%m-%d"),
                         "a_close": a, "h_close": h, "fx": FX,
                         "premium_calc": prem, "premium_src": None,
                         "a_div_yield": None, "h_div_yield": None,
                         "quality": "eod", "updated_at": now})
        db.insert_daily(conn, rows)
        yield conn
        conn.close()

    def test_gap_percentile_and_median(self, conn_with_history):
        t = metrics.monitor_table(conn_with_history).iloc[0]
        n = config.WINDOW_5Y
        # premium is linear: median of the last n obs, latest = max
        expected_median = (1399 + 1400 - n) / 2 / 25.0
        assert t["5y median (pp)"] == pytest.approx(expected_median, abs=0.05)
        assert t["Dist from 5y median (pp)"] == pytest.approx(
            t["Premium calc (%)"] - t["5y median (pp)"])
        assert t["5y percentile"] == pytest.approx(100.0)

    def test_valuation_rankings_available(self, conn_with_history):
        t = metrics.monitor_table(conn_with_history)
        for key in ("Largest H upside if premium reverts to its 5y median",
                    "Premium above its own 5y norm (extra reversion upside)",
                    "Premium below its own 5y norm (convergence already "
                    "played out)",
                    "Premium near 5y floor (lowest 5y percentile)"):
            rk = metrics.rankings(t, key)
            assert len(rk) == 1
            assert "H upside to 5y median (%)" in rk.columns

    def test_h_centric_metrics(self, conn_with_history):
        t = metrics.monitor_table(conn_with_history).iloc[0]
        p = t["Premium calc (%)"]
        # premium 100% would put H at half the A price; here p = 55.96
        assert t["H discount to A (%)"] == pytest.approx(
            (1 - 1 / (1 + p / 100)) * 100)
        # H return if the premium reverts to the 5y median
        expected = ((1 + p / 100) / (1 + t["5y median (pp)"] / 100) - 1) * 100
        assert t["H upside to 5y median (%)"] == pytest.approx(expected)
        # premium is at its 5y max here, so reversion upside is positive
        assert t["H upside to 5y median (%)"] > 0
