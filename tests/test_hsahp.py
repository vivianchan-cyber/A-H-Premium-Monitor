"""HSAHP mirror parsing/validation — offline."""

from __future__ import annotations

import pytest

from ahmon.sources import SchemaChangeError
from ahmon.sources.hsahp import BAND, parse_klines


class TestParseKlines:
    def test_parses_and_sorts(self):
        s = parse_klines(["2026-07-15,125.04", "2026-07-14,124.50"])
        assert list(s.index.strftime("%Y-%m-%d")) == \
            ["2026-07-14", "2026-07-15"]
        assert s.iloc[-1] == pytest.approx(125.04)

    def test_duplicate_dates_keep_last(self):
        s = parse_klines(["2026-07-15,120.00", "2026-07-15,125.04"])
        assert len(s) == 1 and s.iloc[0] == pytest.approx(125.04)

    def test_empty_payload_raises(self):
        with pytest.raises(SchemaChangeError, match="empty"):
            parse_klines([])

    def test_out_of_band_value_refused(self):
        # A level of 12.5 would mean the mirror changed meaning (e.g.
        # started returning something that is not the index level).
        with pytest.raises(SchemaChangeError, match="band"):
            parse_klines(["2026-07-15,12.50"])
        with pytest.raises(SchemaChangeError, match="band"):
            parse_klines([f"2026-07-15,{BAND[1] + 50}"])
