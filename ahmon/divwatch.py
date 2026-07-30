"""Yield-based top-up monitor for the 20 focus holdings.

Simplified per the owner's spec (supersedes the earlier watcher):

- Two dividend classifications with target yields — **stable 5.0%**,
  **cyclical 6.0%** (wider because cyclical earnings and dividends move
  with commodity prices / the economic cycle).
- `not_yield_based` names (China Life, BYD, SMIC) are shown on the page
  but generate no yield-based signal.
- The annual DPS is **manually approved** (approved_dps table, editable
  from the page or `python -m ahmon.divwatch set-dps`). For cyclicals a
  *normalised* DPS should be approved rather than the latest trailing
  payout, because a peak-cycle dividend overstates the sustainable
  yield. Nothing is computed from vendor yields or dividend feeds.

Core arithmetic:
    implied top-up price = approved DPS / target yield
    current yield        = approved DPS / current price
    distance             = (price / top-up price − 1) × 100

Statuses:
    WAIT           price more than 5% above the top-up price
    NEAR TOP-UP    price within 5% above the top-up price
    TOP-UP REVIEW  price at or below the top-up price
    SET DPS        yield-based name without an approved DPS yet —
                   explicit, never a silent blank (small spec addition)

Prices/timestamps come from the existing A-H feed (daily_obs H close):
all 20 holdings are dual-listed, so no extra price source is needed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config, db

WATCHLIST_CSV = config.CONFIG_DIR / "div_watchlist.csv"

TARGET_YIELD = {"stable": 5.0, "cyclical": 6.0}      # percent
PROFILES = ("stable", "cyclical", "not_yield_based")
PROFILE_LABEL = {"stable": "Stable payer", "cyclical": "Cyclical payer",
                 "not_yield_based": "Not yield-based"}
NEAR_BAND_PCT = 5.0

STATUS_ORDER = {"TOP-UP REVIEW": 0, "NEAR TOP-UP": 1, "WAIT": 2,
                "SET DPS": 3, "—": 4}

# Profile names stored by earlier versions of the watchlist. The table
# must render from whatever the database holds — a re-seed fixes the
# stored values, but rendering never assumes it already happened.
LEGACY_PROFILE = {"no_dividend": "not_yield_based"}


def seed_watchlist(conn, csv_path: str | Path | None = None) -> dict:
    """Idempotent: adds/updates names and profiles, never drops one."""
    path = Path(csv_path) if csv_path else WATCHLIST_CSV
    rows = pd.read_csv(path)
    bad = sorted(set(rows["profile"]) - set(PROFILES))
    if bad:
        raise ValueError(f"unknown profiles in {path.name}: {bad}")
    before = set(db.watchlist_df(conn)["ticker"])
    db.upsert_watchlist(conn, rows.to_dict("records"))
    after = db.watchlist_df(conn)
    return {"added": sorted(set(after["ticker"]) - before),
            "total": len(after)}


def topup_price(dps: float, target_yield_pct: float) -> float:
    """Price at which the approved DPS yields exactly the target."""
    return dps / (target_yield_pct / 100.0)


def classify_topup(price: float, topup: float) -> tuple[str, float]:
    """(status, distance%). distance = how far the price sits above the
    top-up price; 0 or negative means at/below it."""
    dist = (price / topup - 1.0) * 100.0
    eps = 1e-9                       # float-safe boundary handling
    if dist <= eps:
        return "TOP-UP REVIEW", dist
    if dist <= NEAR_BAND_PCT + eps:
        return "NEAR TOP-UP", dist
    return "WAIT", dist


def build_topup_table(conn) -> pd.DataFrame:
    """One row per holding: price from the A-H feed, DPS from the
    manually approved table, statuses per the top-up rule. Sorted
    TOP-UP REVIEW → NEAR TOP-UP → WAIT (each by distance), then the
    unset / not-yield-based names."""
    watch = db.watchlist_df(conn)
    if watch.empty:
        return pd.DataFrame()
    dps_map = db.approved_dps_df(conn).set_index("ticker")
    comps = db.companies_df(conn).set_index("h_ticker")

    out = []
    for _, w in watch.iterrows():
        t = w["ticker"]
        profile = LEGACY_PROFILE.get(w["profile"], w["profile"])
        price = asof = None
        if t in comps.index:
            obs = conn.read_df(
                """SELECT h_close, updated_at FROM daily_obs
                   WHERE company_id=:c ORDER BY date DESC LIMIT 1""",
                {"c": int(comps.loc[t, "id"])})
            if not obs.empty and obs.iloc[0]["h_close"]:
                price = float(obs.iloc[0]["h_close"])
                asof = str(obs.iloc[0]["updated_at"])
        dps = (float(dps_map.loc[t, "dps_hkd"])
               if t in dps_map.index else None)
        target = TARGET_YIELD.get(profile)

        status, dist, topup, cur_yield = "—", None, None, None
        if target is not None:                      # yield-based name
            if dps is None or price is None:
                status = "SET DPS"
            else:
                topup = topup_price(dps, target)
                cur_yield = dps / price * 100.0
                status, dist = classify_topup(price, topup)
        out.append({
            "Company": w["name"], "Ticker": t,
            "Classification": PROFILE_LABEL.get(profile, profile),
            "Price (HKD)": price,
            "Approved DPS (HKD)": dps,
            "Current yield (%)": cur_yield,
            "Target yield (%)": target,
            "Top-up price (HKD)": topup,
            "Distance to top-up (%)": dist,
            "Status": status,
            "As of": asof,
        })
    df = pd.DataFrame(out)
    df["_s"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(["_s", "Distance to top-up (%)"],
                        na_position="last").drop(columns="_s")
    return df.reset_index(drop=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Yield-based top-up monitor")
    p.add_argument("command", choices=["seed", "set-dps", "show"])
    p.add_argument("args", nargs="*")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)
    conn = db.connect(a.db)
    try:
        if a.command == "seed":
            r = seed_watchlist(conn)
            print(f"watchlist: {r['total']} names "
                  f"(+{len(r['added'])} new: {r['added'] or '—'})")
        elif a.command == "set-dps":
            if len(a.args) < 2:
                p.error("set-dps TICKER AMOUNT [note...]")
            ticker, amount = a.args[0], float(a.args[1])
            note = " ".join(a.args[2:]) or None
            db.set_approved_dps(conn, ticker, amount, note, source="cli")
            print(f"{ticker}: approved DPS set to {amount} HKD")
        elif a.command == "show":
            t = build_topup_table(conn)
            print(t.to_string(index=False) if not t.empty
                  else "watchlist empty — run seed first")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
