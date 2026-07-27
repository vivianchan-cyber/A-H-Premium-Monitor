"""Scheduled refresh worker — Railway cron entry point.

The Railway ``charismatic-courage`` scheduled service starts with::

    python -m ahmon.cron_refresh

It runs **one** idempotent refresh cycle and exits; it is a cron job, not a
long-running scheduler (Railway owns the cadence — see ``REFRESH_MINUTES`` in
:mod:`ahmon.config` for the intended per-tier windows).

Phase 1 has no live feed: akshare / yfinance are Phase 2+ and still commented
out in ``requirements.txt``. The only data pipeline that exists is the
deterministic sample-data builder, so a refresh rebuilds/extends the sample
database idempotently (upserts keyed on company/date — repeated runs cannot
create duplicates). ``sample_data.build()`` marks every entry in
``source_health`` with status ``'sample'``, so a refreshed database is never
presented as live — the rule in ``CLAUDE.md`` is preserved.

When a live source is wired up (Phase 2+), route it through
:func:`refresh` and it will run on the same Railway schedule with no change to
the start command.
"""

from __future__ import annotations

import sys

from . import config, db, sample_data


def refresh(db_path=config.DB_PATH, portfolio_csv=None) -> int:
    """Run one refresh cycle; return the company count after refreshing.

    Idempotent — safe to run on every cron firing. In Phase 1 this delegates
    to the sample-data builder, the only pipeline available; the resulting
    rows are labelled ``sample`` in ``source_health``.
    """
    sample_data.build(db_path=db_path, portfolio_csv=portfolio_csv)
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    finally:
        conn.close()


def main(argv=None) -> int:
    """Console entry point. Returns 0 on success, non-zero on failure so the
    Railway scheduled service reports a real deploy status instead of a
    silent no-op."""
    try:
        n = refresh()
    except Exception as exc:  # noqa: BLE001 - cron boundary: report and fail loudly
        print(f"[ahmon.cron_refresh] refresh FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"[ahmon.cron_refresh] refresh OK - {n} companies in {config.DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
