"""Commentary speaks the H-discount convention with correct arithmetic."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import commentary, config


def make_table():
    # one focus name: premium 100% (disc 50), fell 10pp today (was 110,
    # disc 52.38) → discount narrowed by 2.38pp
    return pd.DataFrame({
        "Company": ["Bank of China", "PetroChina"],
        "Classification": [config.FOCUS, config.OTHER],
        "Premium calc (%)": [100.0, 50.0],
        "Δ1d (pp)": [-10.0, 0.0],
        "Δ1w (pp)": [None, None], "Δ1m (pp)": [None, None],
        "52w percentile": [None, None],
        "Quality": ["live", "live"],
    })


def test_daily_commentary_in_discount_points():
    att = pd.DataFrame(columns=["Company", "Driver"])
    text = commentary.daily_commentary(make_table(), att)
    assert "median H-share discount" in text
    assert "narrowed by 2.4 percentage points" in text
    assert "premium" not in text.lower()


def test_discount_delta_math():
    t = make_table().dropna(subset=["Δ1d (pp)"])
    dd = commentary._discount_delta(t, "Δ1d (pp)")
    # 100% premium today (disc 50.0) vs 110% yesterday (disc 52.381)
    assert dd.iloc[0] == pytest.approx(50.0 - 110.0 / 210.0 * 100, abs=1e-6)
    assert dd.iloc[1] == pytest.approx(0.0)


def test_closing_summary_direction_key():
    att = pd.DataFrame(columns=["Company", "Driver"])
    sect = pd.DataFrame(columns=["Sector", "Δ1m median (pp)"])
    text = commentary.closing_summary(make_table(), att, sect)
    assert "H-share discount" in text
    assert "negative = H catching up" in text
