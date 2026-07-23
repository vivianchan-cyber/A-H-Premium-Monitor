"""Owner-managed Focus list, applied to the database.

The Focus list is data, not code (project rule): classification lives in
the database and every change is audit-logged. This module reconciles the
database against config/focus_list.csv — companies on the list become
Focus Holdings, companies currently Focus but absent from the list are
demoted to Other A-H Stock. Other tiers (Watchlist, Portfolio) are left
untouched. Idempotent: re-applying an already-applied list changes
nothing and logs nothing.

Run:  python -m ahmon.focus_list          # or the admin button in the app
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config, db

FOCUS_LIST_CSV = config.CONFIG_DIR / "focus_list.csv"


def apply_focus_list(conn, csv_path: str | Path | None = None,
                     source: str = "focus_list.csv") -> dict:
    path = Path(csv_path) if csv_path else FOCUS_LIST_CSV
    wanted = set(pd.read_csv(path)["h_ticker"].str.strip())
    comps = db.companies_df(conn)
    known = set(comps["h_ticker"])
    promoted, demoted = [], []
    for _, c in comps.iterrows():
        current = c["classification"] or config.OTHER
        if c["h_ticker"] in wanted and current != config.FOCUS:
            db.set_classification(conn, int(c["id"]), config.FOCUS,
                                  source=source)
            promoted.append(c["h_ticker"])
        elif c["h_ticker"] not in wanted and current == config.FOCUS:
            db.set_classification(conn, int(c["id"]), config.OTHER,
                                  source=source)
            demoted.append(c["h_ticker"])
    return {"promoted": promoted, "demoted": demoted,
            "missing_from_db": sorted(wanted - known),
            "focus_size": len(wanted & known)}


def main(argv=None):
    p = argparse.ArgumentParser(description="Apply config/focus_list.csv")
    p.add_argument("--db", default=None,
                   help="database (default: DATABASE_URL or local file)")
    p.add_argument("--csv", default=None)
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    r = apply_focus_list(conn, args.csv)
    print(f"Focus list applied: {r['focus_size']} Focus Holdings · "
          f"promoted {r['promoted'] or 'none'} · "
          f"demoted {r['demoted'] or 'none'}")
    if r["missing_from_db"]:
        print(f"WARNING — on the list but not in the database: "
              f"{r['missing_from_db']}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
