"""Data-source layer.

Every source implements `Source` and reports health through
db.set_source_health. Rules (see CLAUDE.md):
- Never silently substitute a different source.
- Never reuse an old value without marking it stale.
- Validate the schema of anything fetched before ingesting it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

import pandas as pd


class SchemaChangeError(RuntimeError):
    """Raised when a source's payload no longer matches the expected shape."""


def check_schema(df: pd.DataFrame, required_columns: list[str], source: str):
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        raise SchemaChangeError(
            f"{source}: expected columns missing {missing} — "
            f"got {list(df.columns)[:20]}")


def with_retries(fn, attempts: int = 4, base_delay: float = 2.0):
    """Call fn with exponential backoff (2s, 4s, 8s …). Re-raises the last
    error so callers can mark the source failed rather than hiding it."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:          # noqa: BLE001 — deliberate catch-all
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last


class Source(ABC):
    name: str = "abstract"

    @abstractmethod
    def fetch_universe(self) -> pd.DataFrame:
        """Return the current A–H universe (name, h_ticker, a_ticker …)."""

    @abstractmethod
    def fetch_quotes(self, h_tickers: list[str]) -> pd.DataFrame:
        """Return latest A/H prices + source-reported premium."""
