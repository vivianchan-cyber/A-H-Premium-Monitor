"""Scheduled refresh worker — Railway cron entry point.

The Railway ``charismatic-courage`` scheduled service starts with::

    python -m ahmon.cron_refresh

It runs **one** refresh cycle and exits; it is a cron job, not a long-running
scheduler (Railway owns the cadence — see ``REFRESH_MINUTES`` in
:mod:`ahmon.config` for the intended per-tier windows).

Safe by default
---------------
A production cron run performs **no writes**. It never rebuilds sample data,
never overwrites observation rows, and never re-seeds classifications:

* If a live-data pipeline is available (Phase 2+), it runs that pipeline.
* If no live source is configured — the current Phase 1 state — it logs
  ``Refresh skipped: no live data source configured`` and exits ``0`` so
  Railway sees a healthy run rather than a crash.

Sample-data generation is a **development-only** convenience, gated behind the
``ALLOW_SAMPLE_REFRESH`` environment variable (default off). Even when it is
enabled, the builder runs **only against an empty database**: if any company,
classification, or daily observation already exists, the refresh refuses to
run rather than overwrite real data or revert dashboard classification edits.

Exit code is ``0`` for every successful outcome (live refresh, skip, or an
enabled sample build) and non-zero only when a refresh raises.
"""

from __future__ import annotations

import os
import sys

from . import config, db, sample_data

SKIP_MESSAGE = "Refresh skipped: no live data source configured"

_TRUE = {"1", "true", "yes", "on"}


def _sample_refresh_enabled() -> bool:
    """Sample generation is opt-in via ALLOW_SAMPLE_REFRESH (default false).

    Read at call time so a cron run and the test suite can toggle it via the
    environment without reimporting the module."""
    return os.environ.get("ALLOW_SAMPLE_REFRESH", "").strip().lower() in _TRUE


def _live_refresh_available() -> bool:
    """Phase 2+ hook. Return True once a real akshare/yfinance pipeline is
    wired in, and run it from :func:`refresh`. Phase 1 has none."""
    return False


def _db_has_data(db_path) -> bool:
    """True if the database already holds companies, classifications, or daily
    observations — i.e. there is real state a sample rebuild would clobber."""
    conn = db.connect(db_path)
    try:
        for table in ("companies", "classifications", "daily_obs"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        return False
    finally:
        conn.close()


def refresh(db_path=config.DB_PATH, portfolio_csv=None) -> dict:
    """Run one refresh cycle. Returns a small result dict describing the
    action taken (``"action"`` is one of ``live_refreshed``, ``skipped``,
    ``sample_built``, ``sample_skipped_nonempty``). Never raises for the
    ordinary "nothing to do" cases — those are successful outcomes."""
    # 1. Live pipeline — the only thing a production cron should ever write.
    if _live_refresh_available():
        # Phase 2+: invoke the live source here and return its result.
        raise NotImplementedError(
            "live refresh advertised but not implemented")  # pragma: no cover

    # 2. No live source. Default: do nothing, touch nothing, exit clean.
    if not _sample_refresh_enabled():
        print(SKIP_MESSAGE)
        return {"action": "skipped"}

    # 3. Sample refresh explicitly enabled (development only).
    if _db_has_data(db_path):
        print("Sample refresh skipped: database already contains data; "
              "refusing to overwrite observations or reseed classifications "
              "(ALLOW_SAMPLE_REFRESH ignored on a non-empty database)",
              file=sys.stderr)
        return {"action": "sample_skipped_nonempty"}

    sample_data.build(db_path=db_path, portfolio_csv=portfolio_csv)
    conn = db.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM companies").fetchone()["n"]
    finally:
        conn.close()
    print(f"Sample data generated into empty database "
          f"(ALLOW_SAMPLE_REFRESH set) — {n} companies")
    return {"action": "sample_built", "companies": n}


def main(argv=None) -> int:
    """Console entry point. Returns 0 on every successful outcome and non-zero
    only when the refresh raises, so Railway reports a real deploy status."""
    try:
        refresh()
    except Exception as exc:  # noqa: BLE001 - cron boundary: report and fail loudly
        print(f"[ahmon.cron_refresh] refresh FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
