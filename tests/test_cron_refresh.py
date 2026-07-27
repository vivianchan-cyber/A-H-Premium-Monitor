"""The Railway scheduled service starts with ``python -m ahmon.cron_refresh``.

These tests are the contract that keeps that command from crashing again:
the module must import cleanly, expose an entry point, and run to a clean
exit against a throwaway database.
"""

import importlib
import runpy
import subprocess
import sys

import pandas as pd
import pytest

from ahmon import config, cron_refresh, db


def _tiny_portfolio(tmp_path):
    """A two-row portfolio so a refresh is fast but exercises the real path."""
    p = tmp_path / "portfolio.csv"
    pd.DataFrame([
        {"name_en": "China Life", "name_zh": "中国人寿", "h_ticker": "2628.HK",
         "a_ticker": "601628.SS", "sector": "Financials",
         "classification": config.FOCUS, "owned": 1, "focus": 1, "weight": 6.0,
         "shares_held": "", "ref_premium_jul2024": 88.0, "notes": ""},
        {"name_en": "Bank of China", "name_zh": "中国银行", "h_ticker": "3988.HK",
         "a_ticker": "601988.SS", "sector": "Financials",
         "classification": config.WATCHLIST, "owned": 0, "focus": 0,
         "weight": "", "shares_held": "", "ref_premium_jul2024": 45.0,
         "notes": ""},
    ]).to_csv(p, index=False)
    return p


def test_module_is_importable():
    """The exact module Railway starts must import without error."""
    mod = importlib.import_module("ahmon.cron_refresh")
    assert hasattr(mod, "main") and hasattr(mod, "refresh")


def test_refresh_populates_db_and_labels_sample(tmp_path):
    db_path = tmp_path / "ahmon.db"
    n = cron_refresh.refresh(db_path=db_path,
                             portfolio_csv=_tiny_portfolio(tmp_path))
    assert n == 2
    conn = db.connect(db_path)
    try:
        # data landed
        assert conn.execute("SELECT COUNT(*) FROM daily_obs").fetchone()[0] > 0
        # ...and is never presented as live (CLAUDE.md rule)
        statuses = {r["status"] for r in
                    conn.execute("SELECT status FROM source_health")}
        assert statuses == {"sample"}
    finally:
        conn.close()


def test_refresh_is_idempotent(tmp_path):
    """Two runs must not double rows — the cron fires repeatedly."""
    db_path = tmp_path / "ahmon.db"
    csv = _tiny_portfolio(tmp_path)
    cron_refresh.refresh(db_path=db_path, portfolio_csv=csv)
    conn = db.connect(db_path)
    first = conn.execute("SELECT COUNT(*) FROM daily_obs").fetchone()[0]
    conn.close()
    cron_refresh.refresh(db_path=db_path, portfolio_csv=csv)
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM daily_obs").fetchone()[0] == first
    finally:
        conn.close()


def test_main_returns_zero(monkeypatch, tmp_path):
    """main() must exit 0 on success so Railway sees a healthy run."""
    monkeypatch.setattr(cron_refresh.sample_data, "build",
                        lambda *a, **k: None)
    monkeypatch.setattr(cron_refresh, "refresh", lambda *a, **k: 45)
    assert cron_refresh.main([]) == 0


def test_main_returns_nonzero_on_failure(monkeypatch):
    """A refresh failure must surface as a non-zero exit, not a silent pass."""
    def boom(*a, **k):
        raise RuntimeError("source down")
    monkeypatch.setattr(cron_refresh, "refresh", boom)
    assert cron_refresh.main([]) == 1


def test_runs_as_python_dash_m():
    """End-to-end: the literal Railway command exits 0."""
    r = subprocess.run([sys.executable, "-m", "ahmon.cron_refresh"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
