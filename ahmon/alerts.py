"""Alert rules evaluated from calculated data; results go to the dashboard
and to the alerts_log table. No external messages are sent (v1 rule)."""

from __future__ import annotations

import pandas as pd

from . import config, db


def evaluate(conn, table: pd.DataFrame,
             dedupe_daily: bool = False) -> pd.DataFrame:
    """Run all configured rules against the current monitor table.
    Returns the fired alerts and writes them to the log. With
    dedupe_daily=True a given (rule, company) is logged at most once per
    calendar day — required when the scheduler evaluates every few
    minutes; the dashboard button keeps the immediate behaviour."""
    fired = []
    today = db.now_iso()[:10]

    def fire(rule, company, message, severity="warning"):
        if dedupe_daily and db.alert_exists_on_day(conn, rule, company,
                                                   today):
            return
        fired.append({"rule": rule, "company": company, "severity": severity,
                      "message": message})
        db.log_alert(conn, rule, message, company, severity)

    for _, r in table.iterrows():
        name = r["Company"]
        if r["Δ1d (pp)"] is not None and \
                abs(r["Δ1d (pp)"]) > config.ALERT_DAILY_PREMIUM_MOVE_PP:
            fire("daily_premium_move", name,
                 f"1-day premium move {r['Δ1d (pp)']:+.1f}pp "
                 f"(threshold ±{config.ALERT_DAILY_PREMIUM_MOVE_PP:.0f}pp)")
        if r["Δ1m (pp)"] is not None and \
                abs(r["Δ1m (pp)"]) > config.ALERT_MONTHLY_PREMIUM_MOVE_PP:
            fire("monthly_premium_move", name,
                 f"1-month premium move {r['Δ1m (pp)']:+.1f}pp "
                 f"(threshold ±{config.ALERT_MONTHLY_PREMIUM_MOVE_PP:.0f}pp)")
        if r["H 1d ret (%)"] is not None and \
                abs(r["H 1d ret (%)"]) > config.ALERT_H_PRICE_MOVE_PCT:
            fire("h_price_move", name,
                 f"H-share 1-day move {r['H 1d ret (%)']:+.1f}% "
                 f"(threshold ±{config.ALERT_H_PRICE_MOVE_PCT:.0f}%)")
        if r["52w percentile"] is not None:
            if r["52w percentile"] >= 98:
                fire("52w_extreme", name, "Premium at/near 52-week high",
                     "info")
            elif r["52w percentile"] <= 2:
                fire("52w_extreme", name, "Premium at/near 52-week low",
                     "info")
        if r["Premium z (1y)"] is not None and \
                abs(r["Premium z (1y)"]) > config.ALERT_ZSCORE:
            fire("premium_zscore", name,
                 f"Premium {r['Premium z (1y)']:+.1f}σ from 1-year average")
        if r["Calc-src diff (pp)"] is not None and \
                abs(r["Calc-src diff (pp)"]) > config.ALERT_DISCREPANCY_PP:
            fire("calc_vs_source", name,
                 f"Calculated vs source premium differ by "
                 f"{r['Calc-src diff (pp)']:+.1f}pp — verify source figure",
                 "serious")
        if r["Quality"] == "stale":
            fire("stale_data", name,
                 f"No successful update since {r['Updated']}", "serious")

    return pd.DataFrame(fired)
