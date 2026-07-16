"""Single-shot refresh for a Railway cron service.

Unlike ahmon/scheduler.py (a long-running process), this runs one cycle
and exits — the shape a cron service expects. Behaviour:

- acquires the exclusive writer lock first (PostgreSQL advisory lock), so
  overlapping cron firings or a concurrently running scheduler can never
  write market updates at the same time; if the lock is held, it exits 0
  with a message and does nothing;
- by default refreshes only while HKEX or SSE is in session (so an
  every-5-minutes cron is cheap outside market hours); `--force` refreshes
  regardless (useful for a first run or debugging);
- `--eod` additionally stores the post-close commentary (schedule it once
  per day after 16:15 HK time).

Railway cron examples (see DEPLOYMENT.md):
    */5 * * * *   python -m ahmon.cron_refresh
    20 16 * * 1-5 python -m ahmon.cron_refresh --eod --force
(Railway cron runs in UTC: 16:20 HKT == 08:20 UTC → "20 8 * * 1-5".)
"""

from __future__ import annotations

import argparse
import sys

from . import db, market_hours


def main(argv=None):
    p = argparse.ArgumentParser(description="One-shot scheduled refresh")
    p.add_argument("--force", action="store_true",
                   help="refresh even while both markets are closed")
    p.add_argument("--eod", action="store_true",
                   help="also store post-close commentary")
    args = p.parse_args(argv)

    if not args.force and not market_hours.any_market_open():
        print("markets closed — nothing to do")
        return 0

    conn = db.connect()
    try:
        if not conn.try_writer_lock():
            print("another writer holds the refresh lock — skipping")
            return 0
        from .scheduler import run_eod, run_refresh_cycle
        if args.eod:
            run_eod(conn)
            print("EOD snapshot + commentary stored")
        else:
            s = run_refresh_cycle(conn)
            print(f"refreshed {s.get('upserted')} companies for "
                  f"{s.get('obs_date')} · {s.get('alerts_fired')} alerts · "
                  f"{s.get('buy_watch')} on buy watch")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
