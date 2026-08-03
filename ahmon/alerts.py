"""Alert rules evaluated from calculated data; results go to the dashboard
and to the alerts_log table. No external messages are sent (v1 rule)."""

from __future__ import annotations

import pandas as pd

from . import config, db, metrics


def _disc_move(premium_now: float, delta_pp: float) -> float:
    """Discount-point equivalent of a premium move ending at
    premium_now: today's discount minus the window-start discount."""
    return (metrics.premium_to_discount(premium_now)
            - metrics.premium_to_discount(premium_now - delta_pp))


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

    # Market-move alerts are worded in the owner's H-discount view (the
    # equivalent premium move rides along in brackets); trigger
    # thresholds stay defined on the premium so historical tuning and
    # dedupe continuity are untouched. Data-integrity alerts
    # (calc_vs_source, staleness) keep premium wording — they compare us
    # against sources that publish premiums.
    def _move_msg(prem, delta_pp, window: str, threshold: float) -> str:
        """Discount-view wording, falling back to plain premium points
        when the row carries no premium level to transform from."""
        if prem is not None and not pd.isna(prem):
            d = _disc_move(prem, delta_pp)
            return (f"H-share discount to A-shares "
                    f"{'widened' if d > 0 else 'narrowed'} "
                    f"{abs(d):.1f}pp {window} "
                    f"(premium move {delta_pp:+.1f}pp, threshold "
                    f"±{threshold:.0f}pp)")
        return (f"{window} premium move {delta_pp:+.1f}pp "
                f"(threshold ±{threshold:.0f}pp)")

    for _, r in table.iterrows():
        name = r["Company"]
        prem = r.get("Premium calc (%)")
        if r["Δ1d (pp)"] is not None and \
                abs(r["Δ1d (pp)"]) > config.ALERT_DAILY_PREMIUM_MOVE_PP:
            fire("daily_premium_move", name,
                 _move_msg(prem, r["Δ1d (pp)"], "in a day",
                           config.ALERT_DAILY_PREMIUM_MOVE_PP))
        if r["Δ1m (pp)"] is not None and \
                abs(r["Δ1m (pp)"]) > config.ALERT_MONTHLY_PREMIUM_MOVE_PP:
            fire("monthly_premium_move", name,
                 _move_msg(prem, r["Δ1m (pp)"], "over a month",
                           config.ALERT_MONTHLY_PREMIUM_MOVE_PP))
        if r["H % change today"] is not None and \
                abs(r["H % change today"]) > config.ALERT_H_PRICE_MOVE_PCT:
            fire("h_price_move", name,
                 f"H-share 1-day move {r['H % change today']:+.1f}% "
                 f"(threshold ±{config.ALERT_H_PRICE_MOVE_PCT:.0f}%)")
        if r["52w percentile"] is not None:
            if r["52w percentile"] >= 98:
                fire("52w_extreme", name,
                     "H-share discount to A-shares at/near its 52-week "
                     "widest", "info")
            elif r["52w percentile"] <= 2:
                fire("52w_extreme", name,
                     "H-share discount to A-shares at/near its 52-week "
                     "narrowest", "info")
        if r["Premium z (1y)"] is not None and \
                abs(r["Premium z (1y)"]) > config.ALERT_ZSCORE:
            fire("premium_zscore", name,
                 f"A–H gap {r['Premium z (1y)']:+.1f}σ from its 1-year "
                 f"average (z-score measured on the premium series)")
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
