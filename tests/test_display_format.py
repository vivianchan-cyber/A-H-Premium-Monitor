"""Display formatting helpers."""

from __future__ import annotations

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
