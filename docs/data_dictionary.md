# Data dictionary

## Database tables (SQLite — `data/ahmon.db`)

### companies
| column | type | meaning |
|---|---|---|
| id | int PK | internal id |
| name_en | text | English company name |
| name_zh | text | Chinese company name (nullable) |
| h_ticker | text unique | H-share ticker, e.g. `2628.HK` |
| a_ticker | text | A-share ticker, e.g. `601628.SS` / `002594.SZ` |
| sector | text | one of the eight dashboard sectors |

### classifications (one row per company)
| column | meaning |
|---|---|
| classification | `Focus Holding` \| `Other Portfolio Holding` \| `Watchlist` \| `Other A-H Stock` |
| owned | 1 when the portfolio owns the stock (implied by Focus/Portfolio) |
| focus | 1 for Focus Holdings |
| weight | optional portfolio weight (%) |
| shares_held | optional share count |
| ref_premium_jul2024 | optional July-2024 reference premium (pp) |
| notes | optional analyst notes |
| changed_at | ISO timestamp of last classification change |

### classification_audit
Append-only log: company, old → new classification, timestamp, source
(`seed:…`, `dashboard`, `dashboard:promote`, `dashboard:demote`, `test`).

### daily_obs (PK company_id + date — duplicates impossible)
| column | meaning |
|---|---|
| a_close / h_close | closing prices, CNY / HKD |
| fx | **HKD per 1 CNY** on that date |
| premium_calc | our A-share premium (%) — the standard definition |
| premium_src | source-reported premium **after conversion to our convention** |
| a_div_yield / h_div_yield | trailing dividend yields (%) |
| quality | `live` (spot refresh) \| `eod` (history backfill) \| `ok` \| `stale` \| `sample` \| `manual_import` \| `failed` |
| updated_at | last successful write (Asia/Singapore ISO) |

### intraday_obs (Focus Holdings only; PK company_id + ts)
Same price/premium fields at 5-minute resolution; `a_is_close=1` marks
ticks taken after the mainland close (A price = latest A-share close).

### company_stats (one row per company, refreshed with each live refresh)
| column | meaning |
|---|---|
| h_mktcap_hkd / a_mktcap_cny | total market value priced off each leg (HKD / CNY) |
| h_pe / a_pe | trailing (TTM) price-to-earnings per leg |
| h_pb / a_pb | price-to-book per leg |
| h_div_yield / a_div_yield | dividend yield % per leg (H yield ≈ A yield × (1 + premium) — the identity used to verify the source fields) |
| updated_at | last refresh (Asia/Singapore ISO) |

Source: Eastmoney quote API (ulist), both legs per company; missing values
(e.g. zero-dividend or loss-making companies) stay NULL — never guessed.

### hsahp_daily
Date-keyed HSAHP closes with the supplying `source` (`sample`,
`manual_import`, later `factsheet`/`mirror`).

### commentary_log
Scheduler-written commentary: timestamp, kind (`daily` | `closing` |
`weekly` | `monthly`), markdown body. Shown in the Commentary tab.

### alerts_log / source_health / constituent_log
Fired alerts (rule, severity, message); per-source status
(`ok|degraded|failed|stale|sample`, last success/error); added/removed
A–H constituents detected on universe refresh.

## Monitor-table columns (dashboard)
Derived in `ahmon/metrics.py`: English and Chinese company names with both
stock codes (H ticker / A ticker); latest prices and FX; `Premium calc (%)`;
per-leg market cap (bn, leg currency), P/E (TTM), P/B and dividend yields;
`Premium src (%)`; `Calc-src diff (pp)`; premium changes Δ1d/Δ1w/Δ1m/Δ3m/
ΔYTD/Δ1y (percentage points); H/A 1-day returns (%); dividend yields;
**historical valuation metrics** — 3y & 5y median premium, distance (gap)
from the 3y and 5y medians, and the 5y percentile of today's premium;
52-week percentile, high and low; 1-year z-score; last update time;
quality label. The 5y metrics need ≥630 stored observations
(`python -m ahmon.backfill` populates ~6 years).

Window conventions: 1w = 5 trading days, 1m = 21, 3m = 63, 1y = 252,
3y = 756, 5y = 1260. YTD = change since the final observation of the prior
calendar year. Percentile = share of the trailing window at or below the
latest value.

## Chart range policy
3m → daily · 1y → weekly (W-FRI last) · 3y/5y/max → monthly (month-end
last). Daily history is always stored regardless of display resolution.
