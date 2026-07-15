"""Deterministic synthetic sample data (Phase 1).

Generates ~5.5 years of daily A/H/FX/premium history for every company in
config/portfolio_sample.csv, a synthetic HSAHP series, and today's 5-minute
intraday ticks for Focus Holdings. Everything is seeded, so tests and the
dashboard are reproducible. All rows are marked quality='sample' and the
source-health panel shows status 'sample' — sample data is never presented
as live.

Run:  python -m ahmon.sample_data
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import calc, config, db

START = "2021-01-04"


def _seed_for(ticker: str) -> int:
    return int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)


def _fx_series(dates: pd.DatetimeIndex) -> pd.Series:
    """HKD per CNY, mean-reverting around ~1.08."""
    rng = np.random.default_rng(20240701)
    n = len(dates)
    fx = np.empty(n)
    fx[0] = 1.10
    for i in range(1, n):
        fx[i] = fx[i - 1] + 0.02 * (1.08 - fx[i - 1]) + rng.normal(0, 0.0015)
    return pd.Series(fx, index=dates)


def _company_history(ticker: str, base_h: float, base_prem: float,
                     dates: pd.DatetimeIndex, fx: pd.Series) -> pd.DataFrame:
    rng = np.random.default_rng(_seed_for(ticker))
    n = len(dates)
    h = np.empty(n)
    prem = np.empty(n)
    h[0] = base_h
    prem[0] = base_prem
    drift = rng.normal(0.0002, 0.0002)
    for i in range(1, n):
        h[i] = max(0.1, h[i - 1] * (1 + drift + rng.normal(0, 0.018)))
        # premium follows a mean-reverting walk around its base level
        prem[i] = prem[i - 1] + 0.03 * (base_prem - prem[i - 1]) \
            + rng.normal(0, 1.2)
    prem = np.clip(prem, -30, 400)
    a = h * (1 + prem / 100.0) / fx.values   # invert the premium formula
    # source-reported premium: ours + small noise; a few odd days exceed the
    # 1pp discrepancy threshold so the flag/alert path is exercised.
    src = prem + rng.normal(0, 0.15, n)
    bad = rng.choice(n, size=max(1, n // 400), replace=False)
    src[bad] += rng.choice([-1, 1], size=len(bad)) * rng.uniform(1.5, 3.0, len(bad))
    return pd.DataFrame({"date": dates, "h_close": h.round(3),
                         "a_close": a.round(3), "fx": fx.values.round(5),
                         "premium_calc": prem.round(3),
                         "premium_src": src.round(3)})


def _hsahp(dates: pd.DatetimeIndex) -> pd.Series:
    rng = np.random.default_rng(20060101)
    n = len(dates)
    v = np.empty(n)
    v[0] = 148.0
    for i in range(1, n):
        v[i] = v[i - 1] + 0.02 * (140.0 - v[i - 1]) + rng.normal(0, 0.9)
    return pd.Series(v.round(2), index=dates, name="HSAHP")


def build(db_path=config.DB_PATH, portfolio_csv=None) -> None:
    portfolio_csv = portfolio_csv or (config.CONFIG_DIR / "portfolio_sample.csv")
    conn = db.connect(db_path)
    port = pd.read_csv(portfolio_csv)

    dates = pd.bdate_range(START, datetime.now(config.TZ).date())
    fx = _fx_series(dates)
    now = db.now_iso()

    rng = np.random.default_rng(7)
    for _, row in port.iterrows():
        cid = db.upsert_company(conn, row["name_en"], row["h_ticker"],
                                row["a_ticker"], row["sector"],
                                row.get("name_zh"))
        db.set_classification(
            conn, cid, row["classification"], source="seed:portfolio_sample.csv",
            owned=int(row.get("owned", 0)),
            weight=None if pd.isna(row.get("weight")) else float(row["weight"]),
            ref_premium_jul2024=None if pd.isna(row.get("ref_premium_jul2024"))
            else float(row["ref_premium_jul2024"]),
            notes=None if pd.isna(row.get("notes")) else str(row["notes"]))

        base_h = float(rng.uniform(2.5, 60))
        base_prem = float(np.clip(rng.normal(55, 40), -10, 220))
        hist = _company_history(row["h_ticker"], base_h, base_prem, dates, fx)
        h_yield = round(float(rng.uniform(1.0, 8.5)), 2)
        a_yield = round(h_yield / (1 + hist["premium_calc"].iloc[-1] / 100), 2)
        db.insert_daily(conn, [{
            "company_id": cid, "date": d.strftime("%Y-%m-%d"),
            "a_close": r.a_close, "h_close": r.h_close, "fx": r.fx,
            "premium_calc": r.premium_calc, "premium_src": r.premium_src,
            "a_div_yield": a_yield, "h_div_yield": h_yield,
            "quality": "sample", "updated_at": now,
        } for d, r in zip(hist["date"], hist.itertuples())])

        # intraday ticks (today, 5-min) for Focus Holdings
        if row["classification"] == config.FOCUS:
            last = hist.iloc[-1]
            t0 = datetime.now(config.TZ).replace(hour=9, minute=30, second=0,
                                                 microsecond=0)
            ticks = []
            h_p, a_p = last.h_close, last.a_close
            r2 = np.random.default_rng(_seed_for(row["h_ticker"]) + 1)
            for k in range(48):
                ts = t0 + timedelta(minutes=5 * k)
                h_p *= 1 + r2.normal(0, 0.0015)
                a_p *= 1 + r2.normal(0, 0.0012)
                pc = calc.a_share_premium(a_p, h_p, last.fx)
                ticks.append({"company_id": cid, "ts": ts.isoformat(),
                              "a_price": round(a_p, 3),
                              "h_price": round(h_p, 3), "fx": last.fx,
                              "premium_calc": round(pc, 3),
                              "premium_src": round(pc + r2.normal(0, 0.1), 3),
                              "a_is_close": 0})
            conn.executemany(
                """INSERT OR REPLACE INTO intraday_obs
                   (company_id, ts, a_price, h_price, fx, premium_calc,
                    premium_src, a_is_close)
                   VALUES (:company_id,:ts,:a_price,:h_price,:fx,
                           :premium_calc,:premium_src,:a_is_close)""", ticks)
            conn.commit()

    hs = _hsahp(pd.bdate_range("2020-07-01", datetime.now(config.TZ).date()))
    db.insert_hsahp(conn, [{"date": d.strftime("%Y-%m-%d"), "close": float(v),
                            "source": "sample", "updated_at": now}
                           for d, v in hs.items()])

    for src in ("akshare (A-H universe & prices)",
                "Yahoo Finance (verification & FX)",
                "HSAHP index series"):
        db.set_source_health(conn, src, "sample",
                             "Phase 1 synthetic data — not a live feed")
    conn.close()


if __name__ == "__main__":
    build()
    print(f"Sample database written to {config.DB_PATH}")
