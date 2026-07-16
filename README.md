# A–H Premium Monitor

Local dashboard that replaces the manual collection of A-share prices,
H-share prices, CNY/HKD FX, A–H premiums, dividend yields and historical
comparisons for dual-listed (A+H) companies.

**Status: Phase 2** — live A/H prices, CNY/HKD FX and source-reported
premiums from akshare/Eastmoney with Yahoo Finance as the independent
verification/FX source. Live and sample data live in **separate database
files** and sample data is never relabelled as live. See
`docs/source_notes.md` for source findings (including the verified
Eastmoney premium convention) and `docs/validation_phase2.md` for the
10-stock live validation.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# note: in restricted environments install akshare with --no-deps
# (its jsonpath dep may not build) — see docs/source_notes.md §3a

# Live mode (Phase 2+)
python -m ahmon.refresh                      # builds/updates data/ahmon_live.db
python -m ahmon.backfill                     # ~6y daily history (one-time,
                                             # resumable; enables the 5y
                                             # median/percentile/gap metrics)
AHMON_DB=data/ahmon_live.db streamlit run app.py

# Phase 4: automatic refresh during HK/mainland trading hours, alerts,
# buy-level signals and post-close commentary — run alongside the app:
AHMON_DB=data/ahmon_live.db python -m ahmon.scheduler

# Sample mode (Phase 1 synthetic data)
python -m ahmon.sample_data                  # builds data/ahmon.db
streamlit run app.py

# 10-stock live validation table (premium convention cross-check)
python -m ahmon.validate --out docs/validation_phase2.md
```

The sidebar has a **Refresh live data now** button in live mode; each
refresh re-verifies the premium convention, cross-checks Yahoo FX against
the rate implied by Eastmoney's own figures, price-verifies the Focus tier
against Yahoo, recalculates every premium independently and flags >1pp
deviations from the source figure.

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
ahmon/refresh.py        live refresh pipeline (python -m ahmon.refresh)
ahmon/backfill.py       multi-year history backfill for the 5y valuation
                        metrics (python -m ahmon.backfill)
ahmon/enrich.py         English company names from Yahoo quote metadata
                        (python -m ahmon.enrich)
ahmon/validate.py       N-stock live validation table (python -m ahmon.validate)
ahmon/sources/          source layer: schema guard, retry/backoff, ticker
                        normalisation, akshare/Eastmoney primary source
                        (with premium-convention guard), Yahoo chart-API
                        verification + FX source, manual CSV import
                        (emergency fallback)
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

## Where does the dashboard run? (there is no public website)

This is a **local application**: the dashboard exists at
`http://localhost:8501` on whatever machine runs `streamlit run app.py`,
and its database is a local file. Nothing is published to the internet.
To use it day-to-day:

1. **Your own computer** (recommended): clone this repository, follow the
   Quick start above, keep `python -m ahmon.scheduler` running during
   market hours. Bookmark `http://localhost:8501`.
2. **A small always-on machine** (home server / NAS / cheap VPS): same
   commands; open the port only to your own network. The app has **no
   authentication**, so do not expose it to the public internet as-is.
3. **Streamlit Community Cloud** can host public apps free of charge, but
   that makes the dashboard (and your Focus/portfolio classifications)
   visible to anyone with the URL — not recommended without adding
   authentication first.

## Buy-level watch

The Stock Monitor tab opens with a rule-based screen that flags H shares
whose premium setup is historically stretched, with a written reason per
flag (reversion upside, 5-year percentile, dividend yield, P/E, size).
Thresholds live in `ahmon/config.py` (`SIGNAL_*`). The scheduler logs
each flag to the Alerts tab once per day. **This is a screen, not
investment advice.**

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
2. ✅ akshare source connected, 10 stocks validated manually
3. ✅ HSAHP index data and historical series (validated Eastmoney mirror,
   full daily history to 2006, refreshed with every live refresh)
4. Scheduler (HK/mainland trading hours), alerts, commentary
5. Full portfolio onboarding and design polish

*Not investment advice.*
