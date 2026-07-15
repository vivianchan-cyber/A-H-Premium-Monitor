"""Manual CSV import — the emergency fallback when every automated source
is down, and the bridge for AASTOCKS/HSI figures that cannot be scraped
(both sites block automated access; see docs/source_notes.md).

Template: config/csv_import_template.csv
Required columns: date, h_ticker, a_price_cny, h_price_hkd, hkd_per_cny.
Optional: premium_src_pct (in the A-share-premium convention; if you are
copying an AASTOCKS H-premium figure, set convention=h_premium and it will
be converted), a_div_yield, h_div_yield, hsahp_close.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import calc, config, db
from . import check_schema

REQUIRED = ["date", "h_ticker", "a_price_cny", "h_price_hkd", "hkd_per_cny"]


def import_csv(conn, path: str | Path) -> dict:
    df = pd.read_csv(path)
    check_schema(df, REQUIRED, f"csv_import:{path}")
    comps = db.companies_df(conn).set_index("h_ticker")
    imported, skipped = 0, []
    now = db.now_iso()
    for _, r in df.iterrows():
        if r["h_ticker"] not in comps.index:
            skipped.append(r["h_ticker"])
            continue
        cid = int(comps.loc[r["h_ticker"], "id"])
        prem = calc.a_share_premium(float(r["a_price_cny"]),
                                    float(r["h_price_hkd"]),
                                    float(r["hkd_per_cny"]))
        src = r.get("premium_src_pct")
        if pd.notna(src) and str(r.get("convention", "")).strip() == "h_premium":
            src = calc.a_premium_from_h_premium(float(src))
        db.insert_daily(conn, [{
            "company_id": cid, "date": str(r["date"]),
            "a_close": float(r["a_price_cny"]),
            "h_close": float(r["h_price_hkd"]),
            "fx": float(r["hkd_per_cny"]),
            "premium_calc": prem,
            "premium_src": float(src) if pd.notna(src) else None,
            "a_div_yield": float(r["a_div_yield"]) if pd.notna(r.get("a_div_yield")) else None,
            "h_div_yield": float(r["h_div_yield"]) if pd.notna(r.get("h_div_yield")) else None,
            "quality": "manual_import", "updated_at": now,
        }])
        imported += 1
    if "hsahp_close" in df.columns:
        hs = df.dropna(subset=["hsahp_close"])
        db.insert_hsahp(conn, [{"date": str(r["date"]),
                                "close": float(r["hsahp_close"]),
                                "source": "manual_import", "updated_at": now}
                               for _, r in hs.iterrows()])
    db.set_source_health(conn, "manual CSV import", "ok",
                         f"imported {imported} rows from {Path(path).name}")
    return {"imported": imported, "skipped": skipped}
