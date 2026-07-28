"""Phase 2 — live source layer, offline. Network calls are stubbed; these
tests pin the ticker conversions, the premium-convention guard, and the
refresh pipeline's data rules (independent calculation, idempotent upserts,
sample-data protection, health/alert bookkeeping)."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import calc, config, db, refresh
from ahmon.sources import ConventionError, tickers
from ahmon.sources.akshare_src import verify_premium_convention

FX = 1.10


# ------------------------------------------------------------------ tickers

class TestTickerConversions:
    def test_em_h_to_db(self):
        assert tickers.h_from_em("00939") == "939.HK"
        assert tickers.h_from_em("09980") == "9980.HK"

    def test_db_h_to_em_round_trip(self):
        for em in ("00939", "02628", "09980"):
            assert tickers.em_from_h(tickers.h_from_em(em)) == em

    def test_em_a_to_db_exchange_suffix(self):
        assert tickers.a_from_em("601939") == "601939.SS"   # Shanghai
        assert tickers.a_from_em("688796") == "688796.SS"   # STAR
        assert tickers.a_from_em("000776") == "000776.SZ"   # Shenzhen
        assert tickers.a_from_em("300638") == "300638.SZ"   # ChiNext

    def test_unknown_a_prefix_rejected(self):
        with pytest.raises(ValueError):
            tickers.a_from_em("870001")

    def test_yahoo_h_symbol_padded(self):
        assert tickers.yahoo_h_symbol("939.HK") == "0939.HK"
        assert tickers.yahoo_h_symbol("2628.HK") == "2628.HK"


# --------------------------------------------------------------- convention

def em_frame(a, h, reported):
    return pd.DataFrame({"a_price_cny": a, "h_price_hkd": h,
                         "premium_src_raw": reported})


class TestConventionGuard:
    def test_a_convention_detected_and_passed_through(self):
        a, h = [10.0, 50.0, 3.0], [8.0, 30.0, 4.0]
        rep = [calc.a_share_premium(x, y, FX) + 0.05 for x, y in zip(a, h)]
        prem, conv = verify_premium_convention(em_frame(a, h, rep), FX)
        assert conv == "a_premium"
        assert prem.tolist() == pytest.approx(rep)

    def test_h_convention_detected_and_converted(self):
        a, h = [10.0, 50.0, 3.0], [8.0, 30.0, 4.0]
        ours = [calc.a_share_premium(x, y, FX) for x, y in zip(a, h)]
        # source reports the AASTOCKS-style H premium instead
        rep = [(1.0 / (1.0 + p / 100.0) - 1.0) * 100.0 for p in ours]
        prem, conv = verify_premium_convention(em_frame(a, h, rep), FX)
        assert conv == "h_premium"
        assert prem.tolist() == pytest.approx(ours, abs=1e-9)

    def test_neither_convention_raises(self):
        a, h = [10.0, 50.0, 3.0], [8.0, 30.0, 4.0]
        with pytest.raises(ConventionError):
            verify_premium_convention(em_frame(a, h, [500.0, -90.0, 250.0]),
                                      FX)


# ------------------------------------------------------------------ refresh

class FakeAkshare:
    """Stands in for AkshareSource: two companies, one new to the DB."""
    name = "akshare (A-H universe & prices)"

    def __init__(self):
        base = pd.DataFrame({
            "name_zh": ["建设银行", "新来的", "另一家"],
            "h_ticker": ["939.HK", "6127.HK", "2318.HK"],
            "a_ticker": ["601939.SS", "603127.SS", "601318.SS"],
            "h_price_hkd": [8.0, 30.0, 5.5],
            "a_price_cny": [10.0, 50.0, 5.0],
        })
        base["premium_src_raw"] = base.apply(
            lambda r: calc.a_share_premium(r["a_price_cny"],
                                           r["h_price_hkd"], FX), axis=1)
        # one deliberate >1pp source discrepancy on the second company
        base.loc[1, "premium_src_raw"] += 2.5
        self.table = base

    def fetch_universe(self):
        return self.table[["name_zh", "h_ticker", "a_ticker"]]

    def fetch_quotes(self, h_tickers=None, hkd_per_cny=None):
        df = self.table.copy()
        prem, conv = verify_premium_convention(df, hkd_per_cny)
        df["premium_src"] = prem
        df["fx_implied"] = ((1 + df["premium_src"] / 100)
                            * df["h_price_hkd"] / df["a_price_cny"])
        df.attrs["convention"] = conv
        df.attrs["dropped_rows"] = 0
        return df


class FakeHsahp:
    name = "HSAHP index series"

    def fetch_history(self, beg="0"):
        idx = pd.to_datetime(["2026-07-14", "2026-07-15"])
        s = pd.Series([124.5, 125.04], index=idx, name="HSAHP")
        return s if beg == "0" else s[s.index >= pd.Timestamp(beg)]

    def fetch_spot(self):
        return {"level": 123.4, "prev_close": 125.04}


class FakeYahoo:
    name = "Yahoo Finance (verification & FX)"

    def fetch_fx(self):
        return {"hkd_per_cny": FX,
                "asof": pd.Timestamp("2026-07-15 15:00", tz=config.TZ)}

    def fetch_quotes(self, h_tickers, a_tickers=None):
        ts = pd.Timestamp("2026-07-15 15:00", tz=config.TZ)
        rows = []
        prices = {"939.HK": (8.0, 10.0), "6127.HK": (30.0, 50.0),
                  "2318.HK": (5.5, 5.0)}
        for h, a in zip(h_tickers, a_tickers or [None] * len(h_tickers)):
            hp, ap = prices[h]
            rows.append({"h_ticker": h, "a_ticker": a,
                         "h_price_hkd": hp,
                         "a_price_cny": None if a is None else ap,
                         "h_asof": ts, "a_asof": ts, "error": None})
        return pd.DataFrame(rows)


@pytest.fixture
def live_conn(tmp_path):
    conn = db.connect(tmp_path / "live.db")
    cid = db.upsert_company(conn, "China Construction Bank", "939.HK",
                            "601939.SS", "Financials", "建设银行")
    db.set_classification(conn, cid, config.FOCUS, source="test")
    yield conn
    conn.close()


class TestRefreshLive:
    def run(self, conn):
        return refresh.refresh_live(conn, ak_source=FakeAkshare(),
                                    yahoo_source=FakeYahoo(),
                                    hsahp_source=FakeHsahp())

    def test_upserts_live_rows_with_independent_premium(self, live_conn):
        s = self.run(live_conn)
        assert s["upserted"] == 3
        assert s["obs_date"] == "2026-07-15"
        obs = db.daily_df(live_conn)
        assert (obs["quality"] == "live").all()
        ccb = obs[obs["company_id"] == 1].iloc[0]
        assert ccb["premium_calc"] == pytest.approx(
            calc.a_share_premium(10.0, 8.0, FX))

    def test_source_discrepancy_flagged_not_copied(self, live_conn):
        s = self.run(live_conn)
        assert s["calc_vs_source_flags"] == 1
        rules = db.alerts_df(live_conn)["rule"].tolist()
        assert "calc_vs_source" in rules
        obs = db.daily_df(live_conn).set_index("company_id")
        comps = db.companies_df(live_conn).set_index("h_ticker")
        new = obs.loc[int(comps.loc["6127.HK", "id"])]
        # calc stays ours; the biased source figure is stored separately
        assert abs(new["premium_calc"] - new["premium_src"]) > 1.0

    def test_universe_sync_adds_and_audits(self, live_conn):
        self.run(live_conn)
        comps = db.companies_df(live_conn).set_index("h_ticker")
        assert comps.loc["6127.HK", "classification"] == config.OTHER
        # a new company known to config/sector_map.csv gets its sector at
        # once (6127.HK = Joinn Laboratories → Healthcare); only tickers
        # absent from the map stay visibly Unclassified
        assert comps.loc["6127.HK", "sector"] == "Healthcare"
        # the feed never renames existing companies
        assert comps.loc["939.HK", "name_en"] == "China Construction Bank"
        n = live_conn.execute("SELECT COUNT(*) FROM constituent_log "
                              "WHERE action='added'").fetchone()[0]
        assert n == 2

    def test_second_run_is_idempotent(self, live_conn):
        self.run(live_conn)
        self.run(live_conn)
        obs = db.daily_df(live_conn)
        assert len(obs) == 3          # still one row per company per day
        n = live_conn.execute("SELECT COUNT(*) FROM constituent_log "
                              "WHERE action='added'").fetchone()[0]
        assert n == 2                 # companies only logged as added once

    def test_health_marked_live(self, live_conn):
        self.run(live_conn)
        health = db.source_health_df(live_conn).set_index("source")
        assert health.loc[FakeAkshare.name, "status"] == "ok"
        assert health.loc[FakeYahoo.name, "status"] == "ok"
        assert health.loc[FakeAkshare.name, "message"].startswith("live:")

    def test_refuses_to_touch_sample_data(self, live_conn):
        db.insert_daily(live_conn, [{
            "company_id": 1, "date": "2026-07-14", "a_close": 1, "h_close": 1,
            "fx": FX, "premium_calc": 0, "premium_src": 0,
            "a_div_yield": None, "h_div_yield": None,
            "quality": "sample", "updated_at": db.now_iso()}])
        with pytest.raises(RuntimeError, match="sample"):
            self.run(live_conn)
        assert (db.daily_df(live_conn)["quality"] == "sample").all()

    def test_hsahp_series_stored_with_source_label(self, live_conn):
        s = self.run(live_conn)
        assert s["hsahp"]["upserted"] == 2
        assert s["hsahp"]["full_backfill"] is True
        hs = db.hsahp_series(live_conn)
        assert len(hs) == 2 and hs.iloc[-1] == pytest.approx(125.04)
        src = live_conn.execute(
            "SELECT DISTINCT source FROM hsahp_daily").fetchall()
        assert [r[0] for r in src] == ["eastmoney_mirror(100.HSAHP)"]
        health = db.source_health_df(live_conn).set_index("source")
        assert health.loc["HSAHP index series", "status"] == "ok"

    def test_hsahp_second_run_incremental_and_idempotent(self, live_conn):
        self.run(live_conn)
        s = self.run(live_conn)
        assert s["hsahp"]["full_backfill"] is False
        assert len(db.hsahp_series(live_conn)) == 2   # no duplicates

    def test_hsahp_failure_is_nonfatal_and_recorded(self, live_conn):
        class BrokenHsahp(FakeHsahp):
            def fetch_history(self, beg="0"):
                raise RuntimeError("mirror down")
        s = refresh.refresh_live(live_conn, ak_source=FakeAkshare(),
                                 yahoo_source=FakeYahoo(),
                                 hsahp_source=BrokenHsahp())
        assert s["upserted"] == 3          # equity refresh unaffected
        assert s["hsahp"] is None
        health = db.source_health_df(live_conn).set_index("source")
        assert health.loc["HSAHP index series", "status"] == "failed"
        assert "hsahp_fetch" in db.alerts_df(live_conn)["rule"].tolist()

    def test_fx_divergence_degrades_source(self, live_conn):
        class SkewedYahoo(FakeYahoo):
            def fetch_fx(self):
                info = super().fetch_fx()
                info["hkd_per_cny"] = FX * 1.01   # 1% off EM-implied
                return info
        s = refresh.refresh_live(live_conn, ak_source=FakeAkshare(),
                                 yahoo_source=SkewedYahoo(),
                                 hsahp_source=FakeHsahp())
        assert s["fx_divergence_pct"] > config.ALERT_FX_DIVERGENCE_PCT
        health = db.source_health_df(live_conn).set_index("source")
        assert health.loc[FakeAkshare.name, "status"] == "degraded"
        assert "fx_divergence" in db.alerts_df(live_conn)["rule"].tolist()
