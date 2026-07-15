"""Calculation engine: premium formula, conversions, changes, attribution.

Definitions (see CLAUDE.md / docs/data_dictionary.md):

    A-share premium (%) = ((A_price_CNY * HKD_per_CNY) / H_price_HKD - 1) * 100

The FX rate is ALWAYS "HKD per 1 CNY" (about 1.05–1.10 historically).
A positive premium means the A share trades above its H counterpart.

AASTOCKS displays the opposite convention: its premium is the H share
relative to the A share's HKD-equivalent price (positive = H above A).
`a_premium_from_h_premium` converts that convention into ours.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config


# ---------------------------------------------------------------- premium

def a_share_premium(a_price_cny: float, h_price_hkd: float,
                    hkd_per_cny: float) -> float:
    """Standard dashboard premium: A share over H share, in percent."""
    if a_price_cny is None or h_price_hkd is None or hkd_per_cny is None:
        raise ValueError("price/FX inputs must not be None")
    if a_price_cny <= 0 or h_price_hkd <= 0:
        raise ValueError("prices must be positive")
    if not 0.5 <= hkd_per_cny <= 2.0:
        # Guard against the inverted rate (CNY per HKD ~ 0.90–0.95 fails too;
        # the plausible HKD-per-CNY band is enforced deliberately narrowly).
        raise ValueError(
            f"hkd_per_cny={hkd_per_cny} outside plausible band; "
            "check the FX direction (must be HKD per 1 CNY)")
    return ((a_price_cny * hkd_per_cny) / h_price_hkd - 1.0) * 100.0


def a_premium_from_h_premium(h_premium_pct: float) -> float:
    """Convert an AASTOCKS-style H-share premium into our A-share premium.

    If source reports p = (H / (A*fx) - 1) * 100, then
    A premium = (A*fx / H - 1) * 100 = (1 / (1 + p/100) - 1) * 100.
    """
    return (1.0 / (1.0 + h_premium_pct / 100.0) - 1.0) * 100.0


def discrepancy_flag(calculated_pp: float, source_pp: float,
                     threshold_pp: float = config.ALERT_DISCREPANCY_PP) -> bool:
    """True when |calculated - source| exceeds the threshold (both already
    normalised to the A-share-premium convention)."""
    return abs(calculated_pp - source_pp) > threshold_pp


# ---------------------------------------------------------------- changes

def series_changes(s: pd.Series) -> dict:
    """Point changes of a (date-indexed, ascending) series over standard
    windows. For premium series the values are percentage points; for the
    HSAHP index they are index points unless callers convert."""
    out = {}
    if s is None or s.dropna().empty:
        return {k: None for k in ("1d", "1w", "1m", "3m", "ytd", "1y")}
    s = s.dropna()
    last = s.iloc[-1]

    def back(n):
        return s.iloc[-n - 1] if len(s) > n else None

    for label, n in (("1d", 1), ("1w", config.WINDOW_1W),
                     ("1m", config.WINDOW_1M), ("3m", config.WINDOW_3M),
                     ("1y", config.WINDOW_1Y)):
        prev = back(n)
        out[label] = None if prev is None else float(last - prev)

    year = s.index[-1].year
    prior = s[s.index.year < year]
    out["ytd"] = None if prior.empty else float(last - prior.iloc[-1])
    return out


def pct_return(s: pd.Series, n: int = 1) -> float | None:
    """Simple percent return over n observations."""
    s = s.dropna()
    if len(s) <= n or s.iloc[-n - 1] == 0:
        return None
    return (s.iloc[-1] / s.iloc[-n - 1] - 1.0) * 100.0


def rolling_stats(s: pd.Series, window: int) -> dict:
    """Percentile of the latest value, median, high and low over a trailing
    window (in observations). Returns None-filled dict when there is not at
    least half the window of history."""
    s = s.dropna()
    if len(s) < max(20, window // 2):
        return {"percentile": None, "median": None, "high": None, "low": None}
    tail = s.iloc[-window:]
    last = tail.iloc[-1]
    return {
        "percentile": float((tail <= last).mean() * 100.0),
        "median": float(tail.median()),
        "high": float(tail.max()),
        "low": float(tail.min()),
    }


def zscore(s: pd.Series, window: int = config.WINDOW_1Y) -> float | None:
    s = s.dropna()
    if len(s) < 60:
        return None
    tail = s.iloc[-window:]
    sd = tail.std()
    if not sd or math.isnan(sd) or sd == 0:
        return None
    return float((tail.iloc[-1] - tail.mean()) / sd)


# ------------------------------------------------------------ attribution

@dataclass
class Attribution:
    driver: str            # one of the six labels below
    premium_move_pp: float
    a_contrib_pp: float    # +ve pushes the premium up
    h_contrib_pp: float    # already sign-flipped: +ve pushes the premium up
    fx_contrib_pp: float

DRIVERS = [
    "H-share outperformance",
    "A-share underperformance",
    "A-share outperformance",
    "H-share underperformance",
    "CNY/HKD movement",
    "Combination of factors",
]


def attribute_premium_move(a0: float, a1: float, h0: float, h1: float,
                           fx0: float, fx1: float) -> Attribution:
    """Decompose a premium move into A-price, H-price and FX contributions.

    Uses log returns: d ln(1 + p/100) = d ln A + d ln fx - d ln H, scaled to
    approximate percentage points so contributions sum to the actual move.
    Purely arithmetic — no inference, per project rules.
    """
    p0 = a_share_premium(a0, h0, fx0)
    p1 = a_share_premium(a1, h1, fx1)
    move = p1 - p0

    la = math.log(a1 / a0)
    lh = -math.log(h1 / h0)          # H up ⇒ premium down
    lfx = math.log(fx1 / fx0)
    total = la + lh + lfx
    if total == 0:
        scale = 0.0
    else:
        scale = move / total          # distribute the exact pp move
    contribs = {"A": la * scale, "H": lh * scale, "FX": lfx * scale}

    ranked = sorted(contribs.items(), key=lambda kv: abs(kv[1]), reverse=True)
    top_key, top_val = ranked[0]
    second_val = abs(ranked[1][1])

    if abs(move) < 0.05:
        driver = "Combination of factors"
    elif second_val > 0.6 * abs(top_val):
        driver = "Combination of factors"
    elif top_key == "FX":
        driver = "CNY/HKD movement"
    elif top_key == "A":
        driver = ("A-share outperformance" if top_val > 0
                  else "A-share underperformance")
    else:  # H leg; contribution is sign-flipped
        driver = ("H-share underperformance" if top_val > 0
                  else "H-share outperformance")

    return Attribution(driver=driver, premium_move_pp=move,
                       a_contrib_pp=contribs["A"], h_contrib_pp=contribs["H"],
                       fx_contrib_pp=contribs["FX"])


# ------------------------------------------------------------ resampling

def resample_for_range(s: pd.Series, range_key: str) -> pd.Series:
    """Chart-range policy: 3m→daily, 1y→weekly, 3y/5y/max→monthly.
    Daily history is always stored; this only affects display."""
    s = s.dropna()
    if s.empty:
        return s
    end = s.index[-1]
    if range_key == "3m":
        return s[s.index >= end - pd.DateOffset(months=3)]
    if range_key == "1y":
        return s[s.index >= end - pd.DateOffset(years=1)].resample("W-FRI").last().dropna()
    if range_key in ("3y", "5y"):
        years = int(range_key[0])
        return s[s.index >= end - pd.DateOffset(years=years)].resample("ME").last().dropna()
    return s.resample("ME").last().dropna()   # max
