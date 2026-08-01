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


def att_of(drivers, move=-8.0):
    return pd.DataFrame({
        "Company": [f"Co{i}" for i in range(len(drivers))],
        "Driver": drivers,
        "Premium move (pp)": [move] * len(drivers),
    })


ALL_COS = [f"Co{i}" for i in range(10)]


class TestDriverSentence:
    def test_each_leg_mapping_with_direction(self):
        cases = {
            "H-share underperformance":
                "widened mainly due to weaker H-share performance",
            "H-share outperformance":
                "narrowed mainly due to stronger H-share performance",
            "A-share outperformance":
                "widened mainly due to stronger A-share performance",
            "A-share underperformance":
                "narrowed mainly due to weaker A-share performance",
        }
        for driver, phrase in cases.items():
            s = commentary._driver_sentence(att_of([driver]), ALL_COS)
            assert phrase in s, driver
            assert "H-share discount to A-shares" in s
            assert "H–A discount" not in s

    def test_movement_in_both_legs_case(self):
        s = commentary._driver_sentence(
            att_of(["Combination of factors"] * 2), ALL_COS)
        assert "movements in both the A- and H-shares" in s

    def test_highest_count_wins(self):
        s = commentary._driver_sentence(
            att_of(["H-share underperformance", "H-share underperformance",
                    "H-share underperformance", "A-share outperformance"]),
            ALL_COS)
        assert "weaker H-share performance" in s
        assert "A-share" not in s.replace("A-shares", "")   # single driver

    def test_tied_top_categories_not_forced(self):
        s = commentary._driver_sentence(
            att_of(["H-share underperformance", "H-share underperformance",
                    "A-share outperformance", "A-share outperformance"]),
            ALL_COS)
        assert "no single driver dominant" in s
        assert "mainly due to" not in s

    def test_weekly_and_monthly_wording_differ(self):
        att = att_of(["A-share outperformance"])
        wk = commentary._driver_sentence(att, ALL_COS, style="week")
        mo = commentary._driver_sentence(att, ALL_COS, style="month")
        assert wk != mo
        assert "Over the month" in mo and "Over the month" not in wk
        assert "wider H-share discount to A-shares was mainly due to " \
               "stronger A-share performance" in mo

    def test_silent_without_attribution(self):
        assert commentary._driver_sentence(None, ALL_COS) == ""
        assert commentary._driver_sentence(
            att_of(["H-share outperformance"], move=0.4), ALL_COS) == ""


def test_no_robotic_phrases_in_rendered_commentary():
    t = make_table()
    t.loc[:, "Δ1w (pp)"] = [-10.0, -6.0]
    t.loc[:, "Δ1m (pp)"] = [-10.0, -6.0]
    att = att_of(["H-share outperformance"])
    att.loc[0, "Company"] = "PetroChina"          # in the 'rest' group
    for text in (commentary.daily_commentary(t, att),
                 commentary.period_commentary(t, "1w", att),
                 commentary.period_commentary(t, "1m", att)):
        low = text.lower()
        assert "cause" not in low
        assert "a mix of factors" not in low
        assert "(1 name" not in text and "names)" not in text


def test_period_commentary_gains_driver_sentence():
    t = make_table()
    t.loc[:, "Δ1w (pp)"] = [-10.0, -6.0]
    att = att_of(["H-share outperformance"])
    att.loc[0, "Company"] = "Bank of China"       # focus name
    text = commentary.period_commentary(t, "1w", att)
    assert "narrowed mainly due to stronger H-share performance" in text
    # without attribution the paragraphs still render, minus the sentence
    assert "mainly due to" not in commentary.period_commentary(t, "1w")
