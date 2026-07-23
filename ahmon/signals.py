"""Buy-level screen: rule-based highlighting of H shares whose premium
setup looks historically attractive, with a plain-language reason for
every flag.

This is a *screen*, not advice: it mechanically applies the thresholds in
config (all owner-tunable) to the same numbers shown in the monitor
table, and every signal spells out which conditions were met and by how
much, so the owner can judge. Logic:

Required (both):
  R1  H upside to 5y median ≥ SIGNAL_MIN_H_UPSIDE_PCT
      — the discount is wide enough vs this company's own history that
        mere reversion to its norm pays meaningfully;
  R2  5y percentile ≥ SIGNAL_MIN_5Y_PERCENTILE
      — the premium is in the top slice of its own 5-year range, i.e.
        the wide discount is rare, not the company's normal state.

Quality (at least SIGNAL_MIN_QUALITY_HITS of three):
  Q1  H dividend yield ≥ SIGNAL_MIN_H_DIV_YIELD   (paid to wait)
  Q2  H P/E (TTM) ≤ SIGNAL_MAX_H_PE               (cheap absolutely too)
  Q3  H market cap ≥ SIGNAL_MIN_H_MKTCAP_HKD      (liquid enough to trade)
"""

from __future__ import annotations

import pandas as pd

from . import config


def _fmt_bn(v: float) -> str:
    return f"{v / 1e9:.0f}bn"


def evaluate_row(r: pd.Series) -> dict | None:
    """Apply the screen to one monitor-table row. Returns None (no signal)
    or {'reasons': [...], 'quality_hits': n, 'upside': x}."""
    upside = r.get("H upside to 5y median (%)")
    pctile = r.get("5y percentile")
    if pd.isna(upside) or pd.isna(pctile):
        return None
    if upside < config.SIGNAL_MIN_H_UPSIDE_PCT \
            or pctile < config.SIGNAL_MIN_5Y_PERCENTILE:
        return None

    reasons = [
        f"the H share is cheaper vs its A share than on {pctile:.0f}% of "
        "days in the last 5 years",
        f"if that gap returns to this company's normal level, the H share "
        f"gains about {upside:+.0f}% (premium today {r['Premium calc (%)']:.0f}%, "
        f"normally {r['5y median (pp)']:.0f}%)",
    ]
    hits = 0
    y = r.get("H div yield (%)")
    if pd.notna(y) and y >= config.SIGNAL_MIN_H_DIV_YIELD:
        hits += 1
        reasons.append(f"pays a {y:.1f}% dividend while you wait")
    pe = r.get("P/E (H)")
    if pd.notna(pe) and 0 < pe <= config.SIGNAL_MAX_H_PE:
        hits += 1
        reasons.append(f"cheap on earnings too (P/E {pe:.1f})")
    mc = r.get("Mkt cap H (HKD bn)")
    if pd.notna(mc) and mc * 1e9 >= config.SIGNAL_MIN_H_MKTCAP_HKD:
        hits += 1
        reasons.append(f"big, easy-to-trade company "
                       f"({_fmt_bn(mc * 1e9)} HKD)")

    if hits < config.SIGNAL_MIN_QUALITY_HITS:
        return None
    return {"reasons": reasons, "quality_hits": hits,
            "upside": float(upside)}


def buy_watch(table: pd.DataFrame) -> pd.DataFrame:
    """The buy-level screen over the whole monitor table, best first.
    Rows whose latest data is stale or sample are excluded — a signal is
    only ever raised on live numbers."""
    rows = []
    for _, r in table.iterrows():
        if r.get("Quality") not in ("live", "eod", "ok"):
            continue
        hit = evaluate_row(r)
        if hit is None:
            continue
        rows.append({
            "Company": r["Company"], "Name (ZH)": r["Name (ZH)"],
            "H Ticker": r["H Ticker"], "A Ticker": r["A Ticker"],
            "Classification": r["Classification"],
            "H Price (HKD)": r["H Price (HKD)"],
            "A Price (CNY)": r.get("A Price (CNY)"),
            "FX (HKD per 1 CNY)": r.get("HKD/CNY"),
            "Premium calc (%)": r["Premium calc (%)"],
            "5y median (pp)": r["5y median (pp)"],
            "H upside to 5y median (%)": round(hit["upside"], 1),
            "5y percentile": r["5y percentile"],
            "H div yield (%)": r["H div yield (%)"],
            "P/E (H)": r["P/E (H)"],
            "Mkt cap H (HKD bn)": r["Mkt cap H (HKD bn)"],
            "Why flagged": "; ".join(hit["reasons"]),
        })
    if not rows:
        return pd.DataFrame(columns=[
            "Company", "Name (ZH)", "H Ticker", "A Ticker",
            "Classification", "H Price (HKD)", "A Price (CNY)",
            "FX (HKD per 1 CNY)", "Premium calc (%)",
            "5y median (pp)", "H upside to 5y median (%)", "5y percentile",
            "H div yield (%)", "P/E (H)", "Mkt cap H (HKD bn)",
            "Why flagged"])
    return pd.DataFrame(rows).sort_values(
        "H upside to 5y median (%)", ascending=False).reset_index(drop=True)


def log_signals(conn, watch: pd.DataFrame, dedupe_date: str) -> int:
    """Write one info alert per flagged company per day (deduped on
    rule+company+date so the scheduler can run this every few minutes)."""
    from . import db
    fired = 0
    for _, r in watch.iterrows():
        if db.alert_exists_on_day(conn, "buy_level", str(r["Company"]),
                                  dedupe_date):
            continue
        db.log_alert(conn, "buy_level",
                     f"[{r['H Ticker']}] {r['Why flagged']} — rule-based "
                     "screen, not advice", str(r["Company"]), "info")
        fired += 1
    return fired
