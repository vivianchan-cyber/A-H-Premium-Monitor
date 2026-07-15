"""Historical backfill: multi-year daily A/H closes + FX so the 5-year
valuation metrics (median premium, percentile, gap to median) compute from
real history instead of showing '—'.

Sources (deliberate, recorded in source_health — never a silent
substitution):
- Prices: Tencent daily klines via akshare (`stock_zh_ah_daily` for the H
  leg, `stock_zh_a_hist_tx` for the A leg), unadjusted closes — the premium
  compares actual traded prices, so adjusted series would be wrong.
  (Eastmoney's history API returns empty replies from some networks; see
  docs/source_notes.md §3a.)
- FX: Yahoo CNYHKD=X daily history, band/currency-checked, forward-filled
  across FX holidays (a premium needs the rate on each equity trading day).
- Verification: for a sample of companies the backfilled H closes are
  compared against Yahoo's independent history; median divergence > 2 %
  fires a `history_verification` alert and marks the backfill degraded.

Rules preserved: premium is always computed by us (premium_src stays NULL —
no source reports historical premiums); rows are labelled quality='eod';
today's date is left to the live spot refresh; upserts are idempotent;
a database containing sample rows is refused.

Run:  python -m ahmon.backfill                 # full universe, resumable
      python -m ahmon.backfill --tiers focus   # highest tier first
"""

from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

from . import calc, config, db
from .refresh import _guard_no_sample

HEALTH_SOURCE = "history backfill (Tencent prices + Yahoo FX)"
VERIFY_TOL_PCT = 2.0
# Resume heuristic: a company with at least this many pre-today rows is
# considered already backfilled (use --force to redo it).
MIN_DONE_ROWS = 250
# Polite spacing between companies (each takes ~12 Tencent calls already).
COMPANY_GAP_S = 1.0

TIER_ORDER = [config.FOCUS, config.PORTFOLIO, config.WATCHLIST, config.OTHER]
TIER_KEYS = {"focus": config.FOCUS, "portfolio": config.PORTFOLIO,
             "watchlist": config.WATCHLIST, "other": config.OTHER}


def build_history_rows(hist: pd.DataFrame, fx: pd.Series, company_id: int,
                       now: str, today: str) -> tuple[list[dict], int]:
    """Join price history with the FX series and compute the premium for
    each day. Pure function (offline-testable). Returns (rows, skipped):
    days before FX history starts, on/after `today`, or with an
    out-of-band rate are skipped, never guessed."""
    fx = fx.sort_index()
    fx_daily = fx.reindex(pd.date_range(fx.index.min(), fx.index.max(),
                                        freq="D")).ffill()
    rows, skipped = [], 0
    for _, r in hist.iterrows():
        d = pd.Timestamp(r["date"])
        if d.strftime("%Y-%m-%d") >= today or d not in fx_daily.index \
                or pd.isna(fx_daily[d]):
            skipped += 1
            continue
        rate = float(fx_daily[d])
        try:
            prem = calc.a_share_premium(float(r["a_close"]),
                                        float(r["h_close"]), rate)
        except ValueError:
            skipped += 1
            continue
        rows.append({
            "company_id": company_id, "date": d.strftime("%Y-%m-%d"),
            "a_close": float(r["a_close"]), "h_close": float(r["h_close"]),
            "fx": rate, "premium_calc": prem, "premium_src": None,
            "a_div_yield": None, "h_div_yield": None,
            "quality": "eod", "updated_at": now,
        })
    return rows, skipped


def verify_against_yahoo(hist: pd.DataFrame, yahoo_h: pd.Series,
                         n_days: int = 250) -> float | None:
    """Median |Tencent − Yahoo| divergence (%) of H closes over the last
    n_days common dates; None when there is no overlap."""
    t = pd.Series(hist["h_close"].values,
                  index=pd.DatetimeIndex(hist["date"]))
    common = t.index.intersection(yahoo_h.index)[-n_days:]
    if len(common) == 0:
        return None
    div = (t[common] / yahoo_h[common] - 1.0).abs() * 100.0
    return float(div.median())


def backfill(conn, years: int = 6, tiers: list[str] | None = None,
             limit: int = 0, verify_n: int = 3, force: bool = False,
             ak_source=None, yahoo_source=None, log=print) -> dict:
    if ak_source is None:
        from .sources.akshare_src import AkshareSource
        ak_source = AkshareSource()
    if yahoo_source is None:
        from .sources.yahoo import YahooSource
        yahoo_source = YahooSource()

    _guard_no_sample(conn)
    today = pd.Timestamp.now(tz=config.TZ).strftime("%Y-%m-%d")
    start_year = pd.Timestamp.now(tz=config.TZ).year - years

    fx = yahoo_source.fetch_fx_history(years + 1)
    log(f"FX history: {len(fx)} days "
        f"({fx.index.min():%Y-%m-%d} → {fx.index.max():%Y-%m-%d})")

    comps = db.companies_df(conn)
    comps["classification"] = comps["classification"].fillna(config.OTHER)
    comps["tier_rank"] = comps["classification"].map(
        {t: i for i, t in enumerate(TIER_ORDER)}).fillna(len(TIER_ORDER))
    comps = comps.sort_values(["tier_rank", "name_en"])
    if tiers:
        wanted = [TIER_KEYS[t] for t in tiers]
        comps = comps[comps["classification"].isin(wanted)]
    if limit:
        comps = comps.head(limit)

    done = failed = skipped_companies = 0
    total_rows = 0
    failures: list[str] = []
    verified: list[tuple[str, float]] = []
    now = db.now_iso()

    for _, c in comps.iterrows():
        if not force:
            n = conn.execute(
                "SELECT COUNT(*) FROM daily_obs WHERE company_id=? AND date<?",
                (int(c["id"]), today)).fetchone()[0]
            if n >= MIN_DONE_ROWS:
                skipped_companies += 1
                continue
        try:
            hist = ak_source.fetch_history(c["h_ticker"], c["a_ticker"],
                                           start_year)
            rows, row_skips = build_history_rows(hist, fx, int(c["id"]),
                                                 now, today)
            db.insert_daily(conn, rows)
            total_rows += len(rows)
            done += 1
            log(f"  {c['name_en']} ({c['h_ticker']}): {len(rows)} days "
                f"({rows[0]['date']} → {rows[-1]['date']}, "
                f"{row_skips} skipped)" if rows else
                f"  {c['name_en']} ({c['h_ticker']}): no usable history")
            if len(verified) < verify_n and rows:
                yh = yahoo_source.fetch_h_history(c["h_ticker"], years)
                div = verify_against_yahoo(hist, yh)
                if div is not None:
                    verified.append((c["h_ticker"], div))
                    if div > VERIFY_TOL_PCT:
                        db.log_alert(
                            conn, "history_verification",
                            f"backfilled H closes diverge from Yahoo by "
                            f"{div:.2f}% (median, threshold "
                            f"{VERIFY_TOL_PCT}%)", str(c["name_en"]),
                            "serious")
        except Exception as e:      # noqa: BLE001 — reported, not hidden
            failed += 1
            failures.append(f"{c['h_ticker']}: {type(e).__name__}: {e}")
            log(f"  {c['name_en']} ({c['h_ticker']}): FAILED — {e}")
        time.sleep(COMPANY_GAP_S)

    worst = max((d for _, d in verified), default=None)
    status = "ok"
    if failed or (worst is not None and worst > VERIFY_TOL_PCT):
        status = "degraded"
    msg = (f"backfilled {done} companies ({total_rows} rows, ≤{years}y, "
           f"quality=eod), {skipped_companies} already done, {failed} failed"
           + (f"; Yahoo check on {len(verified)} names: worst median "
              f"divergence {worst:.2f}%" if worst is not None else ""))
    db.set_source_health(conn, HEALTH_SOURCE, status, msg)
    return {"done": done, "failed": failed, "failures": failures,
            "skipped_companies": skipped_companies, "rows": total_rows,
            "verified": verified, "status": status}


def main(argv=None):
    p = argparse.ArgumentParser(description="A–H Monitor history backfill")
    p.add_argument("--db", default=str(config.LIVE_DB_PATH))
    p.add_argument("--years", type=int, default=6,
                   help="calendar years of history (default 6 so the "
                        "1260-observation 5y window is fully populated)")
    p.add_argument("--tiers", default=None,
                   help="comma list of focus,portfolio,watchlist,other "
                        "(default: all, highest tier first)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--verify-n", type=int, default=3)
    p.add_argument("--force", action="store_true",
                   help="re-fetch companies that already have history")
    args = p.parse_args(argv)

    conn = db.connect(args.db)
    tiers = args.tiers.split(",") if args.tiers else None
    s = backfill(conn, years=args.years, tiers=tiers, limit=args.limit,
                 verify_n=args.verify_n, force=args.force)
    print(f"\nDone: {s['done']} companies backfilled ({s['rows']} rows), "
          f"{s['skipped_companies']} already done, {s['failed']} failed "
          f"→ status {s['status']}")
    for f in s["failures"]:
        print(f"  failure: {f}")
    conn.close()
    return 0 if not s["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
