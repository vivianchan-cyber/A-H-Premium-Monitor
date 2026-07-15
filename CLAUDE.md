# A–H Premium Monitor — project rules

## Financial definitions (non-negotiable)
- **A-share premium (%)** — the dashboard standard:
  `((A_price_CNY × HKD_per_CNY) / H_price_HKD − 1) × 100`
- The FX rate is always **HKD per 1 CNY** (≈1.05–1.10). `calc.a_share_premium`
  rejects rates outside [0.5, 2.0]; the FX-direction tests must never be
  weakened.
- AASTOCKS displays the **opposite** convention (H relative to A). Convert
  external H-premium figures with `calc.a_premium_from_h_premium` before
  storing them in `premium_src`.
- Never copy a displayed premium as our own: always calculate independently
  and compare; >1pp difference fires the `calc_vs_source` alert.
- HSAHP: >100 means A shares trade at a premium; implied average A-share
  premium = HSAHP − 100. The index is EOD-only.
- Premium attribution is **pure arithmetic** (log-return decomposition in
  `calc.attribute_premium_move`). Never ask an LLM to infer attribution.
- Automated commentary is generated from calculated facts via templates
  (`commentary.py`). An LLM narrative may only ever be layered on top of
  verified numbers.

## Data rules
- Timezone: Asia/Singapore (`config.TZ`).
- Never silently substitute a data source; record what supplied each series
  in `source_health`.
- Never reuse an old value without labelling it `stale`; sample data is
  always labelled `sample` and displayed with a warning.
- All ingestion is idempotent (upserts keyed on company/date) — duplicate
  records must be impossible by construction.
- Classification lives in the DB (seeded from `config/portfolio_sample.csv`),
  is editable from the dashboard, and every change is audit-logged. The
  initial Focus list is a starting point, not hard-coded behaviour.
- Storage goes through `ahmon/db.py` only, so SQLite can later be swapped
  for another engine without touching callers.

## Sources
- Both reference pages (AASTOCKS A+H, HSI AH Premium) **block automated
  access** — see `docs/source_notes.md`. They are manual references only.
- Automated pipeline (Phase 2+): akshare primary, Yahoo Finance
  verification, manual CSV import as emergency fallback.

## Engineering
- Run `pytest` before committing; the premium-formula and FX-direction
  tests are the contract.
- Phased delivery (1 sample data → 2 akshare → 3 HSAHP history → 4
  scheduler/alerts → 5 full universe & polish). At each phase end: run
  tests, show the dashboard, document unresolved issues, and get the
  owner's verification before continuing.
