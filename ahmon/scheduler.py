"""Phase 4 scheduler: trading-hours refresh, automatic alerts, buy-level
signals and scheduled commentary.

Jobs (all times Asia/Singapore == HK time):
- refresh tick, every 5 minutes: runs only while HKEX or SSE is in
  session (exchange_calendars — holidays and lunch breaks respected).
  Each tick: full live refresh (one bulk Eastmoney call covers every
  tier, so the 5-minute Focus cadence covers the slower tiers for free),
  Focus intraday ticks into intraday_obs, alert evaluation and the
  buy-level screen — both deduped to once per (rule, company) per day.
- EOD snapshot, 16:15 on HK trading days: final refresh after the HK
  close + alerts/signals + daily and closing commentary stored to
  commentary_log; weekly commentary on Fridays, monthly on the last HK
  session of the month.
- One catch-up refresh at startup so a freshly started scheduler never
  sits on stale data waiting for the next session.

Run:  python -m ahmon.scheduler                # blocks; Ctrl-C to stop
      (run it next to `streamlit run app.py` — the dashboard reads the
      same database and shows every result of these jobs)
"""

from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from . import alerts, commentary, config, db, market_hours, metrics, signals
from .refresh import refresh_live

log = logging.getLogger("ahmon.scheduler")

REFRESH_EVERY_MIN = 5
EOD_HOUR, EOD_MINUTE = 16, 15


def run_refresh_cycle(conn, write_intraday: bool | None = None,
                      **refresh_kwargs) -> dict:
    """One complete cycle: refresh -> alerts (deduped) -> buy signals.
    Factored out of the scheduler so it is unit-testable and reusable."""
    if write_intraday is None:
        write_intraday = market_hours.hk_open()
    summary = refresh_live(conn, write_intraday=write_intraday,
                           **refresh_kwargs)
    table = metrics.monitor_table(conn)
    fired = alerts.evaluate(conn, table, dedupe_daily=True)
    watch = signals.buy_watch(table)
    new_signals = signals.log_signals(conn, watch, db.now_iso()[:10])
    summary["alerts_fired"] = len(fired)
    summary["buy_watch"] = len(watch)
    summary["buy_signals_new"] = new_signals
    return summary


def run_eod(conn) -> None:
    """EOD snapshot + stored commentary."""
    s = run_refresh_cycle(conn, write_intraday=False)
    table = metrics.monitor_table(conn)
    att = metrics.attribution_table(conn, table)
    db.insert_commentary(conn, "daily", commentary.daily_commentary(table, att))
    db.insert_commentary(conn, "closing", commentary.closing_summary(
        table, att, metrics.sector_stats(conn, table)))
    now = pd.Timestamp.now(tz=config.TZ)
    if now.dayofweek == 4:                                   # Friday
        db.insert_commentary(
            conn, "weekly",
            commentary.period_commentary(
                table, "1w", metrics.attribution_over(conn, table, 7)))
    cal = market_hours._cal("XHKG")
    nxt = cal.next_session(now.normalize().tz_localize(None))
    if pd.Timestamp(nxt).month != now.month:                 # month's last session
        db.insert_commentary(
            conn, "monthly",
            commentary.period_commentary(
                table, "1m", metrics.attribution_over(conn, table, 30)))
    if pd.Timestamp(nxt).year != now.year:                   # year's last session
        db.insert_commentary(
            conn, "yearly",
            commentary.period_commentary(
                table, "1y", metrics.attribution_over(conn, table, 365)))
    log.info("EOD snapshot + commentary stored (refresh: %s companies, "
             "%s buy-watch)", s.get("upserted"), s.get("buy_watch"))


def build_scheduler(conn):
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler(timezone=str(config.TZ))

    def tick():
        if not market_hours.any_market_open():
            return
        try:
            s = run_refresh_cycle(conn)
            log.info("tick: %s companies for %s · %s alerts · "
                     "%s on buy watch (%s new)",
                     s.get("upserted"), s.get("obs_date"),
                     s.get("alerts_fired"), s.get("buy_watch"),
                     s.get("buy_signals_new"))
        except Exception:
            log.exception("refresh tick failed (health panel has details)")

    def eod():
        # only on HK trading days; a holiday has no session to snapshot
        now = pd.Timestamp.now(tz=config.TZ)
        if market_hours._cal("XHKG").is_session(
                now.normalize().tz_localize(None)):
            try:
                run_eod(conn)
            except Exception:
                log.exception("EOD job failed")

    sched.add_job(tick, "interval", minutes=REFRESH_EVERY_MIN,
                  id="refresh_tick", coalesce=True, max_instances=1)
    sched.add_job(eod, "cron", day_of_week="mon-fri",
                  hour=EOD_HOUR, minute=EOD_MINUTE, id="eod_snapshot",
                  coalesce=True, max_instances=1)
    return sched


def main(argv=None):
    p = argparse.ArgumentParser(description="A–H Monitor scheduler")
    p.add_argument("--db", default=str(config.LIVE_DB_PATH))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect(args.db)
    log.info("startup catch-up refresh…")
    try:
        s = run_refresh_cycle(conn, write_intraday=False)
        log.info("catch-up done: %s companies · %s on buy watch",
                 s.get("upserted"), s.get("buy_watch"))
    except Exception:
        log.exception("catch-up refresh failed — scheduler continues")
    sched = build_scheduler(conn)
    log.info("scheduler running: refresh every %s min in market hours, "
             "EOD snapshot %02d:%02d on HK trading days",
             REFRESH_EVERY_MIN, EOD_HOUR, EOD_MINUTE)
    try:
        sched.start()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
