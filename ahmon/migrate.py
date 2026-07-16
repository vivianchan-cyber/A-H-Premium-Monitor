"""One-time migration of a local SQLite database into PostgreSQL.

Copies every table byte-for-byte (all values are text/float/int — both
backends hold identical data), preserving primary keys, then advances the
PostgreSQL sequences past the migrated ids so future inserts don't
collide. Order respects foreign keys.

Run:  DATABASE_URL=postgres://…  python -m ahmon.migrate --sqlite data/ahmon_live.db
      (add --replace to wipe the target tables first — use this for
      re-runs; without it, rows are appended and duplicate keys fail
      loudly rather than silently overwriting)
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
import sqlalchemy as sa

from . import config, db


def migrate(sqlite_path: str, target_url: str | None = None,
            replace: bool = False, log=print) -> dict:
    target_url = target_url or config.DATABASE_URL
    if not target_url:
        raise SystemExit("DATABASE_URL is not set (and no target given)")

    src = db.connect(sqlite_path)
    if src.dialect != "sqlite":
        raise SystemExit(f"--sqlite must point at a SQLite file, "
                         f"got {src.dialect}")
    dst = db.connect(target_url)

    counts: dict[str, int] = {}
    try:
        if replace:
            # child tables first so FKs never dangle
            for name in reversed(db.TABLES_IN_FK_ORDER):
                dst.execute(f"DELETE FROM {name}")     # noqa: S608 — fixed list
        for name in db.TABLES_IN_FK_ORDER:
            frame = src.read_df(f"SELECT * FROM {name}")   # noqa: S608
            counts[name] = len(frame)
            if frame.empty:
                continue
            frame = frame.astype(object).where(pd.notna(frame), None)
            table = db.metadata.tables[name]
            with dst.engine.begin() as c:
                c.execute(sa.insert(table), frame.to_dict("records"))
            log(f"  {name}: {len(frame)} rows")
        if dst.dialect == "postgresql":
            for name in db.SERIAL_TABLES:
                dst.execute(
                    f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)")
    finally:
        src.close()
        dst.close()
    return counts


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Copy a local SQLite database into DATABASE_URL")
    p.add_argument("--sqlite", default=str(config.LIVE_DB_PATH),
                   help="source SQLite file (default: data/ahmon_live.db)")
    p.add_argument("--replace", action="store_true",
                   help="delete existing rows in the target first")
    args = p.parse_args(argv)
    counts = migrate(args.sqlite, replace=args.replace)
    total = sum(counts.values())
    print(f"Migrated {total} rows across {len(counts)} tables "
          f"from {args.sqlite}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
