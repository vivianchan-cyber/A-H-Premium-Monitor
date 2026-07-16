"""Live refresh pipeline (Phase 2).

Order of operations, enforcing the project's data rules:

1. FX from Yahoo (CNYHKD=X, HKD per CNY) — the independent FX source.
   If it fails the refresh aborts with source_health 'failed'; no other
   FX is silently substituted.
2. A/H quotes + source premium from akshare/Eastmoney, with the schema
   guard and the premium-convention guard (see sources/akshare_src.py).
3. Cross-check: the FX rate implied by Eastmoney's own premium figures
   vs Yahoo's rate — divergence > config.ALERT_FX_DIVERGENCE_PCT fires
   an 'fx_divergence' alert and marks the source degraded.
4. Universe sync: new A–H companies are added as 'Other A-H Stock'
   (audit-logged, constituent_log 'added'); existing companies are never
   renamed or reclassified by the feed.
5. Premium is calculated independently for every company
   (calc.a_share_premium with Yahoo FX); |calc − source| > 1pp fires
   'calc_vs_source'. The source figure is stored alongside, never copied
   into premium_calc.
6. Idempotent upsert into daily_obs keyed (company_id, date), with the
   observation date taken from the HK quote timestamp — quality='live'.
7. Yahoo price verification for the Focus tier (delayed ~15 min, so the
   threshold is loose): divergence > ALERT_PRICE_VERIFY_PCT fires
   'price_verification'.

Sample data protection: refuses to write live rows into a database that
contains quality='sample' rows — live and sample stay in separate files
(config.LIVE_DB_PATH vs config.SAMPLE_DB_PATH).

Run:  python -m ahmon.refresh            # writes data/ahmon_live.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import calc, config, db
from .sources import tickers

# Liquid H share whose quote timestamp defines the observation date.
DATE_ANCHOR_H = "939.HK"


def seed_portfolio(conn, portfolio_csv: Path | None = None) -> int:
    """Seed companies/classifications from the portfolio file into an empty
    or existing live DB (idempotent; never overwrites a later
    reclassification made from the dashboard)."""
    portfolio_csv = portfolio_csv or (config.CONFIG_DIR / "portfolio_sample.csv")
    port = pd.read_csv(portfolio_csv)
    existing = set(db.companies_df(conn)["h_ticker"])
    added = 0
    for _, row in port.iterrows():
        if row["h_ticker"] in existing:
            continue
        cid = db.upsert_company(conn, row["name_en"], row["h_ticker"],
                                row["a_ticker"], row["sector"],
                                row.get("name_zh"))
        db.set_classification(
            conn, cid, row["classification"],
            source="seed:portfolio_sample.csv",
            owned=int(row.get("owned", 0)),
            weight=None if pd.isna(row.get("weight")) else float(row["weight"]),
            ref_premium_jul2024=None if pd.isna(row.get("ref_premium_jul2024"))
            else float(row["ref_premium_jul2024"]),
            notes=None if pd.isna(row.get("notes")) else str(row["notes"]))
        added += 1
    return added


def sync_universe(conn, quotes: pd.DataFrame) -> dict:
    """Add feed companies missing from the DB as 'Other A-H Stock'.
    Existing companies keep their names/sector/classification — the feed
    never overwrites owner-managed data."""
    comps = db.companies_df(conn)
    existing = set(comps["h_ticker"])
    added = []
    for _, r in quotes.iterrows():
        if r["h_ticker"] in existing:
            continue
        cid = db.upsert_company(conn, r["name_zh"], r["h_ticker"],
                                r["a_ticker"], config.SECTOR_UNCLASSIFIED,
                                r["name_zh"])
        existing.add(r["h_ticker"])
        db.set_classification(conn, cid, config.OTHER,
                              source="universe_sync:akshare")
        conn.execute(
            "INSERT INTO constituent_log (ts, action, h_ticker, detail) "
            "VALUES (?,?,?,?)",
            (db.now_iso(), "added", r["h_ticker"],
             f"new A–H pair from akshare universe ({r['name_zh']})"))
        added.append(r["h_ticker"])
    # Companies with live history that vanished from the feed are only
    # reported, never deleted; their rows go stale visibly.
    feed = set(quotes["h_ticker"])
    missing = sorted(set(comps["h_ticker"]) - feed)
    conn.commit()
    return {"added": added, "missing_from_feed": missing}


def _guard_no_sample(conn):
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM daily_obs WHERE quality='sample'"
    ).fetchone()["n"]
    if n:
        raise RuntimeError(
            f"refusing to write live data into a database holding {n} "
            "sample rows — live data belongs in "
            f"{config.LIVE_DB_PATH.name} (sample data is never relabelled "
            "as live, and live series must not mix with synthetic history)")


def refresh_hsahp(conn, hsahp_source=None) -> dict:
    """Update the HSAHP daily series from the validated Eastmoney mirror:
    full history on first run, incremental (since the last stored date)
    afterwards. Non-fatal by design — equity refresh continues if the
    index mirror is down; the failure is recorded, never papered over."""
    if hsahp_source is None:
        from .sources.hsahp import HsahpSource
        hsahp_source = HsahpSource()
    from .sources.hsahp import SOURCE_LABEL

    last = conn.execute(
        "SELECT MAX(date) FROM hsahp_daily WHERE source=?",
        (SOURCE_LABEL,)).fetchone()[0]
    beg = "0" if last is None else \
        pd.Timestamp(last).strftime("%Y%m%d")     # re-fetch last day too
    series = hsahp_source.fetch_history(beg=beg)
    now = db.now_iso()
    db.insert_hsahp(conn, [{"date": d.strftime("%Y-%m-%d"),
                            "close": float(v), "source": SOURCE_LABEL,
                            "updated_at": now}
                           for d, v in series.items()])
    db.set_source_health(
        conn, hsahp_source.name, "ok",
        f"live: Eastmoney mirror of 100.HSAHP — {len(series)} closes "
        f"upserted (through {series.index.max():%Y-%m-%d}; official series "
        "is EOD, today's value is intraday until the close). Validated vs "
        "our own cap-weighted premium (corr 0.95); monthly factsheet "
        "remains the manual cross-check.")
    return {"upserted": len(series),
            "through": series.index.max().strftime("%Y-%m-%d"),
            "full_backfill": last is None}


def write_intraday_ticks(conn, quotes, comps, fx: float, now_iso: str,
                         a_is_close: bool) -> int:
    """5-minute-style intraday snapshot for Focus Holdings (scheduler use).
    a_is_close=1 marks ticks taken while the mainland market is closed —
    the A price is then the latest A close, not a live print."""
    focus = comps[comps["classification"] == config.FOCUS]
    em = quotes.set_index("h_ticker")
    ticks = []
    for h_ticker, c in focus.iterrows():
        if h_ticker not in em.index:
            continue
        q = em.loc[h_ticker]
        ticks.append({
            "company_id": int(c["id"]), "ts": now_iso,
            "a_price": float(q["a_price_cny"]),
            "h_price": float(q["h_price_hkd"]), "fx": fx,
            "premium_calc": calc.a_share_premium(
                q["a_price_cny"], q["h_price_hkd"], fx),
            "premium_src": float(q["premium_src"]),
            "a_is_close": int(a_is_close),
        })
    conn.executemany(
        """INSERT OR REPLACE INTO intraday_obs
           (company_id, ts, a_price, h_price, fx, premium_calc,
            premium_src, a_is_close)
           VALUES (:company_id,:ts,:a_price,:h_price,:fx,:premium_calc,
                   :premium_src,:a_is_close)""", ticks)
    conn.commit()
    return len(ticks)


def refresh_live(conn, ak_source=None, yahoo_source=None,
                 verify_n: int = 10, hsahp_source=None,
                 write_intraday: bool = False) -> dict:
    """One full live refresh. Returns a summary dict; raises on a failure
    of a primary step (after recording it in source_health)."""
    if ak_source is None:
        from .sources.akshare_src import AkshareSource
        ak_source = AkshareSource()
    if yahoo_source is None:
        from .sources.yahoo import YahooSource
        yahoo_source = YahooSource()

    _guard_no_sample(conn)
    summary: dict = {"alerts": []}

    def alert(rule, message, company=None, severity="warning"):
        db.log_alert(conn, rule, message, company, severity)
        summary["alerts"].append({"rule": rule, "company": company,
                                  "message": message})

    # 1 — FX (independent source; no silent substitute on failure)
    try:
        fx_info = yahoo_source.fetch_fx()
    except Exception as e:
        db.set_source_health(conn, yahoo_source.name, "failed",
                             f"FX fetch failed: {e}")
        raise
    fx = fx_info["hkd_per_cny"]
    summary["fx"] = fx

    # 2 — quotes + convention-checked source premium
    try:
        quotes = ak_source.fetch_quotes(hkd_per_cny=fx)
    except Exception as e:
        db.set_source_health(conn, ak_source.name, "failed",
                             f"quote fetch failed: {e}")
        raise
    summary["quotes"] = len(quotes)
    summary["convention"] = quotes.attrs.get("convention")
    summary["dropped_rows"] = quotes.attrs.get("dropped_rows", 0)
    summary["em_host"] = quotes.attrs.get("em_host", "push2 (realtime)")

    # 3 — FX cross-check (Yahoo vs rate implied by Eastmoney's own figures)
    fx_em = float(quotes["fx_implied"].median())
    fx_div_pct = abs(fx / fx_em - 1.0) * 100.0
    summary["fx_em_implied"] = fx_em
    summary["fx_divergence_pct"] = fx_div_pct
    ak_status, ak_note = "ok", ""
    if "delayed" in summary["em_host"]:
        ak_status = "degraded"
        ak_note = f" · served by {summary['em_host']} — realtime host down"
    if fx_div_pct > config.ALERT_FX_DIVERGENCE_PCT:
        alert("fx_divergence",
              f"Yahoo CNYHKD {fx:.4f} vs Eastmoney-implied {fx_em:.4f} "
              f"({fx_div_pct:.2f}% apart, threshold "
              f"{config.ALERT_FX_DIVERGENCE_PCT}%)", severity="serious")
        ak_status, ak_note = "degraded", (
            f" · FX cross-check divergence {fx_div_pct:.2f}%")

    # 4 — universe sync
    uni = sync_universe(conn, quotes)
    summary["universe_added"] = len(uni["added"])
    summary["universe_missing"] = len(uni["missing_from_feed"])

    # 5+6 — independent premium calc and idempotent upsert
    try:
        anchor = yahoo_source.fetch_quotes([DATE_ANCHOR_H]).iloc[0]
        obs_date = anchor["h_asof"].strftime("%Y-%m-%d")
    except Exception:
        # anchor quote unavailable: fall back to today (SG == HK offset),
        # visibly recorded in the summary rather than silently.
        obs_date = pd.Timestamp.now(tz=config.TZ).strftime("%Y-%m-%d")
        summary["date_anchor"] = "fallback:today"
    else:
        summary["date_anchor"] = f"yahoo:{DATE_ANCHOR_H}"
    summary["obs_date"] = obs_date

    # 4b — English names for companies the universe sync just added
    if uni["added"]:
        from .enrich import enrich_english_names
        try:
            enr = enrich_english_names(conn, yahoo_source,
                                       limit=len(uni["added"]) + 5,
                                       log=lambda *_: None)
            summary["names_enriched"] = enr["updated"]
        except Exception:            # enrichment is cosmetic — never fatal
            summary["names_enriched"] = 0

    # 4c — fundamentals for both legs (P/E, P/B, market cap, div yield).
    # Non-fatal: prices/premium remain valid without them; the gap is
    # visible (None columns) and recorded, never papered over.
    stats_by_ticker: dict = {}
    try:
        stats = ak_source.fetch_stats(
            list(zip(quotes["h_ticker"], quotes["a_ticker"])))
        stats_by_ticker = {s["h_ticker"]: s for s in
                           stats.to_dict("records")}
        summary["stats_fetched"] = len(stats_by_ticker)
    except Exception as e:
        summary["stats_fetched"] = 0
        alert("stats_fetch", f"fundamentals fetch failed: {e}",
              severity="warning")

    comps = db.companies_df(conn).set_index("h_ticker")
    now = db.now_iso()
    rows, stat_rows, discrepancies = [], [], 0
    for _, q in quotes.iterrows():
        cid = int(comps.loc[q["h_ticker"], "id"])
        st = stats_by_ticker.get(q["h_ticker"], {})
        if st:
            stat_rows.append({
                "company_id": cid,
                "h_mktcap_hkd": st.get("h_mktcap"),
                "a_mktcap_cny": st.get("a_mktcap"),
                "h_pe": st.get("h_pe"), "a_pe": st.get("a_pe"),
                "h_pb": st.get("h_pb"), "a_pb": st.get("a_pb"),
                "h_div_yield": st.get("h_div_yield"),
                "a_div_yield": st.get("a_div_yield"),
                "updated_at": now})
        prem = calc.a_share_premium(q["a_price_cny"], q["h_price_hkd"], fx)
        if calc.discrepancy_flag(prem, q["premium_src"]):
            discrepancies += 1
            alert("calc_vs_source",
                  f"Calculated {prem:+.2f}% vs source "
                  f"{q['premium_src']:+.2f}% "
                  f"(Δ{prem - q['premium_src']:+.2f}pp > "
                  f"{config.ALERT_DISCREPANCY_PP}pp) — verify source figure",
                  company=str(comps.loc[q["h_ticker"], "name_en"]),
                  severity="serious")
        rows.append({
            "company_id": cid, "date": obs_date,
            "a_close": float(q["a_price_cny"]),
            "h_close": float(q["h_price_hkd"]), "fx": fx,
            "premium_calc": prem, "premium_src": float(q["premium_src"]),
            "a_div_yield": st.get("a_div_yield"),
            "h_div_yield": st.get("h_div_yield"),
            "quality": "live", "updated_at": now,
        })
    db.insert_daily(conn, rows)
    if stat_rows:
        db.upsert_stats(conn, stat_rows)
    if write_intraday:
        from . import market_hours
        summary["intraday_ticks"] = write_intraday_ticks(
            conn, quotes, comps, fx, now,
            a_is_close=not market_hours.cn_open())
    summary["upserted"] = len(rows)
    summary["calc_vs_source_flags"] = discrepancies

    # 7 — Yahoo verification of the Focus tier
    verified, price_flags = 0, 0
    focus = comps[comps["classification"] == config.FOCUS]
    focus = focus[focus.index.isin(quotes["h_ticker"])].head(verify_n)
    if len(focus):
        yq = yahoo_source.fetch_quotes(
            list(focus.index), list(focus["a_ticker"]))
        em = quotes.set_index("h_ticker")
        for _, v in yq.iterrows():
            if v["error"] or v["h_price_hkd"] is None:
                continue
            verified += 1
            e = em.loc[v["h_ticker"]]
            for leg, em_p, y_p in (("H", e["h_price_hkd"], v["h_price_hkd"]),
                                   ("A", e["a_price_cny"], v["a_price_cny"])):
                if y_p is None:
                    continue
                div = abs(em_p / y_p - 1.0) * 100.0
                if div > config.ALERT_PRICE_VERIFY_PCT:
                    price_flags += 1
                    alert("price_verification",
                          f"{leg}-share price: akshare {em_p} vs Yahoo "
                          f"{y_p} ({div:.1f}% apart, threshold "
                          f"{config.ALERT_PRICE_VERIFY_PCT}%; Yahoo is "
                          "~15 min delayed)",
                          company=str(focus.loc[v["h_ticker"], "name_en"]))
    summary["verified_vs_yahoo"] = verified
    summary["price_verification_flags"] = price_flags

    # health — after everything succeeded
    db.set_source_health(
        conn, ak_source.name, ak_status,
        f"live: {len(rows)} companies upserted for {obs_date}, "
        f"{summary['dropped_rows']} suspended/blank rows dropped, "
        f"premium convention: {summary['convention']}{ak_note}")
    db.set_source_health(
        conn, yahoo_source.name, "ok",
        f"live: CNYHKD=X {fx:.4f} (as of {fx_info['asof']:%H:%M}), "
        f"{verified} Focus names price-verified")

    # HSAHP index (Phase 3) — non-fatal for the equity refresh
    try:
        hs = refresh_hsahp(conn, hsahp_source)
        summary["hsahp"] = hs
    except Exception as e:
        db.set_source_health(conn, "HSAHP index series", "failed",
                             f"mirror fetch failed: {e} — CSV import "
                             "remains the fallback")
        alert("hsahp_fetch", f"HSAHP mirror fetch failed: {e}",
              severity="serious")
        summary["hsahp"] = None
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description="A–H Monitor live refresh")
    p.add_argument("--db", default=str(config.LIVE_DB_PATH),
                   help="database file (default: data/ahmon_live.db)")
    p.add_argument("--verify-n", type=int, default=10,
                   help="Focus names to cross-check against Yahoo")
    args = p.parse_args(argv)

    conn = db.connect(args.db)
    seeded = seed_portfolio(conn)
    if seeded:
        print(f"Seeded {seeded} companies from portfolio_sample.csv")
    s = refresh_live(conn, verify_n=args.verify_n)
    print(f"Refreshed {s['upserted']} companies for {s['obs_date']} "
          f"(date anchor {s['date_anchor']})")
    print(f"FX: Yahoo {s['fx']:.4f} HKD/CNY · Eastmoney-implied "
          f"{s['fx_em_implied']:.4f} · divergence {s['fx_divergence_pct']:.2f}%")
    print(f"Premium convention detected: {s['convention']} · "
          f"calc-vs-source >1pp flags: {s['calc_vs_source_flags']}")
    print(f"Universe: +{s['universe_added']} added, "
          f"{s['universe_missing']} DB companies missing from feed")
    print(f"Yahoo verification: {s['verified_vs_yahoo']} names checked, "
          f"{s['price_verification_flags']} price flags")
    if s.get("hsahp"):
        h = s["hsahp"]
        print(f"HSAHP: {h['upserted']} closes upserted through "
              f"{h['through']}"
              + (" (full 2006→ backfill)" if h["full_backfill"] else ""))
    else:
        print("HSAHP: mirror fetch FAILED — see Health tab")
    if s["alerts"]:
        print(f"{len(s['alerts'])} alerts logged (see Alerts tab)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
