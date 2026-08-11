"""Public read-only mode: no portfolio traces in commentary, the
universe-wide yield watch, the auth bypass and the throttled universe
dividend fetch. Offline."""

from __future__ import annotations

import importlib

import pandas as pd
import pytest

from ahmon import commentary, config, db, divwatch


def make_table():
    return pd.DataFrame({
        "Company": ["Bank of China", "PetroChina"],
        "Classification": [config.FOCUS, config.OTHER],
        "Premium calc (%)": [100.0, 50.0],
        "Δ1d (pp)": [-10.0, -6.0],
        "Δ1w (pp)": [-10.0, -6.0], "Δ1m (pp)": [-10.0, -6.0],
        "52w percentile": [None, None],
        "Quality": ["live", "live"],
    })


class TestPublicCommentary:
    def test_daily_shows_universe_only(self):
        att = pd.DataFrame(columns=["Company", "Driver"])
        text = commentary.daily_commentary(make_table(), att, public=True)
        assert "**Full A–H Universe:**" in text
        assert "Focus" not in text and "focus" not in text
        assert "excluding" not in text
        assert "Bank of China" not in text          # no holding names

    def test_period_shows_universe_only(self):
        text = commentary.period_commentary(make_table(), "1w",
                                            public=True)
        assert "**Full A–H Universe (week):**" in text
        assert "Focus" not in text and "focus" not in text

    def test_private_note_unchanged(self):
        att = pd.DataFrame(columns=["Company", "Driver"])
        text = commentary.daily_commentary(make_table(), att)
        assert "**Focus Holdings:**" in text

    def test_public_note_gains_driver_sentence(self):
        att = pd.DataFrame({
            "Company": ["Bank of China", "PetroChina"],
            "Driver": ["H-share outperformance"] * 2,
            "Premium move (pp)": [-10.0, -6.0],
        })
        text = commentary.daily_commentary(make_table(), att, public=True)
        assert "stronger H-share performance" in text


class TestPublicAuth:
    def test_public_mode_returns_open_viewer(self, monkeypatch):
        monkeypatch.setenv("AHMON_PUBLIC", "1")
        importlib.reload(config)
        try:
            from ahmon import auth
            u = auth.require_login()
            assert u == {"email": "public", "role": "viewer",
                         "public": True}
        finally:
            monkeypatch.delenv("AHMON_PUBLIC")
            importlib.reload(config)

    def test_flag_off_by_default(self):
        assert config.PUBLIC_MODE is False


class TestUniverseYieldTable:
    @pytest.fixture
    def conn(self, tmp_path):
        c = db.connect(tmp_path / "t.db")
        for name, h, price in [("ICBC", "1398.HK", 7.0),
                               ("NoDiv Co", "9999.HK", 5.0)]:
            cid = db.upsert_company(c, name, h, f"6{h[:-3]}.SS",
                                    "Financials")
            db.insert_daily(c, [{
                "company_id": cid, "date": db.now_iso()[:10],
                "a_close": 1.0, "h_close": price, "fx": 1.1,
                "premium_calc": 0.0, "premium_src": 0.0,
                "a_div_yield": None, "h_div_yield": None,
                "quality": "live", "updated_at": db.now_iso()}])
        yield c
        c.close()

    def monitor_frame(self):
        return pd.DataFrame({
            "Company": ["ICBC", "NoDiv Co"],
            "H Ticker": ["1398.HK", "9999.HK"],
            "Sector": ["Financials", "Financials"],
            "H Price (HKD)": [7.0, 5.0],
        })

    def test_trailing_dps_drives_status(self, conn):
        today = db.now_iso()[:10]
        db.insert_dps_events(conn, [
            {"ticker": "1398.HK", "ex_date": today, "amount_hkd": 0.36,
             "source": "yahoo:events"}])
        t = divwatch.universe_yield_table(conn, self.monitor_frame()) \
            .set_index("H Ticker")
        # 0.36 @5% → 7.20 reference; price 7.00 → IN RANGE — REVIEW
        assert t.loc["1398.HK", "Status"] == divwatch.ST_REVIEW
        assert t.loc["1398.HK", "Price for 5% yield (HKD)"] == \
            pytest.approx(7.2)
        assert t.loc["1398.HK", "Current yield (%)"] == \
            pytest.approx(0.36 / 7.0 * 100)

    def test_no_dividend_is_dash_not_fake_signal(self, conn):
        t = divwatch.universe_yield_table(conn, self.monitor_frame()) \
            .set_index("H Ticker")
        assert t.loc["9999.HK", "Status"] == "—"
        assert pd.isna(t.loc["9999.HK", "Trailing 12m DPS (HKD)"])

    def test_manual_overrides_never_leak_to_public_table(self, conn):
        # the owner's normalised DPS is a private judgement: the public
        # table must use the trailing record only
        today = db.now_iso()[:10]
        db.insert_dps_events(conn, [
            {"ticker": "1398.HK", "ex_date": today, "amount_hkd": 0.36,
             "source": "yahoo:events"}])
        db.set_approved_dps(conn, "1398.HK", 9.99, source="owner")
        t = divwatch.universe_yield_table(conn, self.monitor_frame()) \
            .set_index("H Ticker")
        assert t.loc["1398.HK", "Trailing 12m DPS (HKD)"] == \
            pytest.approx(0.36)

    def test_universe_dps_fetch_throttles_when_fresh(self, conn):
        class Boom:
            def fetch_dividends(self, t):
                raise AssertionError("must not fetch while fresh")

        db.set_source_health(conn, divwatch.UNIVERSE_DPS_SOURCE, "ok",
                             "just ran")
        r = divwatch.refresh_universe_dps(conn, yahoo_source=Boom(),
                                          max_age_days=7)
        assert r["skipped"] == "fresh"

    def test_universe_dps_fetch_stores_events(self, conn):
        today = db.now_iso()[:10]

        class Fake:
            def fetch_dividends(self, t):
                if t == "1398.HK":
                    return pd.DataFrame(
                        {"ex_date": [today], "amount_hkd": [0.36]})
                return pd.DataFrame(columns=["ex_date", "amount_hkd"])

        r = divwatch.refresh_universe_dps(conn, yahoo_source=Fake(),
                                          max_age_days=None)
        assert r["stored"] == 1 and r["no_dividends"] == 1
        ev = db.dps_events_df(conn)
        assert len(ev) == 1 and ev.iloc[0]["ticker"] == "1398.HK"
