"""Market Overview rules: valuation-status bands, 5-year median context,
insufficient-history handling, and the deterministic interpretation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ahmon import config, metrics


class TestValuationStatusBands:
    # Upper bounds are inclusive; a hair above moves to the next band.
    CASES = [
        (0.0, "Very attractive relative to history"),
        (10.0, "Very attractive relative to history"),
        (10.01, "Attractive relative to history"),
        (25.0, "Attractive relative to history"),
        (25.01, "Moderately attractive"),
        (50.0, "Moderately attractive"),
        (50.01, "Moderately expensive"),
        (75.0, "Moderately expensive"),
        (75.01, "Expensive relative to history"),
        (90.0, "Expensive relative to history"),
        (90.01, "Very expensive relative to history"),
        (100.0, "Very expensive relative to history"),
    ]

    @pytest.mark.parametrize("pct,label", CASES)
    def test_band_boundaries(self, pct, label):
        assert metrics.valuation_status(pct) == label

    def test_missing_percentile(self):
        assert metrics.valuation_status(None) is None
        assert metrics.valuation_status(float("nan")) is None


def bdays(n, end="2026-07-23"):
    return pd.bdate_range(end=end, periods=n)


class TestHsahpValuation:
    def test_full_window_median_and_distance(self):
        # 1300 obs (> 5y window); last 1260 run linearly 100..125.9
        # with the current value at the top of the window.
        vals = np.concatenate([np.full(40, 100.0),
                               np.linspace(100, 125.9, 1260)])
        v = metrics.hsahp_valuation(pd.Series(vals, index=bdays(1300)))
        assert v["sufficient"] and v["full_window"]
        assert v["window_label"] == "5-year"
        assert v["current"] == pytest.approx(125.9)
        assert v["median"] == pytest.approx(112.95, abs=0.02)
        assert v["diff_pts"] == pytest.approx(125.9 - v["median"])
        assert v["diff_pct"] == pytest.approx(
            (125.9 / v["median"] - 1) * 100)
        assert v["percentile"] == pytest.approx(100.0)

    def test_below_median_distance_is_negative(self):
        vals = np.concatenate([np.linspace(150, 120, 1290),
                               [110.0] * 10])
        v = metrics.hsahp_valuation(pd.Series(vals, index=bdays(1300)))
        assert v["diff_pts"] < 0 and v["diff_pct"] < 0

    def test_partial_history_is_labelled_not_5y(self):
        # ~2 years of data: stats exist but must not claim "5-year"
        v = metrics.hsahp_valuation(
            pd.Series(np.linspace(110, 120, 504), index=bdays(504)))
        assert v["sufficient"] and not v["full_window"]
        assert v["window_label"].startswith("available 2.0-year")
        assert "5-year" not in v["window_label"]

    def test_too_little_history_gives_no_statistics(self):
        v = metrics.hsahp_valuation(
            pd.Series(np.linspace(110, 120, 100), index=bdays(100)))
        assert not v["sufficient"]
        assert v["median"] is None and v["percentile"] is None
        assert metrics.valuation_status(v["percentile"]) is None


class TestInterpretation:
    def make(self, **over):
        v = {"current": 123.44, "sufficient": True, "full_window": True,
             "window_label": "5-year", "median": 132.10,
             "diff_pts": 123.44 - 132.10,
             "diff_pct": (123.44 / 132.10 - 1) * 100, "percentile": 17.0}
        v.update(over)
        return v

    def test_structure_and_points_wording(self):
        text = metrics.hsahp_interpretation(
            self.make(), {"1m": 2.0, "1y": -0.8})
        assert "The HSAHP Index is 123.44" in text
        assert "average A-share premium of 23.4%" in text
        assert "17th percentile of its 5-year history" in text
        assert "8.7 points below its 5-year median of 132.10" in text
        assert "widened by 2.0 points over the past month" in text
        assert "0.8 points below its level one year ago" in text
        assert "%" not in text.split("premium of 23.4%")[1]  # changes in pts

    def test_narrowing_and_missing_year(self):
        text = metrics.hsahp_interpretation(
            self.make(), {"1m": -3.2, "1y": None})
        assert "narrowed by 3.2 points" in text
        assert "one year ago" not in text

    def test_insufficient_history_stays_silent_on_percentile(self):
        v = self.make(sufficient=False, median=None, percentile=None,
                      diff_pts=None, window_label=None)
        text = metrics.hsahp_interpretation(v, {"1m": None, "1y": None})
        assert "percentile" not in text and "median" not in text
        assert "The HSAHP Index is 123.44" in text
