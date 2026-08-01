"""Sectors tab arithmetic: the H-discount view of sector statistics."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import metrics


class TestPremiumToDiscount:
    def test_known_values(self):
        # +100% premium: A costs 2x H, so H is 50% off
        assert metrics.premium_to_discount(100.0) == pytest.approx(50.0)
        assert metrics.premium_to_discount(0.0) == pytest.approx(0.0)
        # a discounted A share (rare) flips sign
        assert metrics.premium_to_discount(-20.0) == pytest.approx(-25.0)

    def test_monotone_so_orderings_survive(self):
        prems = pd.Series([10.0, 50.0, 120.0, 300.0])
        discs = metrics.premium_to_discount(prems)
        assert list(discs.sort_values().index) == \
            list(prems.sort_values().index)


class TestSectorStats:
    def make_table(self):
        # two sectors; premiums chosen for round discounts
        return pd.DataFrame({
            "Sector": ["Financials", "Financials", "Technology"],
            "Premium calc (%)": [100.0, 25.0, 300.0],
            "H discount to A (%)": [50.0, 20.0, 75.0],
            "Δ1m (pp)": [10.0, 5.0, 0.0],
            "Δ1y (pp)": [None, None, None],
        })

    def test_median_discount_and_delta_in_discount_points(self):
        s = metrics.sector_stats(None, self.make_table()) \
            .set_index("Sector")
        assert s.loc["Financials", "Median H discount (%)"] == \
            pytest.approx(35.0)               # median of 50 and 20
        assert s.loc["Technology", "Median H discount (%)"] == \
            pytest.approx(75.0)
        # Δ1m for the 100%-premium name: premium was 90 a month ago →
        # discount was 90/190 = 47.368 → change +2.632pp; for the
        # 25%-premium name: 20 a month ago → 16.667 → +3.333pp;
        # median of the two ≈ +2.98pp
        assert s.loc["Financials", "Δ1m median (pp)"] == \
            pytest.approx(2.98, abs=0.01)
        # sorted by median discount, biggest first
        first = metrics.sector_stats(None, self.make_table()).iloc[0]
        assert first["Sector"] == "Technology"
