"""Trading-calendar awareness for HKEX (H shares) and SSE (A shares).

Uses exchange_calendars (XHKG / XSHG) so holidays and lunch breaks are
respected. Two consumers:

- the scheduler: refresh only while a relevant market is open, plus one
  EOD snapshot after the HK close;
- staleness labelling: data is only 'stale' relative to when fresh data
  could exist. During a session the anchor is "now"; outside it, the
  anchor is the last session close — so an evening refresh stays fresh
  all night instead of being wall-clock-stale 15 minutes after the close.

All functions accept an optional `now` (tz-aware) for testability.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import config

_CALS: dict = {}


def _cal(code: str):
    if code not in _CALS:
        import exchange_calendars as xc
        _CALS[code] = xc.get_calendar(code)
    return _CALS[code]


def _now(now: datetime | None) -> pd.Timestamp:
    ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(config.TZ)
    if ts.tzinfo is None:
        ts = ts.tz_localize(config.TZ)
    return ts.floor("min")


def hk_open(now: datetime | None = None) -> bool:
    """HKEX continuous trading (respects lunch break and holidays)."""
    return bool(_cal("XHKG").is_open_on_minute(_now(now)))


def cn_open(now: datetime | None = None) -> bool:
    """SSE continuous trading."""
    return bool(_cal("XSHG").is_open_on_minute(_now(now)))


def any_market_open(now: datetime | None = None) -> bool:
    return hk_open(now) or cn_open(now)


def last_hk_close(now: datetime | None = None) -> pd.Timestamp:
    """Most recent HKEX close at or before `now` (tz: config.TZ)."""
    return _cal("XHKG").previous_close(_now(now)).tz_convert(config.TZ)


def freshness_anchor(now: datetime | None = None) -> pd.Timestamp:
    """The moment data could last have been refreshed meaningfully.
    While HK trades: now. Otherwise: the last HK close. (H prices freeze
    at the HK close; the A leg alone doesn't unfreeze the premium row.)"""
    ts = _now(now)
    return ts if hk_open(ts) else last_hk_close(ts)


def is_stale(updated_at, classification: str,
             now: datetime | None = None) -> bool:
    """Session-aware staleness: age is measured against the freshness
    anchor, with the per-tier threshold from config.STALE_MINUTES."""
    limit = config.STALE_MINUTES.get(classification, 60)
    upd = pd.Timestamp(updated_at)
    if upd.tzinfo is None:
        upd = upd.tz_localize(config.TZ)
    age_min = (freshness_anchor(now) - upd).total_seconds() / 60.0
    return age_min > limit
