"""Market-move alerts speak the H-discount view; integrity alerts and
trigger thresholds stay on the premium."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import alerts, db


def test_daily_move_alert_worded_in_discount_points(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    table = pd.DataFrame([{
        "Company": "Bank of China",
        "Premium calc (%)": 40.0,      # was 30% yesterday: +10pp premium
        "Δ1d (pp)": 10.0, "Δ1m (pp)": None,
        "H % change today": None, "52w percentile": 99.0,
        "Premium z (1y)": None, "Calc-src diff (pp)": None,
        "Quality": "live", "Updated": "now",
    }])
    fired = alerts.evaluate(conn, table).set_index("rule")
    # discount went 23.077 -> 28.571: widened 5.5pp
    msg = fired.loc["daily_premium_move", "message"]
    assert "H-share discount to A-shares widened 5.5pp in a day" in msg
    assert "premium move +10.0pp" in msg          # convention kept in brackets
    assert fired.loc["52w_extreme", "message"] == \
        "H-share discount to A-shares at/near its 52-week widest"
    conn.close()
