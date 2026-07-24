"""Display formatting helpers."""

from __future__ import annotations

import io

import pandas as pd

from ahmon import metrics


class TestFormatChangePts:
    def test_negative_zero_displays_unsigned(self):
        assert metrics.format_change_pts(-0.04) == "0.0 pts"
        assert metrics.format_change_pts(-0.0) == "0.0 pts"
        assert metrics.format_change_pts(0.0) == "0.0 pts"
        assert metrics.format_change_pts(0.04) == "0.0 pts"

    def test_signed_values_keep_sign(self):
        assert metrics.format_change_pts(2.04) == "+2.0 pts"
        assert metrics.format_change_pts(-0.8) == "-0.8 pts"
        assert metrics.format_change_pts(-0.05) == "-0.1 pts"

    def test_missing_is_em_dash(self):
        assert metrics.format_change_pts(None) == "—"
        assert metrics.format_change_pts(float("nan")) == "—"


class TestXlsxExport:
    def test_round_trips_through_excel(self):
        df = pd.DataFrame({"Company": ["Bank of China", "比亚迪"],
                           "Premium calc (%)": [30.09, 19.5]})
        blob = metrics.table_to_xlsx_bytes(df, sheet="focus_a-h_holdings")
        assert blob[:2] == b"PK"                    # xlsx = zip container
        back = pd.read_excel(io.BytesIO(blob))
        assert list(back["Company"]) == ["Bank of China", "比亚迪"]
        assert back["Premium calc (%)"].iloc[0] == 30.09

    def test_long_sheet_name_capped_at_31_chars(self):
        df = pd.DataFrame({"a": [1]})
        blob = metrics.table_to_xlsx_bytes(df, sheet="x" * 60)
        assert pd.read_excel(io.BytesIO(blob)).iloc[0, 0] == 1
