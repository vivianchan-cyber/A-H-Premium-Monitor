"""Phase 4 — market-hours staleness, buy-level screen, alert dedupe,
commentary storage, scheduler cycle. Offline (calendars are local data;
network sources are stubbed)."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import alerts, config, db, market_hours, signals


# ------------------------------------------------------------ market hours

class TestMarketHours:
    # 2026-07-15 was a Wednesday (regular HK/SSE trading day).
    def test_open_midsession(self):
        assert market_hours.hk_open("2026-07-15 10:30+08:00")
        assert market_hours.cn_open("2026-07-15 10:30+08:00")

    def test_hk_lunch_break_closed(self):
        assert not market_hours.hk_open("2026-07-15 12:30+08:00")

    def test_evening_and_weekend_closed(self):
        assert not market_hours.any_market_open("2026-07-15 20:00+08:00")
        assert not market_hours.any_market_open("2026-07-18 10:30+08:00")

    def test_stale_during_session_by_tier_threshold(self):
        # Focus threshold is 15 min: 20-minute-old data is stale mid-session
        assert market_hours.is_stale("2026-07-15T10:10:00+08:00",
                                     config.FOCUS,
                                     now="2026-07-15 10:30+08:00")
        assert not market_hours.is_stale("2026-07-15T10:20:00+08:00",
                                         config.FOCUS,
                                         now="2026-07-15 10:30+08:00")

    def test_evening_refresh_stays_fresh_overnight(self):
        # refreshed at 17:13 after the 16:00 close -> fresh at 23:00 and
        # still fresh before the next open
        upd = "2026-07-15T17:13:00+08:00"
        assert not market_hours.is_stale(upd, config.FOCUS,
                                         now="2026-07-15 23:00+08:00")
        assert not market_hours.is_stale(upd, config.FOCUS,
                                         now="2026-07-16 08:00+08:00")

    def test_pre_close_data_goes_stale_after_session(self):
        # last update 14:00, session closed 16:00 -> 2h behind the close
        assert market_hours.is_stale("2026-07-15T14:00:00+08:00",
                                     config.FOCUS,
                                     now="2026-07-15 23:00+08:00")


# ------------------------------------------------------------- buy signals

def monitor_row(**over):
    base = {
        "Company": "Test Bank", "Name (ZH)": "测试银行",
        "H Ticker": "1.HK", "A Ticker": "600000.SS",
        "Classification": config.FOCUS, "Quality": "live",
        "H Price (HKD)": 5.0, "A Price (CNY)": 6.9,
        "FX (HKD per 1 CNY)": 1.16,
        "Premium calc (%)": 60.0,
        "5y median (pp)": 30.0, "H upside to 5y median (%)": 23.1,
        "5y percentile": 92.0, "H div yield (%)": 6.0,
        "P/E (H)": 5.0, "Mkt cap H (HKD bn)": 200.0,
    }
    base.update(over)
    return pd.Series(base)


class TestBuySignals:
    def test_full_setup_flagged_with_reasons(self):
        hit = signals.evaluate_row(monitor_row())
        assert hit is not None and hit["quality_hits"] == 3
        text = " ".join(hit["reasons"])
        assert "cheaper vs its A share than on 92% of days" in text
        assert "gains about +23%" in text
        assert "pays a 6.0% dividend while you wait" in text

    def test_upside_and_percentile_are_required(self):
        assert signals.evaluate_row(
            monitor_row(**{"H upside to 5y median (%)": 10.0})) is None
        assert signals.evaluate_row(
            monitor_row(**{"5y percentile": 50.0})) is None

    def test_needs_min_quality_hits(self):
        row = monitor_row(**{"H div yield (%)": 1.0, "P/E (H)": 30.0,
                             "Mkt cap H (HKD bn)": 200.0})
        assert signals.evaluate_row(row) is None       # only size hits

    def test_stale_and_sample_rows_never_flagged(self):
        for q in ("stale", "sample"):
            t = pd.DataFrame([monitor_row(Quality=q)])
            assert signals.buy_watch(t).empty

    def test_watch_sorted_and_logged_once_per_day(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        t = pd.DataFrame([
            monitor_row(),
            monitor_row(Company="Bigger Upside",
                        **{"H upside to 5y median (%)": 40.0}),
        ])
        w = signals.buy_watch(t)
        assert list(w["Company"]) == ["Bigger Upside", "Test Bank"]
        assert "Why flagged" in w.columns
        # price context columns sit between H price and the premium
        cols = list(w.columns)
        assert cols.index("H Price (HKD)") < cols.index("A Price (CNY)") \
            < cols.index("FX (HKD per 1 CNY)") \
            < cols.index("Premium calc (%)")
        assert w.iloc[0]["FX (HKD per 1 CNY)"] == 1.16
        today = db.now_iso()[:10]     # dedupe key must match log timestamps
        assert signals.log_signals(conn, w, today) == 2
        assert signals.log_signals(conn, w, today) == 0   # deduped
        rules = db.alerts_df(conn)["rule"].tolist()
        assert rules.count("buy_level") == 2
        conn.close()


# ------------------------------------------------------------ alert dedupe

class TestAlertDedupe:
    def test_dedupe_daily_logs_once(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        t = pd.DataFrame([{
            "Company": "Co", "Δ1d (pp)": 9.0, "Δ1m (pp)": None,
            "H % change today": None, "52w percentile": None,
            "Premium z (1y)": None, "Calc-src diff (pp)": None,
            "Quality": "live", "Updated": "t",
        }])
        first = alerts.evaluate(conn, t, dedupe_daily=True)
        second = alerts.evaluate(conn, t, dedupe_daily=True)
        assert len(first) == 1 and len(second) == 0
        assert len(db.alerts_df(conn)) == 1
        conn.close()


# ------------------------------------------------------------- commentary

class TestCommentaryStorage:
    def test_insert_and_read_back(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        db.insert_commentary(conn, "daily", "**Focus:** narrowed.")
        df = db.commentary_df(conn)
        assert list(df["kind"]) == ["daily"]
        assert df.iloc[0]["body"].startswith("**Focus:**")
        conn.close()


# ------------------------------------------------------- scheduler cycle

class TestRunRefreshCycle:
    def test_cycle_refreshes_alerts_and_signals(self, tmp_path):
        from ahmon.scheduler import run_refresh_cycle
        from tests.test_live_sources import (FakeAkshare, FakeHsahp,
                                             FakeYahoo)
        conn = db.connect(tmp_path / "live.db")
        cid = db.upsert_company(conn, "China Construction Bank", "939.HK",
                                "601939.SS", "Financials", "建设银行")
        db.set_classification(conn, cid, config.FOCUS, source="test")
        s = run_refresh_cycle(conn, write_intraday=False,
                              ak_source=FakeAkshare(),
                              yahoo_source=FakeYahoo(),
                              hsahp_source=FakeHsahp())
        assert s["upserted"] == 3
        assert "alerts_fired" in s and "buy_watch" in s
        conn.close()
