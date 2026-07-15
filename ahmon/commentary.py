"""Template-based daily commentary generated purely from calculated data.
An LLM narrative layer may be added later, but only on top of these verified
numbers — never as a replacement for them."""

from __future__ import annotations

import pandas as pd

from . import config


def _fmt_names(names: list[str]) -> str:
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def daily_commentary(table: pd.DataFrame, attribution: pd.DataFrame) -> str:
    focus = table[table["Classification"] == config.FOCUS].dropna(
        subset=["Δ1d (pp)"])
    rest = table[table["Classification"] != config.FOCUS].dropna(
        subset=["Δ1d (pp)"])
    parts = []

    if not focus.empty:
        med = focus["Δ1d (pp)"].median()
        direction = ("narrowed" if med < -0.05 else
                     "widened" if med > 0.05 else "was broadly unchanged")
        top = focus.reindex(focus["Δ1d (pp)"].sort_values().index).head(3)
        drivers = attribution[attribution["Company"].isin(top["Company"])]
        h_driven = drivers["Driver"].str.contains("H-share").sum() if not drivers.empty else 0
        cause = ("mainly because their H shares outperformed their "
                 "corresponding A shares" if h_driven >= 2 else
                 "driven by a mix of A-share and H-share moves")
        parts.append(
            f"**Focus Holdings:** The median premium among focus holdings "
            f"{direction} by {abs(med):.1f} percentage points today. "
            f"The largest narrowing occurred in "
            f"{_fmt_names(list(top['Company']))}, {cause}.")

    if not rest.empty:
        med = rest["Δ1d (pp)"].median()
        direction = ("narrowed" if med < -0.05 else
                     "widened" if med > 0.05 else "was broadly unchanged")
        big = rest[rest["Δ1d (pp)"].abs() > 5]
        flag = (f" {len(big)} non-focus companies recorded premium movements "
                f"of more than five percentage points and were flagged for "
                f"review ({_fmt_names(list(big['Company'].head(5)))})."
                if not big.empty else "")
        parts.append(
            f"**Broader A–H Market:** Across the rest of the A–H universe, "
            f"the median premium {direction}"
            + (f" by {abs(med):.1f} percentage points" if abs(med) > 0.05 else "")
            + f".{flag}")

    warn = table[table["Quality"].isin(["stale", "failed"])]
    if not warn.empty:
        parts.append(f"**Data quality:** {len(warn)} companies have stale or "
                     f"failed data and their figures above should be treated "
                     f"with caution: {_fmt_names(list(warn['Company'].head(5)))}.")
    sample = (table["Quality"] == "sample").all()
    if sample:
        parts.append("_All figures are Phase 1 synthetic sample data._")
    return "\n\n".join(parts) if parts else "No data available for commentary."


def closing_summary(table: pd.DataFrame, attribution: pd.DataFrame,
                    sector_stats: pd.DataFrame) -> str:
    """Short factual closing summary across the full universe."""
    t = table.dropna(subset=["Δ1d (pp)"])
    if t.empty:
        return "No observations today."
    med = t["Δ1d (pp)"].median()
    overall = ("narrowed" if med < -0.05 else
               "widened" if med > 0.05 else "was broadly unchanged")
    sect = sector_stats.dropna(subset=["Δ1m median (pp)"])
    lead = sect.reindex(sect["Δ1m median (pp)"].abs()
                        .sort_values(ascending=False).index).head(2)
    movers = t.reindex(t["Δ1d (pp)"].abs()
                       .sort_values(ascending=False).index).head(3)
    mv = "; ".join(f"{r['Company']} {r['Δ1d (pp)']:+.1f}pp"
                   for _, r in movers.iterrows())
    att = attribution.head(3)
    h_share = att["Driver"].str.contains("H-share").mean() if not att.empty else 0
    driver_line = ("H-share price action drove most of the largest moves."
                   if h_share > 0.5 else
                   "A-share price action (or FX) drove most of the largest moves.")
    return (f"The overall A-share premium {overall} today "
            f"(median {med:+.1f}pp). Sector leadership: "
            f"{_fmt_names(list(lead['Sector']))}. "
            f"Largest individual changes: {mv}. {driver_line}")
