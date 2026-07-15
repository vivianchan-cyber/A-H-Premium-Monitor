# A–H Premium Monitor

Local dashboard that replaces the manual collection of A-share prices,
H-share prices, CNY/HKD FX, A–H premiums, dividend yields and historical
comparisons for dual-listed (A+H) companies.

**Status: Phase 1** — full dashboard, calculation engine, SQLite storage and
tests running on deterministic *sample* data. No live feed is connected yet
(clearly labelled in the UI). See `docs/source_notes.md` for why the two
reference pages cannot be scraped and what the live pipeline will use.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m ahmon.sample_data     # builds data/ahmon.db (~45 companies, 5y+ history)
streamlit run app.py
```

Run the tests:

```bash
pytest
```

## Layout

```
app.py                  Streamlit dashboard (9 tabs)
ahmon/config.py         constants, thresholds, timezone (Asia/Singapore)
ahmon/calc.py           premium formula, convention conversion, changes,
                        percentiles, attribution, chart resampling
ahmon/db.py             SQLite layer (all SQL lives here — swappable engine)
ahmon/metrics.py        monitor table, rankings, sector stats
ahmon/alerts.py         alert rules -> dashboard + alerts_log
ahmon/commentary.py     template commentary from calculated facts
ahmon/sample_data.py    deterministic Phase 1 sample data generator
ahmon/sources/          source layer: schema guard, retry/backoff,
                        manual CSV import (emergency fallback)
config/portfolio_sample.csv   editable classification seed (23 Focus names)
config/csv_import_template.csv  manual-import template
docs/                   data_dictionary.md, source_notes.md
tests/                  premium formula, FX direction, DB, import, metrics
```

## Core definition

```
A-share premium (%) = ((A price in CNY × HKD per CNY) / H price in HKD − 1) × 100
```

Positive = the A share trades above its H counterpart. This is the opposite
of AASTOCKS' displayed convention (H relative to A); external figures are
converted before storage, and a calculated-vs-source difference > 1pp fires
an alert. Full rules in `CLAUDE.md`.

## Managing classifications

The Focus list is data, not code: edit `config/portfolio_sample.csv` before
first run, or use the **Stock Monitor → Reclassify / Promote to Focus /
Remove from Focus** controls afterwards. Every change is audit-logged.

## Emergency manual import

Fill `config/csv_import_template.csv` and upload it from the sidebar. If
you copy an AASTOCKS premium figure, set `convention=h_premium` and it will
be converted to the dashboard's A-share-premium convention.

## Phases

1. ✅ Sample data: layout, calculations, database, tests
2. akshare source connected, 10 stocks validated manually
3. HSAHP index data and historical series
4. Scheduler (HK/mainland trading hours), alerts, commentary
5. Full portfolio onboarding and design polish

*Not investment advice.*
