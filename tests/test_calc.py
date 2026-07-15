"""Tests for the premium formula, FX direction, conversions, attribution."""

import math

import pandas as pd
import pytest

from ahmon import calc, config


class TestPremiumFormula:
    def test_definition_example(self):
        # A = 10 CNY, fx = 1.08 HKD per CNY -> 10.80 HKD equivalent.
        # H = 9 HKD -> premium = (10.8/9 - 1)*100 = 20%
        assert calc.a_share_premium(10.0, 9.0, 1.08) == pytest.approx(20.0)

    def test_parity_is_zero(self):
        assert calc.a_share_premium(10.0, 10.8, 1.08) == pytest.approx(0.0)

    def test_h_above_a_gives_negative_premium(self):
        # H share more expensive than the A-share HKD equivalent
        assert calc.a_share_premium(10.0, 12.0, 1.08) < 0

    def test_rejects_nonpositive_prices(self):
        with pytest.raises(ValueError):
            calc.a_share_premium(0, 9, 1.08)
        with pytest.raises(ValueError):
            calc.a_share_premium(10, -1, 1.08)


class TestFxDirection:
    """The FX input must be HKD per 1 CNY (≈1.05–1.10), never the inverse."""

    def test_inverted_rate_changes_answer_materially(self):
        # The inverse rate (CNY per HKD ≈ 0.926) still falls inside the
        # plausibility band, so direction cannot be caught by range checks
        # alone: a known parity pair must price to ~0% with the correct
        # rate and be unmistakably wrong with the inverse. Phase 2 runs
        # this same check against live parity pairs at startup.
        right = calc.a_share_premium(10.0, 10.8, 1.08)
        wrong = calc.a_share_premium(10.0, 10.8, 1 / 1.08)
        assert right == pytest.approx(0.0)
        assert abs(wrong) > 10  # inversion is unmistakably wrong

    def test_extreme_rates_rejected(self):
        with pytest.raises(ValueError):
            calc.a_share_premium(10, 9, 8.0)   # e.g. CNY per USD by mistake
        with pytest.raises(ValueError):
            calc.a_share_premium(10, 9, 0.13)  # inverse of the above

    def test_higher_hkd_per_cny_raises_premium(self):
        low = calc.a_share_premium(10.0, 9.0, 1.05)
        high = calc.a_share_premium(10.0, 9.0, 1.10)
        assert high > low


class TestConventionConversion:
    def test_round_trip(self):
        # A premium of +25% == H premium of -20%
        a_prem = 25.0
        h_prem = (1 / (1 + a_prem / 100) - 1) * 100
        assert calc.a_premium_from_h_premium(h_prem) == pytest.approx(a_prem)

    def test_zero_maps_to_zero(self):
        assert calc.a_premium_from_h_premium(0.0) == pytest.approx(0.0)

    def test_discrepancy_flag(self):
        assert not calc.discrepancy_flag(20.0, 20.9)
        assert calc.discrepancy_flag(20.0, 21.1)


class TestAttribution:
    def test_contributions_sum_to_move(self):
        att = calc.attribute_premium_move(10, 10.4, 9, 8.9, 1.08, 1.081)
        total = att.a_contrib_pp + att.h_contrib_pp + att.fx_contrib_pp
        assert total == pytest.approx(att.premium_move_pp, abs=1e-9)

    def test_h_outperformance_narrows(self):
        # H rallies 5%, A and FX flat -> premium narrows, driver = H outperf.
        att = calc.attribute_premium_move(10, 10, 9, 9.45, 1.08, 1.08)
        assert att.premium_move_pp < 0
        assert att.driver == "H-share outperformance"

    def test_a_underperformance_narrows(self):
        att = calc.attribute_premium_move(10, 9.5, 9, 9, 1.08, 1.08)
        assert att.premium_move_pp < 0
        assert att.driver == "A-share underperformance"

    def test_a_outperformance_widens(self):
        att = calc.attribute_premium_move(10, 10.5, 9, 9, 1.08, 1.08)
        assert att.premium_move_pp > 0
        assert att.driver == "A-share outperformance"

    def test_fx_only_move(self):
        att = calc.attribute_premium_move(10, 10, 9, 9, 1.05, 1.10)
        assert att.driver == "CNY/HKD movement"
        assert att.premium_move_pp > 0

    def test_mixed_move_is_combination(self):
        att = calc.attribute_premium_move(10, 10.3, 9, 8.75, 1.08, 1.08)
        assert att.driver == "Combination of factors"


class TestSeriesHelpers:
    def make_series(self, n=800):
        idx = pd.bdate_range("2023-01-02", periods=n)
        return pd.Series(range(n), index=idx, dtype=float)

    def test_changes_windows(self):
        s = self.make_series()
        ch = calc.series_changes(s)
        assert ch["1d"] == 1
        assert ch["1w"] == config.WINDOW_1W
        assert ch["1m"] == config.WINDOW_1M
        assert ch["1y"] == config.WINDOW_1Y

    def test_percentile_of_max_is_100(self):
        s = self.make_series()
        st = calc.rolling_stats(s, 252)
        assert st["percentile"] == pytest.approx(100.0)
        assert st["high"] == s.iloc[-1]

    def test_resample_policy(self):
        s = self.make_series()
        assert len(calc.resample_for_range(s, "3m")) >= 55       # daily
        weekly = calc.resample_for_range(s, "1y")
        assert 45 <= len(weekly) <= 60                            # weekly
        monthly = calc.resample_for_range(s, "max")
        assert len(monthly) <= 40                                 # monthly
