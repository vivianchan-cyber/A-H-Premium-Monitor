"""Assembles the per-company monitor table, rankings and sector stats
from stored observations. Pure reads + arithmetic — no network access."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from . import calc, config, db


def _series(g: pd.DataFrame, col: str) -> pd.Series:
    return pd.Series(g[col].values, index=pd.DatetimeIndex(g["date"]))


def monitor_table(conn) -> pd.DataFrame:
    """One row per company with every field the Stock Monitor displays."""
    comps = db.companies_df(conn)
    obs = db.daily_df(conn)
    stats = db.stats_df(conn)
    stats = stats.set_index("company_id") if not stats.empty else None
    rows = []
    now = datetime.now(config.TZ)
    for _, c in comps.iterrows():
        g = obs[obs["company_id"] == c["id"]]
        if g.empty:
            continue
        prem = _series(g, "premium_calc")
        h = _series(g, "h_close")
        a = _series(g, "a_close")
        last = g.iloc[-1]
        ch = calc.series_changes(prem)
        r3 = calc.rolling_stats(prem, config.WINDOW_3Y)
        r5 = calc.rolling_stats(prem, config.WINDOW_5Y)
        r52 = calc.rolling_stats(prem, config.WINDOW_1Y)
        st = (stats.loc[c["id"]].to_dict()
              if stats is not None and c["id"] in stats.index else {})
        updated = pd.to_datetime(last["updated_at"])
        stale_after = config.STALE_MINUTES.get(c["classification"], 60)
        is_sample = last["quality"] == "sample"
        if is_sample:
            quality = "sample"
        elif (now - updated).total_seconds() / 60 > stale_after:
            quality = "stale"
        else:
            quality = last["quality"]
        rows.append({
            "Company": c["name_en"], "Name (ZH)": c["name_zh"],
            "Classification": c["classification"] or config.OTHER,
            "Sector": c["sector"],
            "H Ticker": c["h_ticker"], "A Ticker": c["a_ticker"],
            "H Price (HKD)": last["h_close"], "A Price (CNY)": last["a_close"],
            "HKD/CNY": last["fx"],
            "Premium calc (%)": last["premium_calc"],
            "Premium src (%)": last["premium_src"],
            "Calc-src diff (pp)":
                None if last["premium_src"] is None
                else last["premium_calc"] - last["premium_src"],
            "Δ1d (pp)": ch["1d"], "Δ1w (pp)": ch["1w"], "Δ1m (pp)": ch["1m"],
            "Δ3m (pp)": ch["3m"], "ΔYTD (pp)": ch["ytd"], "Δ1y (pp)": ch["1y"],
            "H 1d ret (%)": calc.pct_return(h),
            "A 1d ret (%)": calc.pct_return(a),
            "H div yield (%)": (last["h_div_yield"]
                                if last["h_div_yield"] is not None
                                else st.get("h_div_yield")),
            "A div yield (%)": (last["a_div_yield"]
                                if last["a_div_yield"] is not None
                                else st.get("a_div_yield")),
            "Mkt cap H (HKD bn)":
                None if st.get("h_mktcap_hkd") is None
                else st["h_mktcap_hkd"] / 1e9,
            "Mkt cap A (CNY bn)":
                None if st.get("a_mktcap_cny") is None
                else st["a_mktcap_cny"] / 1e9,
            "P/E (H)": st.get("h_pe"), "P/E (A)": st.get("a_pe"),
            "P/B (H)": st.get("h_pb"), "P/B (A)": st.get("a_pb"),
            "3y median (pp)": r3["median"], "5y median (pp)": r5["median"],
            "Dist from 3y median (pp)":
                None if r3["median"] is None
                else last["premium_calc"] - r3["median"],
            "Dist from 5y median (pp)":
                None if r5["median"] is None
                else last["premium_calc"] - r5["median"],
            # H-buyer framing: the H share trades at a discount to its A
            # twin; closing the gap entirely would return the premium
            # itself. The realistic anchor is the company's own 5y median:
            # H upside = (1+p_today)/(1+p_median) - 1 with A price and FX
            # held constant.
            "H discount to A (%)":
                (1.0 - 1.0 / (1.0 + last["premium_calc"] / 100.0)) * 100.0,
            "H upside to 5y median (%)":
                None if r5["median"] is None
                else ((1.0 + last["premium_calc"] / 100.0)
                      / (1.0 + r5["median"] / 100.0) - 1.0) * 100.0,
            "5y percentile": r5["percentile"],
            "52w percentile": r52["percentile"],
            "52w high (pp)": r52["high"], "52w low (pp)": r52["low"],
            "Premium z (1y)": calc.zscore(prem),
            "Updated": last["updated_at"], "Quality": quality,
            "company_id": c["id"],
        })
    df = pd.DataFrame(rows)
    # With a single day of live history every change/percentile field is
    # None; coerce numeric columns to float (None -> NaN) so downstream
    # arithmetic, sorting and .abs() work instead of TypeError-ing on
    # object dtype.
    text_cols = {"Company", "Name (ZH)", "Classification", "Sector",
                 "H Ticker", "A Ticker", "Updated", "Quality"}
    for c in df.columns:
        if c not in text_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ------------------------------------------------------------------ rankings

RANKINGS = {
    "Largest H upside if premium reverts to its 5y median":
        ("H upside to 5y median (%)", False),
    "Deepest H discount to A (widest premium)": ("Premium calc (%)", False),
    "Smallest H discount / H above A": ("Premium calc (%)", True),
    "Premium above its own 5y norm (extra reversion upside)":
        ("Dist from 5y median (pp)", False),
    "Premium below its own 5y norm (convergence already played out)":
        ("Dist from 5y median (pp)", True),
    "Premium near 5y floor (lowest 5y percentile)": ("5y percentile", True),
    "Fastest 1-day narrowing": ("Δ1d (pp)", True),
    "Fastest 1-month narrowing": ("Δ1m (pp)", True),
    "Fastest 1-month widening": ("Δ1m (pp)", False),
    "Largest deviation from 3y median": ("Dist from 3y median (pp)", None),
    "H-share outperformance vs A (1d)": ("H-A 1d spread (%)", False),
}


def rankings(table: pd.DataFrame, key: str, n: int = 10) -> pd.DataFrame:
    t = table.copy()
    t["H-A 1d spread (%)"] = t["H 1d ret (%)"] - t["A 1d ret (%)"]
    col, ascending = RANKINGS[key]
    if ascending is None:                       # by absolute deviation
        t = t.reindex(t[col].abs().sort_values(ascending=False).index)
    else:
        t = t.sort_values(col, ascending=ascending)
    cols = ["Company", "Name (ZH)", "H Ticker", "A Ticker", "Classification",
            "Premium calc (%)", "H discount to A (%)", "5y median (pp)",
            "Dist from 5y median (pp)", "H upside to 5y median (%)",
            "5y percentile", "52w percentile", "Δ1m (pp)",
            "H div yield (%)", "P/E (H)", col]
    return t[list(dict.fromkeys(cols))].head(n).reset_index(drop=True)


def extremes_52w(table: pd.DataFrame) -> pd.DataFrame:
    """Companies at (or within 2% of) their 52-week premium high or low."""
    t = table.dropna(subset=["52w percentile"]).copy()
    t["52w extreme"] = np.select(
        [t["52w percentile"] >= 98, t["52w percentile"] <= 2],
        ["New 52w high", "New 52w low"], default="")
    return t[t["52w extreme"] != ""][
        ["Company", "Name (ZH)", "H Ticker", "A Ticker", "Classification",
         "Premium calc (%)", "52w percentile", "52w high (pp)",
         "52w low (pp)", "52w extreme"]]


# -------------------------------------------------------------------- sector

def sector_stats(conn, table: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sector, g in table.groupby("Sector"):
        weights = g["Premium calc (%)"] * 0 + 1.0   # equal weight fallback
        rows.append({
            "Sector": sector, "Companies": len(g),
            "Median premium (%)": g["Premium calc (%)"].median(),
            "Weighted avg premium (%)":
                np.average(g["Premium calc (%)"], weights=weights),
            "Δ1m median (pp)": g["Δ1m (pp)"].median(),
            "Δ1y median (pp)": g["Δ1y (pp)"].median(),
        })
    return pd.DataFrame(rows).sort_values("Median premium (%)",
                                          ascending=False).reset_index(drop=True)


def sector_history(conn) -> pd.DataFrame:
    """Daily median premium per sector (long format: date, sector, median)."""
    obs = db.daily_df(conn)
    comps = db.companies_df(conn)[["id", "sector"]]
    m = obs.merge(comps, left_on="company_id", right_on="id")
    return (m.groupby(["date", "sector"])["premium_calc"]
             .median().rename("median_premium").reset_index())


# --------------------------------------------------------------- attribution

def attribution_table(conn, table: pd.DataFrame) -> pd.DataFrame:
    obs = db.daily_df(conn)
    rows = []
    for _, r in table.iterrows():
        g = obs[obs["company_id"] == r["company_id"]].tail(2)
        if len(g) < 2:
            continue
        p, q = g.iloc[0], g.iloc[1]
        att = calc.attribute_premium_move(p["a_close"], q["a_close"],
                                          p["h_close"], q["h_close"],
                                          p["fx"], q["fx"])
        rows.append({
            "Company": r["Company"], "Classification": r["Classification"],
            "Premium move (pp)": round(att.premium_move_pp, 2),
            "Driver": att.driver,
            "A contribution (pp)": round(att.a_contrib_pp, 2),
            "H contribution (pp)": round(att.h_contrib_pp, 2),
            "FX contribution (pp)": round(att.fx_contrib_pp, 2),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.reindex(df["Premium move (pp)"].abs()
                        .sort_values(ascending=False).index).reset_index(drop=True)
    return df
