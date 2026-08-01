"""Template-based daily commentary generated purely from calculated data,
written in the owner's H-buyer convention: every figure is the H-share
discount to the A twin (discount = premium ÷ (100 + premium) × 100).
'Narrowed' = discounts shrank = H shares caught up (convergence an H
holder benefits from); 'widened' = H shares got relatively cheaper.
An LLM narrative layer may be added later, but only on top of these
verified numbers — never as a replacement for them."""

from __future__ import annotations

import pandas as pd

from . import config, metrics

# Review-flag thresholds in DISCOUNT points. Discount space is
# compressed vs premium space: for a mid-range name (premium ≈ 60%,
# discount ≈ 37.5%) a 5pp premium move ≈ 2pp of discount and a 10pp
# move ≈ 4pp — these keep the flags at the same real-world sensitivity
# the old premium-point thresholds (5/10) had.
FLAG_PP = {"1d": 2.0, "1w": 2.0, "1m": 4.0}


def _fmt_names(names: list[str]) -> str:
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


_DRIVER_PHRASE = {
    "H-share outperformance": "H-share strength",
    "H-share underperformance": "H-share weakness",
    "A-share outperformance": "A-share strength",
    "A-share underperformance": "A-share weakness",
    "CNY/HKD movement": "currency moves",
    "Combination of factors": "a mix of factors",
}


def _cause_sentence(att: pd.DataFrame, companies,
                    min_move_pp: float = 1.0) -> str:
    """One sentence naming what drove the window's moves — counted from
    the arithmetic attribution decomposition, never inferred."""
    if att is None or att.empty:
        return ""
    sub = att[att["Company"].isin(set(companies))]
    sub = sub[sub["Premium move (pp)"].abs() >= min_move_pp]
    if sub.empty:
        return ""
    counts = sub["Driver"].map(_DRIVER_PHRASE).value_counts()
    bits = [f"{phrase} ({n} name{'s' if n > 1 else ''})"
            for phrase, n in counts.head(3).items()]
    return (" Cause of the larger moves, by attribution: "
            + ", ".join(bits) + ".")


def _discount_delta(t: pd.DataFrame, col: str) -> pd.Series:
    """Per-company change of the H discount over the window whose
    premium change is stored in `col` (result in discount points)."""
    now = t["Premium calc (%)"]
    return (metrics.premium_to_discount(now)
            - metrics.premium_to_discount(now - t[col]))


def _direction(med: float) -> str:
    return ("narrowed" if med < -0.05 else
            "widened" if med > 0.05 else "was broadly unchanged")


def daily_commentary(table: pd.DataFrame, attribution: pd.DataFrame) -> str:
    focus = table[table["Classification"] == config.FOCUS].dropna(
        subset=["Δ1d (pp)"]).copy()
    rest = table[table["Classification"] != config.FOCUS].dropna(
        subset=["Δ1d (pp)"]).copy()
    parts = []

    if not focus.empty:
        focus["dd"] = _discount_delta(focus, "Δ1d (pp)")
        med = focus["dd"].median()
        top = focus.sort_values("dd").head(3)      # deepest narrowing
        drivers = attribution[attribution["Company"].isin(top["Company"])]
        h_driven = drivers["Driver"].str.contains("H-share").sum() \
            if not drivers.empty else 0
        cause = ("mainly because their H shares outperformed their "
                 "corresponding A shares" if h_driven >= 2 else
                 "driven by a mix of A-share and H-share moves")
        amount = (f" by {abs(med):.1f} percentage points"
                  if abs(med) > 0.05 else "")
        parts.append(
            f"**Focus Holdings:** The median H-share discount among focus "
            f"holdings {_direction(med)}{amount} today. "
            f"The discounts that narrowed most were "
            f"{_fmt_names(list(top['Company']))}, {cause}.")

    if not rest.empty:
        rest["dd"] = _discount_delta(rest, "Δ1d (pp)")
        med = rest["dd"].median()
        big = rest[rest["dd"].abs() > FLAG_PP["1d"]]
        flag = (f" {len(big)} non-focus companies recorded discount "
                f"movements of more than {FLAG_PP['1d']:.0f} percentage "
                f"points and were flagged for review "
                f"({_fmt_names(list(big['Company'].head(5)))})."
                if not big.empty else "")
        parts.append(
            f"**Broader A–H Market:** Across the rest of the A–H "
            f"universe, the median H-share discount {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points"
               if abs(med) > 0.05 else "")
            + f".{flag}"
            + _cause_sentence(attribution, rest["Company"]))

    warn = table[table["Quality"].isin(["stale", "failed"])]
    if not warn.empty:
        parts.append(f"**Data quality:** {len(warn)} companies have stale or "
                     f"failed data and their figures above should be treated "
                     f"with caution: {_fmt_names(list(warn['Company'].head(5)))}.")
    sample = (table["Quality"] == "sample").all()
    if sample:
        parts.append("_All figures are Phase 1 synthetic sample data._")
    return "\n\n".join(parts) if parts else "No data available for commentary."


def period_commentary(table: pd.DataFrame, period: str,
                      attribution: pd.DataFrame | None = None) -> str:
    """Weekly ('1w') or monthly ('1m') commentary from stored changes.
    Same Focus-first structure as the daily note, over a longer window.
    `attribution` should be the decomposition computed over the SAME
    window (metrics.attribution_over), so each paragraph can state what
    caused its moves."""
    col = {"1w": "Δ1w (pp)", "1m": "Δ1m (pp)"}[period]
    label = {"1w": "week", "1m": "month"}[period]
    focus = table[table["Classification"] == config.FOCUS].dropna(
        subset=[col]).copy()
    rest = table[table["Classification"] != config.FOCUS].dropna(
        subset=[col]).copy()
    parts = []

    if not focus.empty:
        focus["dd"] = _discount_delta(focus, col)
        med = focus["dd"].median()
        nar = focus.sort_values("dd").head(3)
        wid = focus.sort_values("dd", ascending=False).head(3)
        amount = (f" by {abs(med):.1f} percentage points"
                  if abs(med) > 0.05 else "")
        parts.append(
            f"**Focus Holdings ({label}):** The median H-share discount "
            f"among focus holdings {_direction(med)}{amount} over "
            f"the past {label}. Discounts that narrowed most "
            f"(H catching up): {_fmt_names(list(nar['Company']))} "
            f"({', '.join(f'{v:+.1f}pp' for v in nar['dd'])}). "
            f"Widened most (H relatively cheaper): "
            f"{_fmt_names(list(wid['Company']))} "
            f"({', '.join(f'{v:+.1f}pp' for v in wid['dd'])})."
            + _cause_sentence(attribution, focus["Company"]))

    if not rest.empty:
        rest["dd"] = _discount_delta(rest, col)
        med = rest["dd"].median()
        thr = FLAG_PP[period]
        big = rest[rest["dd"].abs() > thr]
        noun = "company" if len(big) == 1 else "companies"
        flag = (f" {len(big)} {noun} moved more than {thr:.0f}pp of "
                f"discount and {'was' if len(big) == 1 else 'were'} "
                f"flagged for review "
                f"({_fmt_names(list(big['Company'].head(5)))})."
                if not big.empty else "")
        parts.append(
            f"**Broader A–H Market ({label}):** Across the rest of the "
            f"universe, the median H-share discount {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points" if abs(med) > 0.05
               else "") + f".{flag}"
            + _cause_sentence(attribution, rest["Company"],
                              min_move_pp=2.0))

    hi = table.dropna(subset=["52w percentile"])
    ext = hi[(hi["52w percentile"] >= 98) | (hi["52w percentile"] <= 2)]
    if not ext.empty:
        parts.append(f"**52-week extremes:** "
                     f"{_fmt_names(list(ext['Company'].head(6)))} are at or "
                     f"near 52-week discount extremes (the percentile is "
                     f"identical in premium or discount terms).")
    if (table["Quality"] == "sample").all():
        parts.append("_All figures are Phase 1 synthetic sample data._")
    return "\n\n".join(parts) if parts else "Not enough history yet."


def closing_summary(table: pd.DataFrame, attribution: pd.DataFrame,
                    sector_stats: pd.DataFrame) -> str:
    """Short factual closing summary across the full universe."""
    t = table.dropna(subset=["Δ1d (pp)"]).copy()
    if t.empty:
        return "No observations today."
    t["dd"] = _discount_delta(t, "Δ1d (pp)")
    med = t["dd"].median()
    sect = sector_stats.dropna(subset=["Δ1m median (pp)"])
    lead = sect.reindex(sect["Δ1m median (pp)"].abs()
                        .sort_values(ascending=False).index).head(2)
    movers = t.reindex(t["dd"].abs()
                       .sort_values(ascending=False).index).head(3)
    mv = "; ".join(f"{r['Company']} {r['dd']:+.1f}pp"
                   for _, r in movers.iterrows())
    att = attribution.head(3)
    h_share = att["Driver"].str.contains("H-share").mean() \
        if not att.empty else 0
    driver_line = ("H-share price action drove most of the largest moves."
                   if h_share > 0.5 else
                   "A-share price action (or FX) drove most of the largest moves.")
    return (f"The typical H-share discount {_direction(med)} today "
            f"(median {med:+.1f}pp; negative = H catching up). "
            f"Sector leadership: {_fmt_names(list(lead['Sector']))}. "
            f"Largest individual discount changes: {mv}. {driver_line}")
