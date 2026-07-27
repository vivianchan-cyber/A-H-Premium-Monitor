"""The Railway scheduled service starts with ``python -m ahmon.cron_refresh``.

These tests are the contract that keeps that command safe: it must import,
run to a clean (exit 0) finish with no live source, and — crucially — never
touch real data. Sample generation is a development-only escape hatch behind
``ALLOW_SAMPLE_REFRESH`` and may only ever populate an empty database.
"""

import importlib
import subprocess
import sys

import pandas as pd
import pytest

from ahmon import config, cron_refresh, db


def _tiny_portfolio(tmp_path):
    """A two-row portfolio so a sample build is fast but real."""
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


def _seed_production_row(db_path):
    """Put one real-looking observation + classification into a DB, as if the
    dashboard/live pipeline had written it."""
    conn = db.connect(db_path)
    cid = db.upsert_company(conn, "China Life", "2628.HK", "601628.SS",
                            "Financials", "中国人寿")
    db.set_classification(conn, cid, config.FOCUS, source="ui")
    db.insert_daily(conn, [{
        "company_id": cid, "date": "2026-07-14", "a_close": 38.5,
        "h_close": 16.2, "fx": 1.082, "premium_calc": 157.1,
        "premium_src": 157.0, "a_div_yield": 1.5, "h_div_yield": 3.4,
        "quality": "ok", "updated_at": db.now_iso()}])
    conn.close()
    return cid


# --------------------------------------------------------------- basic contract

def test_module_is_importable():
    """The exact module Railway starts must import without error."""
    mod = importlib.import_module("ahmon.cron_refresh")
    assert hasattr(mod, "main") and hasattr(mod, "refresh")


# ------------------------------------------------------- default run is inert

def test_default_run_makes_no_sample_writes(tmp_path, monkeypatch, capsys):
    """No flag, no live source → skip, write nothing, exit 0."""
    monkeypatch.delenv("ALLOW_SAMPLE_REFRESH", raising=False)
    db_path = tmp_path / "ahmon.db"
    # Guard: build() must not be invoked on a default cron run.
    monkeypatch.setattr(cron_refresh.sample_data, "build",
                        lambda *a, **k: pytest.fail("build() must not run"))

    res = cron_refresh.refresh(db_path=db_path)

    assert res["action"] == "skipped"
    assert cron_refresh.SKIP_MESSAGE in capsys.readouterr().out
    assert not db_path.exists()          # nothing written, DB not even created


def test_default_main_exits_zero_when_skipped(tmp_path, monkeypatch):
    """The command still exits 0 when the refresh is skipped."""
    monkeypatch.delenv("ALLOW_SAMPLE_REFRESH", raising=False)
    monkeypatch.setattr(cron_refresh, "refresh", lambda: {"action": "skipped"})
    assert cron_refresh.main([]) == 0


def test_runs_as_python_dash_m(tmp_path):
    """End-to-end: the literal Railway command exits 0 with the skip message
    and the default (unset) environment."""
    env = {k: v for k, v in _clean_env().items()}
    env.pop("ALLOW_SAMPLE_REFRESH", None)
    r = subprocess.run([sys.executable, "-m", "ahmon.cron_refresh"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert cron_refresh.SKIP_MESSAGE in r.stdout


def _clean_env():
    import os
    return dict(os.environ)


# ---------------------------------------------- existing data is protected

def test_existing_observations_unchanged_even_with_flag(tmp_path, monkeypatch):
    """ALLOW_SAMPLE_REFRESH=true must NOT overwrite existing observations."""
    monkeypatch.setenv("ALLOW_SAMPLE_REFRESH", "true")
    db_path = tmp_path / "ahmon.db"
    cid = _seed_production_row(db_path)

    res = cron_refresh.refresh(db_path=db_path,
                               portfolio_csv=_tiny_portfolio(tmp_path))

    assert res["action"] == "sample_skipped_nonempty"
    conn = db.connect(db_path)
    try:
        rows = db.daily_df(conn, cid)
        assert len(rows) == 1                       # not rebuilt
        assert rows.iloc[0]["a_close"] == 38.5      # original value intact
        assert rows.iloc[0]["quality"] == "ok"      # not relabelled 'sample'
    finally:
        conn.close()


def test_existing_classifications_unchanged_even_with_flag(tmp_path,
                                                           monkeypatch):
    """A user's dashboard classification must never be reseeded/reverted."""
    monkeypatch.setenv("ALLOW_SAMPLE_REFRESH", "true")
    db_path = tmp_path / "ahmon.db"
    cid = _seed_production_row(db_path)
    # move it to WATCHLIST as a dashboard edit would
    conn = db.connect(db_path)
    db.set_classification(conn, cid, config.WATCHLIST, source="ui")
    conn.close()

    cron_refresh.refresh(db_path=db_path,
                         portfolio_csv=_tiny_portfolio(tmp_path))

    conn = db.connect(db_path)
    try:
        row = db.companies_df(conn).iloc[0]
        assert row["classification"] == config.WATCHLIST   # not reverted to CSV
    finally:
        conn.close()


# ------------------------------------------ sample generation is opt-in only

def test_sample_generation_requires_flag(tmp_path, monkeypatch):
    """Without the flag: no build. With the flag on an empty DB: build runs."""
    db_path = tmp_path / "ahmon.db"
    csv = _tiny_portfolio(tmp_path)

    monkeypatch.delenv("ALLOW_SAMPLE_REFRESH", raising=False)
    assert cron_refresh.refresh(db_path=db_path,
                                portfolio_csv=csv)["action"] == "skipped"
    assert not db_path.exists()

    monkeypatch.setenv("ALLOW_SAMPLE_REFRESH", "true")
    res = cron_refresh.refresh(db_path=db_path, portfolio_csv=csv)
    assert res["action"] == "sample_built" and res["companies"] == 2
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM daily_obs").fetchone()[0] > 0
        statuses = {r["status"] for r in
                    conn.execute("SELECT status FROM source_health")}
        assert statuses == {"sample"}     # generated data still labelled sample
    finally:
        conn.close()


def test_sample_build_seeds_classifications_only_when_empty(tmp_path,
                                                           monkeypatch):
    """On an empty DB the enabled sample build seeds classifications; on a
    populated DB it is refused (covered above) — so seeding is empty-only."""
    monkeypatch.setenv("ALLOW_SAMPLE_REFRESH", "true")
    db_path = tmp_path / "ahmon.db"
    res = cron_refresh.refresh(db_path=db_path,
                               portfolio_csv=_tiny_portfolio(tmp_path))
    assert res["action"] == "sample_built"
    conn = db.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM classifications").fetchone()[0] == 2
    finally:
        conn.close()


# --------------------------------------------------------- failure surfaces

def test_main_returns_nonzero_on_failure(monkeypatch):
    """A refresh that raises must surface as a non-zero exit."""
    def boom(*a, **k):
        raise RuntimeError("source down")
    monkeypatch.setattr(cron_refresh, "refresh", boom)
    assert cron_refresh.main([]) == 1
