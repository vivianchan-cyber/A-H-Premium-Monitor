# Source notes — findings from the pre-implementation inspection (2026-07-15)

This file documents what was learned about the two primary reference sources
**before any code was written**, plus the fallback strategy the pipeline is
built on. It is the licence/feasibility record the project rules require.

## 1. AASTOCKS A+H shares page
`http://www.aastocks.com/en/stocks/market/ah.aspx`

### Accessibility
- Returns **HTTP 403 Forbidden** to non-browser clients, including requests
  with a normal Chrome user-agent string. The site actively blocks
  automated access. Verified directly on 2026-07-15 from this project's
  environment (both a headless fetcher and `curl` with a browser UA).
- Consequence: this page **cannot be used as an automated feed**.

### Data fields shown (from the public page description)
Per dual-listed company: company name, H-share code, A-share code (SH/SZ),
H-share price (HKD), A-share price (CNY), change %, and an **AH premium %**.
The page also offers All/SZ/SH filters and an AH premium distribution chart.

### Premium convention — IMPORTANT
AASTOCKS explains its convention as: *positive premium % = the H share's
price is higher than the A share's HKD-equivalent price*. That is the
**H-share premium**, the opposite of this dashboard's standard definition
(A-share premium over H). Any figure copied from AASTOCKS must be converted
with `calc.a_premium_from_h_premium()` before comparison. Note: convention
must be re-verified manually in Phase 2 against 10 stocks, since site
labelling can change.

### Licensing / terms
Quote data is licensed to AASTOCKS by HKEx Information Services, China
Investment Information Services, Shenzhen Securities Information Co. and
others. Their terms disclaim accuracy and do not grant redistribution
rights; scraping is not permitted. Prices for free users are delayed
(HK quotes typically ≥15 minutes).

### Role in this project
Manual validation reference only (a human reads the page and cross-checks
figures, or keys them into the CSV import with `convention=h_premium`).

## 2. Hang Seng Stock Connect China AH Premium Index (HSAHP)
`https://www.hsi.com.hk/eng/indexes/all-indexes/ahpremium`

### Accessibility
- The page refuses non-browser connections from this environment (the
  connection is dropped / 403). The site is JavaScript-rendered and served
  behind bot protection. Verified 2026-07-15.

### Index facts (from Hang Seng Indexes' public materials)
- Tracks the average price premium of A shares over H shares for the most
  liquid AH companies eligible for Stock Connect; **free-float weighted**.
- **Reading**: >100 = A shares trade at a premium; 100 = parity; <100 = A
  shares at a discount. Implied average A-share premium = HSAHP − 100.
- **Updates once per day (EOD)** — there is no intraday HSAHP. The Market
  Overview section therefore refreshes this series daily only.
- History back to 2006/2007.
- Hang Seng publishes a **monthly factsheet PDF** (e.g.
  `hsi.com.hk/static/uploads/contents/en/dl_centre/factsheets/ahpremiume.pdf`)
  with the level, returns and constituents — usable for manual monthly
  verification.

### Licensing / terms
Hang Seng Indexes data is proprietary and licensed; bulk download of the
daily series requires a data licence. Third parties (TradingView symbol
`HSI:HSAHP`, MacroMicro, etc.) display it under their own licences and may
not be scraped either.

### Role in this project
Index level maintained via: (a) manual entry / CSV import of the daily
close, (b) monthly factsheet cross-check, (c) Phase 3 investigation of a
programmatic mirror (e.g. Eastmoney/akshare index endpoints) — to be
validated against the factsheet before trust.

## 3. Chosen automated pipeline (fallback-first design)

| Purpose | Primary | Verification | Emergency fallback |
|---|---|---|---|
| A–H universe list | akshare (Eastmoney AH comparison table) | manual vs AASTOCKS page | seeded classification file |
| A/H prices | akshare spot | Yahoo Finance quotes | manual CSV import |
| Source-reported premium | akshare AH table (convention to be verified in Phase 2) | AASTOCKS manual read | CSV import |
| CNY/HKD FX | akshare FX / Yahoo `CNYHKD=X` | cross-check both, alert on >0.5% divergence | manual CSV |
| Dividend yields | akshare / Yahoo fundamentals | manual spot checks | CSV |
| HSAHP series | manual daily close + monthly factsheet | factsheet PDF | CSV import (`hsahp_close` column) |

Rules enforced in code:
- Never silently substitute a source; the health panel names what actually
  supplied each series (`source_health` table).
- Never reuse an old value without labelling it `stale`.
- Every fetched payload passes `check_schema()` before ingestion
  (schema-change detection), with retries + exponential backoff
  (`with_retries`).
- Independently calculate the premium and compare with the source figure;
  discrepancies > 1pp raise the `calc_vs_source` alert.

## 4. Refresh limitations (summary)
- AASTOCKS: no automated access; free quotes delayed ≥15 min anyway.
- HSAHP: daily EOD only, by design.
- akshare/Eastmoney/Sina: near-real-time but unofficial endpoints — treat
  as best-effort, rate-limit politely (the Phase 4 scheduler's 5/15/30-min
  tiers keep request volume low), and expect occasional schema changes
  (hence the schema guard).
- Yahoo Finance: HK quotes ~15 min delayed for some exchanges; fine for a
  monitor, not for execution.
