"""Project-wide configuration and constants."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"
SAMPLE_DB_PATH = DATA_DIR / "ahmon.db"        # Phase 1 synthetic data
LIVE_DB_PATH = DATA_DIR / "ahmon_live.db"     # live feed (Phase 2+)
# AHMON_DB selects which database the dashboard opens (defaults to the
# sample DB so Phase 1 behaviour is unchanged). Live and sample data live
# in separate files so synthetic history can never blend into live series.
DB_PATH = Path(os.environ.get("AHMON_DB", SAMPLE_DB_PATH))

# Hosted deployment (Railway): when DATABASE_URL is set the storage layer
# uses PostgreSQL instead of the SQLite files above. Never hard-code the
# URL — it carries credentials and must live in environment variables.
DATABASE_URL = os.environ.get("DATABASE_URL") or None

TZ = ZoneInfo("Asia/Singapore")

# Classification labels (stored verbatim in the DB — do not rename casually).
FOCUS = "Focus Holding"
PORTFOLIO = "Other Portfolio Holding"
WATCHLIST = "Watchlist"
OTHER = "Other A-H Stock"
CLASSIFICATIONS = [FOCUS, PORTFOLIO, WATCHLIST, OTHER]

# Sector for universe companies not yet classified by the owner.
SECTOR_UNCLASSIFIED = "Unclassified"

SECTORS = [
    "Financials",
    "Oil & Gas",
    "Mining & Materials",
    "Telecom",
    "Industrials",
    "Consumer",
    "Healthcare",
    "Technology",
]

# Alert thresholds (percentage points unless noted).
ALERT_DAILY_PREMIUM_MOVE_PP = 5.0
ALERT_MONTHLY_PREMIUM_MOVE_PP = 10.0
ALERT_H_PRICE_MOVE_PCT = 5.0
ALERT_DISCREPANCY_PP = 1.0
ALERT_ZSCORE = 2.0
# Cross-source checks (Phase 2 live pipeline).
ALERT_FX_DIVERGENCE_PCT = 0.5      # Yahoo FX vs FX implied by Eastmoney
ALERT_PRICE_VERIFY_PCT = 2.0       # akshare vs Yahoo price (Yahoo ~15m delayed)

# Buy-level screen thresholds (ahmon/signals.py). Rule-based screening,
# not investment advice — tune these to taste; every triggered signal
# lists which thresholds it met and by how much.
SIGNAL_MIN_H_UPSIDE_PCT = 15.0     # H upside if premium reverts to 5y median
SIGNAL_MIN_5Y_PERCENTILE = 80.0    # premium unusually wide vs own history
SIGNAL_MIN_H_DIV_YIELD = 4.0       # paid to wait (H leg, %)
SIGNAL_MAX_H_PE = 12.0             # H cheap in absolute terms too (TTM)
SIGNAL_MIN_H_MKTCAP_HKD = 10e9     # liquidity floor (10bn HKD)
# A signal needs the two premium conditions plus at least this many of the
# three quality conditions (yield / P-E / size).
SIGNAL_MIN_QUALITY_HITS = 2

# Data considered stale during market hours after this many minutes without
# a successful refresh (per classification tier).
STALE_MINUTES = {FOCUS: 15, PORTFOLIO: 30, WATCHLIST: 30, OTHER: 60}

# Trading-window refresh cadence in minutes (Phase 4 scheduler).
REFRESH_MINUTES = {FOCUS: 5, PORTFOLIO: 15, WATCHLIST: 15, OTHER: 30}

# Rolling windows in trading days.
WINDOW_1W = 5
WINDOW_1M = 21
WINDOW_3M = 63
WINDOW_1Y = 252
WINDOW_3Y = 756
WINDOW_5Y = 1260
