# Source notes — pre-implementation inspection (2026-07-15) + Phase 2 live findings (2026-07-15)

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

## 3a. Phase 2 live findings (verified 2026-07-15 from this environment)

### Connectivity
All planned hosts are reachable: query1.finance.yahoo.com,
qt.gtimg.cn / web.ifzq.gtimg.cn (Tencent), push2.eastmoney.com /
push2his.eastmoney.com (Eastmoney).

### akshare endpoints
- `stock_zh_ah_name` / `stock_zh_ah_spot` (Tencent): work; 220 A+H names,
  but **H-share quotes only** — no A price and no premium column.
- `stock_zh_ah_spot_em` (Eastmoney "AH股比价", the endpoint the live layer
  uses): ~201 companies with H code, A code, both prices and a premium
  column (溢价). Quirks handled in `sources/akshare_src.py`:
  - **502 bursts** — push2.eastmoney.com intermittently returns
    502/timeouts; `with_retries(attempts=5, base_delay=3)` rides them out.
  - **Pagination duplicates/gaps** — the endpoint paginates a
    live-resorting list, so within one fetch a stock can appear on two
    pages (deduped by H ticker) or briefly drop out (picked up on the next
    refresh).
- Install caveat: `pip install --no-deps akshare` in environments where
  its `jsonpath` dependency cannot build, then provide a stub `jsonpath`
  module (only akshare's macro-economics functions — unused here — need
  the real one). `tabulate`, `beautifulsoup4`, `lxml`, `tqdm`,
  `py-mini-racer`, `decorator`, `openpyxl`, `xlrd`, `html5lib` cover the
  rest of its runtime imports.

### Eastmoney premium convention — VERIFIED
Eastmoney's 溢价 column is the **A-share premium** (positive = A above H):
recomputing `((A_CNY × HKD_per_CNY) / H_HKD − 1) × 100` from the table's
own prices reproduces 溢价 to ~0.1pp across the whole table. Same
convention as this dashboard, **opposite of AASTOCKS**. This is re-checked
arithmetically on **every fetch** (`verify_premium_convention`): if the
column ever flips to the H-relative convention it is converted via
`calc.a_premium_from_h_premium`; if it matches neither convention the
fetch raises `ConventionError` instead of storing a wrong sign.
10-stock manual validation: `docs/validation_phase2.md` (all 10 within
1pp of our independent calculation, from two independent price sources).

### Yahoo Finance
- The **yfinance library is unusable here**: its cookie/crumb bootstrap is
  rate-limited (HTTP 429) and its curl_cffi browser-impersonation TLS
  handshake is reset by egress proxies. Not a host block — the plain
  v8 chart endpoint works with a browser User-Agent.
- `sources/yahoo.py` therefore calls
  `query1.finance.yahoo.com/v8/finance/chart/<symbol>` directly with
  `requests` (quotes ~15 min delayed for HK — fine for a monitor).
  CNYHKD=X supplies HKD-per-CNY FX; the quote currency and the
  [0.5, 2.0] band are asserted on every fetch so a flipped rate cannot
  slip in. Requests are spaced ~0.35 s to stay under Yahoo's rate limits.

### Cross-source checks now live
- Yahoo FX vs the FX implied by Eastmoney's own premium figures
  (median across the table): >0.5 % divergence → `fx_divergence` alert +
  source marked degraded. Observed divergence: 0.04–0.08 %.
- akshare vs Yahoo prices for the Focus tier: >2 % → `price_verification`
  alert (loose because Yahoo is delayed).
- Known behaviour: on very-high-premium names (>200 %) the fixed 1pp
  `calc_vs_source` threshold can be crossed by FX timing alone, since the
  difference scales with (1 + premium) × FX-divergence. Three such flags
  fired on 2026-07-15 (Δ ≈ 1.6pp at premiums of 110–272 %). Whether the
  threshold should become relative for such names is an owner decision —
  left absolute for now (conservative: it over-alerts, never under-alerts).

### Historical backfill (added with the 5y valuation metrics)
- **Eastmoney's history API is unusable from this environment**: the kline
  endpoints (`push2his.eastmoney.com/api/qt/stock/kline/get`, bare and
  numbered subdomains) close the connection with an empty reply, while the
  same hosts' spot endpoints work. Verified 2026-07-15 with both curl and
  requests, with browser headers.
- **Tencent daily klines are used instead** (hosts qt.gtimg.cn /
  web.ifzq.gtimg.cn, both reachable): akshare `stock_zh_ah_daily` for the
  H leg and `stock_zh_a_hist_tx` for the A leg, both with `adjust=""` —
  the premium compares actual traded prices, so **unadjusted** closes are
  the correct series. ~25–30 s per company (year-chunked pagination);
  `python -m ahmon.backfill` is resumable and idempotent.
- Premium history is computed by us for dates where **both** markets
  traded (inner join); no source reports historical premiums, so
  `premium_src` stays NULL on `quality='eod'` rows.
- FX history: Yahoo CNYHKD=X daily closes (band/currency-checked),
  forward-filled onto equity trading days. FX candle timestamps convert to
  Asia/Singapore dates, so an individual day's rate can be off by one FX
  session (<0.1 % effect on the premium) — acceptable for percentile/median
  statistics, documented here for exactness.
- Verification: sampled companies' backfilled H closes are compared with
  Yahoo's independent daily history; median divergence > 2 % fires
  `history_verification` and marks the backfill degraded
  (`source_health` row: "history backfill (Tencent prices + Yahoo FX)").

### Live vs sample storage
Live data is written to `data/ahmon_live.db` (`python -m ahmon.refresh`);
the Phase 1 synthetic set stays in `data/ahmon.db`. The refresh **refuses
to write into any database containing `quality='sample'` rows**, so sample
history can never blend into live series or be relabelled. The dashboard
selects its database via the `AHMON_DB` environment variable.

### Still not live in Phase 2
- Dividend yields: the Eastmoney AH table has none, and Yahoo's
  fundamentals API needs the blocked crumb bootstrap — yields display
  as "—" for live rows (candidates: akshare per-stock endpoints, Phase 5).
- HSAHP: Phase 3 (health row shows 'stale' with instructions meanwhile).
- Intraday ticks (`intraday_obs`): Phase 4 scheduler territory; Phase 2
  refresh upserts the current daily row only.
- Stale labelling is wall-clock based (per-tier `STALE_MINUTES`), so
  outside trading hours live rows show as `stale` — technically true but
  noisy; the Phase 4 trading-calendar scheduler will refine it.

## 4. Refresh limitations (summary)
- AASTOCKS: no automated access; free quotes delayed ≥15 min anyway.
- HSAHP: daily EOD only, by design.
- akshare/Eastmoney/Sina: near-real-time but unofficial endpoints — treat
  as best-effort, rate-limit politely (the Phase 4 scheduler's 5/15/30-min
  tiers keep request volume low), and expect occasional schema changes
  (hence the schema guard).
- Yahoo Finance: HK quotes ~15 min delayed for some exchanges; fine for a
  monitor, not for execution.
