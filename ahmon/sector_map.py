"""Owner-curated sector taxonomy for the full A–H universe.

Sectors are data, not code: config/sector_map.csv maps every H ticker to
one of the eight config.SECTORS buckets, and this module reconciles the
database against it. Conventions used by the map (only eight buckets, so
neighbouring industries are folded in):

- power & environmental utilities, transport (airlines, rail, ports,
  shipping, expressways, logistics), construction & machinery
  → Industrials
- real-estate developers, brokers, insurers, banks, futures houses
  → Financials
- autos & parts, appliances, food & beverage, retail, media
  → Consumer
- chemicals, steel, cement, glass, paper, battery materials, metals
  → Mining & Materials
- semis, electronics, hardware, telecom equipment → Technology
  (telecom *operators* → Telecom)

Every change is written through db.upsert_company's sector field and
recorded in constituent_log ('sector_set'), so the Health/audit trail
shows what moved. Idempotent: re-applying changes nothing.

The live refresh applies the map on every cycle, so newly listed A–H
pairs picked up by the universe sync get their sector as soon as the CSV
knows them; until then they stay visibly 'Unclassified'.

Run:  python -m ahmon.sector_map          # or the admin button in the app
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config, db

SECTOR_MAP_CSV = config.CONFIG_DIR / "sector_map.csv"


def load_sector_map(csv_path: str | Path | None = None) -> dict[str, str]:
    """Ticker → sector, validated against config.SECTORS."""
    path = Path(csv_path) if csv_path else SECTOR_MAP_CSV
    df = pd.read_csv(path)
    dupes = df[df["h_ticker"].duplicated()]["h_ticker"].tolist()
    if dupes:
        raise ValueError(f"duplicate tickers in {path.name}: {dupes}")
    bad = sorted(set(df["sector"]) - set(config.SECTORS))
    if bad:
        raise ValueError(f"unknown sectors in {path.name}: {bad} "
                         f"(valid: {config.SECTORS})")
    return dict(zip(df["h_ticker"].str.strip(), df["sector"].str.strip()))


def apply_sector_map(conn, csv_path: str | Path | None = None,
                     source: str = "sector_map.csv") -> dict:
    """Set each company's sector to the mapped value. Returns what changed
    and which DB companies the map doesn't cover (left untouched)."""
    smap = load_sector_map(csv_path)
    comps = db.companies_df(conn)
    updated, unmapped = [], []
    for _, c in comps.iterrows():
        want = smap.get(c["h_ticker"])
        if want is None:
            unmapped.append(c["h_ticker"])
            continue
        if c["sector"] != want:
            conn.execute(
                "UPDATE companies SET sector=:s WHERE id=:i",
                {"s": want, "i": int(c["id"])})
            db.log_constituent(conn, "sector_set", c["h_ticker"],
                               f"{c['sector']} → {want} ({source})")
            updated.append(f"{c['h_ticker']}: {c['sector']} → {want}")
    return {"updated": updated, "unmapped": sorted(unmapped),
            "mapped_size": len(smap)}


def main(argv=None):
    p = argparse.ArgumentParser(description="Apply config/sector_map.csv")
    p.add_argument("--db", default=None,
                   help="database (default: DATABASE_URL or local file)")
    p.add_argument("--csv", default=None)
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    r = apply_sector_map(conn, args.csv)
    print(f"Sector map applied: {len(r['updated'])} companies updated "
          f"of {r['mapped_size']} mapped")
    for u in r["updated"]:
        print(" ", u)
    if r["unmapped"]:
        print(f"WARNING — in the database but not in the map "
              f"(left as-is): {r['unmapped']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
