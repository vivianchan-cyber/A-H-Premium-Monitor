"""Project-wide configuration and constants."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"
DB_PATH = DATA_DIR / "ahmon.db"

TZ = ZoneInfo("Asia/Singapore")

# Classification labels (stored verbatim in the DB — do not rename casually).
FOCUS = "Focus Holding"
PORTFOLIO = "Other Portfolio Holding"
WATCHLIST = "Watchlist"
OTHER = "Other A-H Stock"
CLASSIFICATIONS = [FOCUS, PORTFOLIO, WATCHLIST, OTHER]

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
