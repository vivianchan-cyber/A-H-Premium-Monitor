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
FLAG_PP = {"1d": 2.0, "1w": 2.0, "1m": 4.0, "1y": 8.0}


def _fmt_names(names: list[str]) -> str:
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


# The attribution decomposition only knows which price leg moved — the
# H share, the A share, or both — never the economic reason behind the
# move. The driver sentences below therefore describe the dominant
# price leg and nothing more, in plain prose (full breakdowns stay on
# the Attribution tab).
_DRIVER_CAT = {
    "H-share underperformance": "h_weak",
    "H-share outperformance": "h_strong",
    "A-share outperformance": "a_strong",
    "A-share underperformance": "a_weak",
    "Combination of factors": "both",
    "CNY/HKD movement": "fx",
}
# leg categories: (direction of the discount, performance phrase)
_LEG_WORDS = {
    "h_weak": ("widened", "weaker H-share performance"),
    "h_strong": ("narrowed", "stronger H-share performance"),
    "a_strong": ("widened", "stronger A-share performance"),
    "a_weak": ("narrowed", "weaker A-share performance"),
}

_CONTRIB_COLS = {"a": "A contribution (pp)", "h": "H contribution (pp)",
                 "fx": "FX contribution (pp)"}


def _dominant_leg(att_rows: pd.DataFrame | None) -> tuple[str, float] | None:
    """('a'|'h'|'fx', summed pp) for the leg with the largest total
    contribution across these rows — both leg columns are oriented so
    positive pushes the premium (and hence the discount) up. None when
    the rows carry no contribution columns or the sums are all ~zero."""
    if att_rows is None or att_rows.empty or \
            not all(c in att_rows.columns for c in _CONTRIB_COLS.values()):
        return None
    sums = {leg: att_rows[col].sum(skipna=True)
            for leg, col in _CONTRIB_COLS.items()}
    leg = max(sums, key=lambda k: abs(sums[k]))
    if pd.isna(sums[leg]) or abs(sums[leg]) < 1e-9:
        return None
    return leg, sums[leg]


def _driver_sentence(att: pd.DataFrame, companies, style: str = "week",
                     min_move_pp: float = 1.0) -> str:
    """One concise sentence naming the main price-leg driver of the
    window's larger moves: the attribution category with the highest
    count of names. A top count of "Combination of factors" is resolved
    to the leg with the largest summed contribution — the sentence must
    name a leg, never "movements in both". A tie between the top two
    categories is reported as no single driver dominant. Weekly and monthly
    phrasing differ slightly so stored notes don't read as one
    repeated template. Returns '' when attribution is unavailable."""
    if att is None or att.empty:
        return ""
    sub = att[att["Company"].isin(set(companies))]
    sub = sub[sub["Premium move (pp)"].abs() >= min_move_pp]
    if sub.empty:
        return ""
    counts = sub["Driver"].map(_DRIVER_CAT).value_counts()
    if counts.empty:
        return ""
    tie = len(counts) >= 2 and counts.iloc[0] == counts.iloc[1]
    top = counts.index[0]
    # week keeps the "The median … {direction} mainly due to …" form;
    # month and year lead with an "Over the …" clause so the stored
    # notes don't all read as one repeated template
    prefix = {"month": "Over the month, ", "year": "Over the year, "} \
        .get(style)

    if tie:
        s = ("the change reflected a combination of A-share and H-share "
             "movements, with no single driver dominant.")
        return " " + ((prefix + s) if prefix else s[0].upper() + s[1:])
    if top == "both":
        # "Combination of factors" only means no leg cleared 60% within
        # single names — summed across the flagged names one leg still
        # dominates, so name it instead of printing filler (owner rule:
        # never say "movements in both the A- and H-shares")
        dom = _dominant_leg(sub)
        if dom is None:
            s = ("the change reflected a combination of A-share and "
                 "H-share movements, with no single driver dominant.")
            return " " + ((prefix + s) if prefix else s[0].upper() + s[1:])
        leg, val = dom
        top = ("fx" if leg == "fx" else
               (("h_weak" if val > 0 else "h_strong") if leg == "h" else
                ("a_strong" if val > 0 else "a_weak")))
    if top == "fx":
        s = ("the change was mainly associated with movements in the "
             "CNY/HKD exchange rate.")
        return " " + ((prefix + s) if prefix else s[0].upper() + s[1:])
    direction, perf = _LEG_WORDS[top]
    if prefix:
        form = "wider" if direction == "widened" else "narrower"
        return (f" {prefix}the {form} H-share discount to "
                f"A-shares was mainly due to {perf}.")
    return (f" The median H-share discount to A-shares {direction} "
            f"mainly due to {perf}.")


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
        drivers = attribution[attribution["Company"].isin(top["Company"])] \
            if attribution is not None and not attribution.empty \
            else pd.DataFrame()
        # name the dominant price leg for these three, or say nothing:
        # a vague "movements in both" clause is banned wording
        dom = _dominant_leg(drivers)
        cause = ""
        if dom is not None:
            leg, val = dom
            phrase = {
                ("h", False): "their H shares outperformed their "
                              "corresponding A shares",
                ("h", True): "their H shares lagged their "
                             "corresponding A shares",
                ("a", True): "their A shares outperformed their "
                             "corresponding H shares",
                ("a", False): "their A shares weakened against their "
                              "corresponding H shares",
            }.get((leg, val > 0))
            cause = (", mainly reflecting the CNY/HKD exchange-rate move"
                     if phrase is None else f", mainly because {phrase}")
        amount = (f" by {abs(med):.1f} percentage points"
                  if abs(med) > 0.05 else "")
        parts.append(
            f"**Focus Holdings:** The median H-share discount among focus "
            f"holdings {_direction(med)}{amount} today. "
            f"The discounts that narrowed most were "
            f"{_fmt_names(list(top['Company']))}{cause}.")

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
            f"universe (excluding focus holdings), the median H-share "
            f"discount {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points"
               if abs(med) > 0.05 else "")
            + f".{flag}"
            + _driver_sentence(attribution, rest["Company"]))

    both = table.dropna(subset=["Δ1d (pp)"]).copy()
    if not both.empty:
        both["dd"] = _discount_delta(both, "Δ1d (pp)")
        med = both["dd"].median()
        level = metrics.premium_to_discount(
            both["Premium calc (%)"]).median()
        parts.append(
            f"**Full A–H Universe:** Taking focus holdings and the rest "
            f"together, the median H-share discount across all "
            f"{len(both)} companies {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points"
               if abs(med) > 0.05 else "")
            + f" today and stands at {level:.1f}%.")

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
    col = {"1w": "Δ1w (pp)", "1m": "Δ1m (pp)", "1y": "Δ1y (pp)"}[period]
    label = {"1w": "week", "1m": "month", "1y": "year"}[period]
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
            + _driver_sentence(attribution, focus["Company"],
                               style=label))

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
            f"universe (excluding focus holdings), the median H-share "
            f"discount {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points" if abs(med) > 0.05
               else "") + f".{flag}"
            + _driver_sentence(attribution, rest["Company"],
                               style=label, min_move_pp=2.0))

    both = table.dropna(subset=[col]).copy()
    if not both.empty:
        both["dd"] = _discount_delta(both, col)
        med = both["dd"].median()
        level = metrics.premium_to_discount(
            both["Premium calc (%)"]).median()
        parts.append(
            f"**Full A–H Universe ({label}):** Taking focus holdings and "
            f"the rest together, the median H-share discount across all "
            f"{len(both)} companies {_direction(med)}"
            + (f" by {abs(med):.1f} percentage points"
               if abs(med) > 0.05 else "")
            + f" over the past {label} and stands at {level:.1f}%.")

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
