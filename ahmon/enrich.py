"""English-name enrichment for universe companies.

Companies added automatically from the akshare A–H universe only carry
Eastmoney's Chinese name. This fills companies.name_en from the H-share
quote metadata on Yahoo (longName), leaving name_zh untouched, so every
table can show both names. Idempotent: only companies whose English name
still contains CJK characters are touched; a failed lookup leaves the
Chinese name in place (never blanked).

Run:  python -m ahmon.enrich
"""

from __future__ import annotations

import argparse
import re
import sys

from . import config, db

CJK = re.compile(r"[㐀-鿿]")


def needs_english_name(name: str | None) -> bool:
    return bool(name) and bool(CJK.search(name))


def enrich_english_names(conn, yahoo_source=None, limit: int = 0,
                         log=print) -> dict:
    if yahoo_source is None:
        from .sources.yahoo import YahooSource
        yahoo_source = YahooSource()
    comps = db.companies_df(conn)
    todo = comps[comps["name_en"].map(needs_english_name)]
    if limit:
        todo = todo.head(limit)
    updated, failed = 0, []
    for _, c in todo.iterrows():
        try:
            name = yahoo_source.fetch_english_name(c["h_ticker"])
        except Exception as e:      # noqa: BLE001 — reported, not hidden
            failed.append(f"{c['h_ticker']}: {e}")
            continue
        if name and not CJK.search(name):
            db.update_company_name_en(conn, int(c["id"]), name.strip())
            updated += 1
            log(f"  {c['h_ticker']}: {c['name_en']} -> {name.strip()}")
        else:
            failed.append(f"{c['h_ticker']}: no English name on Yahoo")
    return {"candidates": len(todo), "updated": updated, "failed": failed}


def main(argv=None):
    p = argparse.ArgumentParser(description="Fill English company names")
    p.add_argument("--db", default=str(config.LIVE_DB_PATH))
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args(argv)
    conn = db.connect(args.db)
    s = enrich_english_names(conn, limit=args.limit)
    print(f"{s['updated']}/{s['candidates']} names filled, "
          f"{len(s['failed'])} lookups failed")
    for f in s["failed"][:10]:
        print("  failed:", f)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
