"""Dividend buy-level watch — HK dividend book on a yield threshold.

Core rule: a name is in buy range when trailing yield >= its threshold
(5.0% stable payers, 6.0% cyclical — the extra 1pp buffers a DPS cut so
the position still clears the >4.8% mandate). The value-trap guard then
checks the FORWARD yield: a threshold that fired only because the price
collapsed ahead of an expected cut is not a buy.

Statuses:
- BUY       trailing >= threshold AND forward >= threshold
- CHECK     trailing >= threshold BUT forward < threshold, or the
            consensus DPS is missing / older than 90 days
- WAIT      trailing < threshold
- NO POLICY profile 'no_dividend' (monitored, not evaluated; if it
            starts paying, it is promoted to WAIT and flagged)
- NO DATA   no price/yield stored yet — shown explicitly, because a
            silently missing name would read as "not in range"
            (NO DATA is an addition to the spec for exactly that reason)

Data notes (repo conventions kept, per the spec's own instruction):
- Storage goes through ahmon/db.py only. Tables: div_watchlist,
  dps_events (declared dividends, specials stored separately),
  yield_daily (one row per ticker per day).
- The repo's existing Eastmoney dividend-yield fields stay the VENDOR
  yield; when trailing DPS history is not yet loaded for a name, the
  vendor yield is used as the interim trailing yield and the row is
  labelled source='vendor_yield' — visibly, never silently.
- |computed − vendor| > 0.3pp sets the divergence flag.

CLI:
  python -m ahmon.divwatch seed                  # watchlist from CSV
  python -m ahmon.divwatch import-dps FILE.csv   # ticker,ex_date,amount_hkd[,special]
  python -m ahmon.divwatch import-yields FILE.csv# manual price/DPS rows
  python -m ahmon.divwatch bridge               # interim rows for A-H names
  python -m ahmon.divwatch show                 # print today's table
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config, db

WATCHLIST_CSV = config.CONFIG_DIR / "div_watchlist.csv"

THRESHOLDS = {"stable": 5.0, "cyclical": 6.0}       # percent
PROFILES = ("stable", "cyclical", "no_dividend")
CONSENSUS_MAX_AGE_DAYS = 90
VENDOR_DIVERGENCE_PP = 0.3
TRAILING_WINDOW_DAYS = 365

STATUS_ORDER = {"BUY": 0, "CHECK": 1, "WAIT": 2, "NO POLICY": 3,
                "NO DATA": 4}


# ------------------------------------------------------------------- seed

def seed_watchlist(conn, csv_path: str | Path | None = None) -> dict:
    """Idempotent: adds/updates names, never drops or deactivates one —
    a name below its threshold is what the watcher exists to monitor."""
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


# ------------------------------------------------------------ calculations

def trailing_dps(events: pd.DataFrame, asof: str) -> float | None:
    """Sum of declared regular dividends with ex-date in the 365 days up
    to `asof`. Specials are excluded by design. None if no events."""
    if events is None or events.empty:
        return None
    ev = events[(events["special"] == 0)]
    lo = (pd.Timestamp(asof) - pd.Timedelta(days=TRAILING_WINDOW_DAYS))
    ev = ev[(pd.to_datetime(ev["ex_date"]) > lo)
            & (pd.to_datetime(ev["ex_date"]) <= pd.Timestamp(asof))]
    if ev.empty:
        return None
    return float(ev["amount_hkd"].sum())


def distance_to_threshold_pct(price, dps, threshold_pct) -> float | None:
    """% price move to the exact threshold price (dps / threshold).
    Negative: the price must fall to get in range. Positive: headroom —
    how far the price could rise before dropping out of range."""
    if price is None or not dps or threshold_pct is None or price <= 0:
        return None
    target = dps / (threshold_pct / 100.0)
    return (target / price - 1.0) * 100.0


def classify(profile: str, trailing_yield, forward_yield,
             consensus_age_days) -> tuple[str, str]:
    """(status, reason). The CHECK branches are the value-trap guard."""
    if profile == "no_dividend":
        if trailing_yield and trailing_yield > 0:
            return ("WAIT", "dividend initiated/resumed — review profile "
                            "(promoted from NO POLICY)")
        return ("NO POLICY", "no dividend policy — monitored only")
    thr = THRESHOLDS[profile]
    if trailing_yield is None or pd.isna(trailing_yield):
        return ("NO DATA", "no price/DPS stored yet")
    if trailing_yield < thr:
        return ("WAIT", f"trailing {trailing_yield:.2f}% below "
                        f"{thr:.1f}% threshold")
    if forward_yield is None or pd.isna(forward_yield):
        return ("CHECK", "in range on trailing DPS, but no forward "
                         "consensus to rule out an expected cut")
    if consensus_age_days is not None and \
            consensus_age_days > CONSENSUS_MAX_AGE_DAYS:
        return ("CHECK", f"consensus DPS is {consensus_age_days:.0f} days "
                         "old (>90) — treat the forward yield as unknown")
    if forward_yield < thr:
        return ("CHECK", f"forward {forward_yield:.2f}% below threshold — "
                         "market expects a cut; trailing yield may be "
                         "fictional")
    return ("BUY", f"trailing {trailing_yield:.2f}% and forward "
                   f"{forward_yield:.2f}% both clear {thr:.1f}%")


def days_in_range(history: pd.DataFrame, threshold_pct: float,
                  events: pd.DataFrame | None = None) -> int:
    """Consecutive most-recent stored days with trailing yield >= the
    threshold. A stored row without trailing_dps is re-derived from the
    declared-dividend history as of that row's date (never from today's
    DPS projected backwards)."""
    if history is None or history.empty:
        return 0
    h = history.sort_values("date")
    n = 0
    for _, r in h[::-1].iterrows():
        ty = _trailing_yield_of_row(r)
        if ty is None and events is not None and not events.empty \
                and r.get("price"):
            dps = trailing_dps(events, str(r["date"]))
            if dps:
                ty = dps / float(r["price"]) * 100.0
        if ty is not None and ty >= threshold_pct:
            n += 1
        else:
            break
    return n


def _trailing_yield_of_row(r) -> float | None:
    """Trailing yield of a yield_daily row: computed DPS first, vendor
    yield as the labelled fallback."""
    price = r.get("price")
    dps = r.get("trailing_dps")
    if price and dps and price > 0:
        return dps / price * 100.0
    v = r.get("vendor_yield")
    if v is not None and not pd.isna(v):
        return float(v)
    return None


# ----------------------------------------------------------- daily bridge

def bridge_from_monitor(conn, date: str | None = None) -> int:
    """Interim rows for watch names that are A-H companies: today's H
    close + the vendor yield already fetched by the equity refresh.
    Labelled source='vendor_yield' so the provenance is never hidden.
    Replaced by the declared-DPS feed in build step 3."""
    date = date or db.now_iso()[:10]
    watch = db.watchlist_df(conn)
    comps = db.companies_df(conn)
    merged = watch.merge(comps, left_on="ticker", right_on="h_ticker",
                         how="inner")
    rows = []
    for _, r in merged.iterrows():
        obs = conn.read_df(
            """SELECT date, h_close, h_div_yield, updated_at FROM daily_obs
               WHERE company_id=:c ORDER BY date DESC LIMIT 1""",
            {"c": int(r["id"])})
        if obs.empty or not obs.iloc[0]["h_close"]:
            continue
        o = obs.iloc[0]
        stale = str(o["date"]) < date
        rows.append({
            "ticker": r["ticker"], "date": date,
            "price": float(o["h_close"]),
            "vendor_yield": None if pd.isna(o["h_div_yield"])
            else float(o["h_div_yield"]),
            "as_of": str(o["updated_at"]),
            "source": "vendor_yield",
            "quality": "stale" if stale else "ok",
        })
    if rows:
        db.upsert_yield_daily(conn, rows)
    return len(rows)


# ------------------------------------------------------------- watch table

def build_watch_table(conn) -> pd.DataFrame:
    """One row per watch name, classified. Sorted BUY → CHECK → WAIT
    (by distance to threshold), then NO POLICY / NO DATA."""
    watch = db.watchlist_df(conn)
    if watch.empty:
        return pd.DataFrame()
    latest = db.latest_yield_rows(conn)
    latest = latest.set_index("ticker") if not latest.empty else None
    all_events = db.dps_events_df(conn)
    history = db.yield_history_df(conn)
    today = db.now_iso()[:10]

    out = []
    for _, w in watch.iterrows():
        t = w["ticker"]
        row = latest.loc[t] if latest is not None and t in latest.index \
            else None
        price = None if row is None else row.get("price")
        asof = None if row is None else (row.get("as_of") or row.get("date"))
        quality = "missing" if row is None else row.get("quality", "ok")

        ev = all_events[all_events["ticker"] == t] if not all_events.empty \
            else all_events
        dps = trailing_dps(ev, today)
        src = None if row is None else row.get("source")
        if dps is None and row is not None:
            dps = row.get("trailing_dps")
        t_yield = (dps / price * 100.0) if (price and dps and price > 0) \
            else None
        if t_yield is None and row is not None:
            t_yield = _trailing_yield_of_row(row)
            if t_yield is not None:
                src = "vendor_yield"

        fwd_dps = None if row is None else row.get("forward_dps_consensus")
        f_yield = (fwd_dps / price * 100.0) \
            if (price and fwd_dps and price > 0) else None
        c_asof = None if row is None else row.get("consensus_asof")
        c_age = None
        if c_asof:
            c_age = (pd.Timestamp(today) - pd.Timestamp(str(c_asof)[:10])).days

        status, reason = classify(w["profile"], t_yield, f_yield, c_age)

        thr = THRESHOLDS.get(w["profile"])
        vendor = None if row is None else row.get("vendor_yield")
        diverges = (t_yield is not None and vendor is not None
                    and not pd.isna(vendor) and src != "vendor_yield"
                    and abs(t_yield - float(vendor)) > VENDOR_DIVERGENCE_PP)
        dps_gap = None
        if dps and fwd_dps:
            dps_gap = (fwd_dps / dps - 1.0) * 100.0

        hist = history[history["ticker"] == t] if not history.empty \
            else history
        out.append({
            "Ticker": t, "Name": w["name"], "Profile": w["profile"],
            "Price (HKD)": price,
            "Trailing yield (%)": t_yield,
            "Forward yield (%)": f_yield,
            "Threshold (%)": thr,
            # DPS route when history is loaded; pure-yield algebra
            # (identical result) when only a yield is known
            "Distance to threshold (%)":
                distance_to_threshold_pct(price, dps, thr)
                if dps else ((t_yield / thr - 1.0) * 100.0
                             if (t_yield is not None and thr) else None),
            "Status": status,
            "Why": reason + (" · vendor yield diverges >0.3pp"
                             if diverges else ""),
            "Fwd vs trailing DPS (%)": dps_gap,
            "Days in range":
                days_in_range(hist, thr, ev) if thr else 0,
            "Yield source": src or ("declared DPS" if dps else None),
            "As of": asof, "Quality": quality,
        })
    df = pd.DataFrame(out)
    df["_s"] = df["Status"].map(STATUS_ORDER).fillna(9)
    df = df.sort_values(
        ["_s", "Distance to threshold (%)"],
        ascending=[True, False], na_position="last").drop(columns="_s")
    return df.reset_index(drop=True)


# -------------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(description="Dividend buy-level watch")
    p.add_argument("command", choices=["seed", "import-dps",
                                       "import-yields", "bridge", "show"])
    p.add_argument("file", nargs="?")
    p.add_argument("--db", default=None)
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    try:
        if args.command == "seed":
            r = seed_watchlist(conn)
            print(f"watchlist: {r['total']} names "
                  f"(+{len(r['added'])} new: {r['added'] or '—'})")
        elif args.command == "import-dps":
            rows = pd.read_csv(args.file).to_dict("records")
            for r in rows:
                r.setdefault("source", f"manual:{Path(args.file).name}")
            db.insert_dps_events(conn, rows)
            print(f"{len(rows)} dividend events stored (idempotent)")
        elif args.command == "import-yields":
            rows = pd.read_csv(args.file).to_dict("records")
            for r in rows:
                r.setdefault("source", f"manual:{Path(args.file).name}")
                r.setdefault("date", db.now_iso()[:10])
            db.upsert_yield_daily(conn, rows)
            print(f"{len(rows)} daily yield rows stored")
        elif args.command == "bridge":
            n = bridge_from_monitor(conn)
            print(f"{n} interim rows written from the A-H vendor feed")
        elif args.command == "show":
            t = build_watch_table(conn)
            print(t.to_string(index=False) if not t.empty
                  else "watchlist empty — run seed first")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
