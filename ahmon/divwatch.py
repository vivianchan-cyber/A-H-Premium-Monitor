"""Yield-based top-up monitor for the 20 focus holdings.

Works out of the box: each yield-based name's DPS defaults to its
**trailing 12-month declared ordinary dividends** (fetched from Yahoo's
dividend events into dps_events), so yields, top-up prices, distances
and statuses appear without any manual input. A **manually approved /
normalised DPS** (approved_dps table) is an optional override — it wins
over the trailing figure wherever set, and the *DPS basis* column shows
which one each row uses. This matters most for cyclicals, where a
peak-cycle trailing payout overstates the sustainable yield.

Targets: **stable 5.0%**, **cyclical 6.0%** (wider because cyclical
earnings and dividends move with commodity prices / the economic
cycle). `not_yield_based` names (China Life, BYD, SMIC) are listed but
generate no signal.

Arithmetic:
    top-up price = DPS / target yield
    current yield = DPS / price
    distance      = (price / top-up price − 1) × 100

Statuses:
    WAIT              price more than 5% above the top-up price
    NEAR RANGE        price within 5% above the top-up price
    IN RANGE — REVIEW price at or below the top-up price: the yield
                      threshold is met, but dividend sustainability
                      still requires the owner's review
    DATA REVIEW       yield-based name whose DPS cannot be established
    —                 not yield-based (listed, no signal)

Caveat, stated rather than hidden: Yahoo's dividend events do not label
special dividends, so an unusual one-off payout inflates the trailing
figure — exactly the case the manual override exists for.

CLI:
  python -m ahmon.divwatch seed        # watchlist from CSV
  python -m ahmon.divwatch fetch-dps   # declared dividends from Yahoo
  python -m ahmon.divwatch set-dps 941.HK 4.60 [note]
  python -m ahmon.divwatch show
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, db

WATCHLIST_CSV = config.CONFIG_DIR / "div_watchlist.csv"

TARGET_YIELD = {"stable": 5.0, "cyclical": 6.0}      # percent
PROFILES = ("stable", "cyclical", "not_yield_based")
PROFILE_LABEL = {"stable": "Stable payer", "cyclical": "Cyclical payer",
                 "not_yield_based": "Not yield-based"}
NEAR_BAND_PCT = 5.0
TRAILING_WINDOW_DAYS = 365

BASIS_MANUAL = "Manual normalised DPS"
BASIS_TRAILING = "Trailing 12m DPS"
BASIS_TRAILING_FLAG = "Trailing DPS — review special"

ST_REVIEW = "IN RANGE — REVIEW"
ST_NEAR = "NEAR RANGE"
ST_WAIT = "WAIT"
ST_DATA = "DATA REVIEW"

STATUS_ORDER = {ST_REVIEW: 0, ST_NEAR: 1, ST_WAIT: 2, ST_DATA: 3, "—": 4}

# A trailing total this far above the previous 12 months is the shape a
# special / one-off payment leaves in the history.
UNUSUAL_PAYOUT_RATIO = 1.6

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


def refresh_dps(conn, yahoo_source=None) -> dict:
    """Fetch declared dividends for every watch name into dps_events
    (idempotent). A name with zero dividends gets no rows — that reads
    as DATA REVIEW in the table, never as a silent blank."""
    if yahoo_source is None:
        from .sources.yahoo import YahooSource
        yahoo_source = YahooSource()
    watch = db.watchlist_df(conn)
    stored, failed = 0, []
    for t in watch["ticker"]:
        try:
            divs = yahoo_source.fetch_dividends(t)
        except Exception as e:                    # noqa: BLE001 per-ticker
            failed.append(f"{t}: {type(e).__name__}: {e}")
            continue
        if divs.empty:
            continue
        rows = [{"ticker": t, "ex_date": r["ex_date"],
                 "amount_hkd": float(r["amount_hkd"]), "special": 0,
                 "source": "yahoo:events"} for _, r in divs.iterrows()]
        db.insert_dps_events(conn, rows)
        stored += len(rows)
    db.set_source_health(
        conn, "Dividend history (Yahoo events)",
        "ok" if not failed else "degraded",
        f"{stored} declared dividends stored for {len(watch)} watch "
        f"names; failures: {failed or 'none'}. Yahoo does not label "
        "special dividends — normalise via the manual override where "
        "a payout looks one-off.")
    return {"stored": stored, "failed": failed}


def trailing_dps(events: pd.DataFrame, asof: str) -> float | None:
    """Sum of declared ordinary dividends with ex-date in the 365 days
    up to `asof`; None if there are none."""
    if events is None or events.empty:
        return None
    ev = events[events["special"] == 0]
    lo = pd.Timestamp(asof) - pd.Timedelta(days=TRAILING_WINDOW_DAYS)
    ev = ev[(pd.to_datetime(ev["ex_date"]) > lo)
            & (pd.to_datetime(ev["ex_date"]) <= pd.Timestamp(asof))]
    return float(ev["amount_hkd"].sum()) if len(ev) else None


def topup_price(dps: float, target_yield_pct: float) -> float:
    """Price at which the DPS yields exactly the target."""
    return dps / (target_yield_pct / 100.0)


# --------------------------------------------------- what-if calculator

def required_price(annual_dps: float | None,
                   target_yield_pct: float) -> float | None:
    """Share price at which `annual_dps` yields exactly
    `target_yield_pct` (a percentage, e.g. 5.0 → 5%). None when the
    DPS is unavailable; zero/negative targets are rejected outright.
    Purely a what-if computation — never touches stored thresholds."""
    if target_yield_pct is None or pd.isna(target_yield_pct) \
            or target_yield_pct <= 0:
        raise ValueError("target yield must be a positive percentage")
    if annual_dps is None or pd.isna(annual_dps) or annual_dps <= 0:
        return None
    return annual_dps / (target_yield_pct / 100.0)


def position_vs_required(current_price: float,
                         req_price: float) -> tuple[str, float]:
    """Plain-language position of the current price against the
    required price: ('23.5% below target price', -23.5). The text
    never shows a bare negative number."""
    pct = (current_price / req_price - 1.0) * 100.0
    if abs(pct) < 0.05:
        return "At target price", pct
    side = "below" if pct < 0 else "above"
    return f"{abs(pct):.1f}% {side} target price", pct


def classify_topup(price: float, topup: float) -> tuple[str, float]:
    """(status, distance%). distance = how far the price sits above the
    top-up price; 0 or negative means at/below it."""
    dist = (price / topup - 1.0) * 100.0
    eps = 1e-9                       # float-safe boundary handling
    if dist <= eps:
        return ST_REVIEW, dist
    if dist <= NEAR_BAND_PCT + eps:
        return ST_NEAR, dist
    return ST_WAIT, dist


def position_text(dist: float | None) -> str | None:
    """Plain-language position vs the top-up price: '23.5% below' /
    '2.4% above' / 'at top-up price' — below is where buying happens,
    so it is never shown as a negative number."""
    if dist is None or pd.isna(dist):
        return None
    if abs(dist) < 0.05:
        return "at top-up price"
    return f"{abs(dist):.1f}% {'below' if dist < 0 else 'above'}"


def unusual_payout(events: pd.DataFrame, asof: str) -> bool:
    """True when the trailing 12m total is more than
    UNUSUAL_PAYOUT_RATIO x the previous 12m total — likely a special or
    unusual payment inflating the trailing figure. Needs two years of
    history; a name without a prior-year record is never flagged."""
    if events is None or events.empty:
        return False
    ev = events[events["special"] == 0]
    d = pd.to_datetime(ev["ex_date"])
    now = pd.Timestamp(asof)
    cur = float(ev[(d > now - pd.Timedelta(days=365))
                   & (d <= now)]["amount_hkd"].sum())
    prev = float(ev[(d > now - pd.Timedelta(days=730))
                    & (d <= now - pd.Timedelta(days=365))]
                 ["amount_hkd"].sum())
    return prev > 0 and cur > UNUSUAL_PAYOUT_RATIO * prev


def build_topup_table(conn) -> pd.DataFrame:
    """One row per holding. DPS precedence: manual override, else
    trailing 12m declared dividends, else DATA REVIEW. Sorted
    TOP-UP REVIEW → NEAR TOP-UP → WAIT (each by distance), then
    DATA REVIEW and the not-yield-based names."""
    watch = db.watchlist_df(conn)
    if watch.empty:
        return pd.DataFrame()
    manual = db.approved_dps_df(conn).set_index("ticker")
    comps = db.companies_df(conn).set_index("h_ticker")
    all_events = db.dps_events_df(conn)
    today = db.now_iso()[:10]

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
                asof = str(obs.iloc[0]["updated_at"])[:16].replace("T", " ")
        target = TARGET_YIELD.get(profile)

        ev = all_events[all_events["ticker"] == t] \
            if not all_events.empty else all_events
        dps, basis = None, None
        if t in manual.index:
            dps, basis = float(manual.loc[t, "dps_hkd"]), BASIS_MANUAL
        else:
            dps = trailing_dps(ev, today)
            if dps is not None:
                basis = BASIS_TRAILING_FLAG if unusual_payout(ev, today) \
                    else BASIS_TRAILING

        status, dist, topup, cur_yield = "—", None, None, None
        if target is not None:                      # yield-based name
            if dps is None or price is None:
                status = ST_DATA
            else:
                topup = topup_price(dps, target)
                cur_yield = dps / price * 100.0
                status, dist = classify_topup(price, topup)
        out.append({
            "Company": w["name"], "Ticker": t,
            "Classification": PROFILE_LABEL.get(profile, profile),
            "Price (HKD)": price,
            "DPS (HKD)": dps,
            "DPS basis": basis if target is not None else None,
            "Current yield (%)": cur_yield,
            "Target yield (%)": target,
            "Top-up price (HKD)": topup,
            "Position vs top-up price": position_text(dist),
            "Status": status,
            "As of": asof,
            "_dist": dist,
        })
    df = pd.DataFrame(out)
    for c in ["Price (HKD)", "DPS (HKD)", "Current yield (%)",
              "Target yield (%)", "Top-up price (HKD)"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")   # None → NaN → "—"
    for c in ["DPS basis", "Position vs top-up price", "As of"]:
        df[c] = df[c].fillna("—")
    df["_s"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(["_s", "_dist"], na_position="last") \
        .drop(columns=["_s", "_dist"])
    return df.reset_index(drop=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Yield-based top-up monitor")
    p.add_argument("command", choices=["seed", "fetch-dps", "set-dps",
                                       "show"])
    p.add_argument("args", nargs="*")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)
    conn = db.connect(a.db)
    try:
        if a.command == "seed":
            r = seed_watchlist(conn)
            print(f"watchlist: {r['total']} names "
                  f"(+{len(r['added'])} new: {r['added'] or '—'})")
        elif a.command == "fetch-dps":
            r = refresh_dps(conn)
            print(f"{r['stored']} declared dividends stored; "
                  f"failures: {r['failed'] or 'none'}")
        elif a.command == "set-dps":
            if len(a.args) < 2:
                p.error("set-dps TICKER AMOUNT [note...]")
            ticker, amount = a.args[0], float(a.args[1])
            note = " ".join(a.args[2:]) or None
            db.set_approved_dps(conn, ticker, amount, note, source="cli")
            print(f"{ticker}: manual normalised DPS set to {amount} HKD")
        elif a.command == "show":
            t = build_topup_table(conn)
            print(t.to_string(index=False) if not t.empty
                  else "watchlist empty — run seed first")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
