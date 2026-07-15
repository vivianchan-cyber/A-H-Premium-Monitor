"""Yahoo Finance — independent verification source for A/H prices and the
primary CNY/HKD FX source (symbol CNYHKD=X, i.e. HKD per 1 CNY).

Implementation note (2026-07-15): the yfinance *library* is unusable from
this environment — its cookie/crumb bootstrap is rate-limited (HTTP 429)
and its curl_cffi browser-impersonation handshake is reset by the egress
proxy. The public v8 chart endpoint (query1.finance.yahoo.com) works fine
with a plain browser User-Agent, so this module calls it directly with
`requests`. HK quotes are ~15 min delayed — fine for a monitor.
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import requests

from .. import config
from . import SchemaChangeError, Source, with_retries
from . import tickers

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
FX_SYMBOL = "CNYHKD=X"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
}
# Polite spacing between chart calls; Yahoo 429s aggressive clients.
REQUEST_GAP_S = 0.35


class YahooSource(Source):
    name = "Yahoo Finance (verification & FX)"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    # ------------------------------------------------------------- internals

    def _chart_meta(self, symbol: str) -> dict:
        def call():
            r = self.session.get(CHART_URL.format(symbol=symbol),
                                 params={"range": "1d", "interval": "1d"},
                                 timeout=20)
            r.raise_for_status()
            return r.json()

        payload = with_retries(call)
        try:
            meta = payload["chart"]["result"][0]["meta"]
            out = {
                "symbol": symbol,
                "price": float(meta["regularMarketPrice"]),
                "currency": meta["currency"],
                "market_time": datetime.fromtimestamp(
                    meta["regularMarketTime"], tz=config.TZ),
            }
        except (KeyError, IndexError, TypeError) as e:
            raise SchemaChangeError(
                f"yahoo chart {symbol}: unexpected payload shape ({e}); "
                f"keys={list(payload)[:5]}") from e
        time.sleep(REQUEST_GAP_S)
        return out

    # ----------------------------------------------------------------- API

    def fetch_fx(self) -> dict:
        """HKD per 1 CNY from CNYHKD=X, with its quote timestamp."""
        m = self._chart_meta(FX_SYMBOL)
        if m["currency"] != "HKD":
            raise SchemaChangeError(
                f"CNYHKD=X quoted in {m['currency']!r}, expected HKD — "
                "FX direction can no longer be trusted")
        if not 0.5 <= m["price"] <= 2.0:
            raise SchemaChangeError(
                f"CNYHKD=X rate {m['price']} outside the plausible "
                "HKD-per-CNY band [0.5, 2.0]")
        return {"hkd_per_cny": m["price"], "asof": m["market_time"]}

    def fetch_quotes(self, h_tickers: list[str],
                     a_tickers: list[str] | None = None) -> pd.DataFrame:
        """Latest H (and optionally matching A) prices for verification.
        Currency of every quote is checked against its exchange so a wrong
        symbol mapping cannot slip through."""
        rows = []
        pairs = zip(h_tickers, a_tickers or [None] * len(h_tickers))
        for h, a in pairs:
            row = {"h_ticker": h, "a_ticker": a,
                   "h_price_hkd": None, "a_price_cny": None,
                   "h_asof": None, "a_asof": None, "error": None}
            try:
                hm = self._chart_meta(tickers.yahoo_h_symbol(h))
                if hm["currency"] != "HKD":
                    raise SchemaChangeError(
                        f"{h}: H quote in {hm['currency']!r}, expected HKD")
                row.update(h_price_hkd=hm["price"], h_asof=hm["market_time"])
                if a is not None:
                    am = self._chart_meta(tickers.yahoo_a_symbol(a))
                    if am["currency"] != "CNY":
                        raise SchemaChangeError(
                            f"{a}: A quote in {am['currency']!r}, expected CNY")
                    row.update(a_price_cny=am["price"],
                               a_asof=am["market_time"])
            except Exception as e:   # noqa: BLE001 — per-ticker isolation
                row["error"] = f"{type(e).__name__}: {e}"
            rows.append(row)
        return pd.DataFrame(rows)

    def fetch_universe(self) -> pd.DataFrame:
        raise NotImplementedError(
            "Yahoo is the verification/FX source; the A–H universe comes "
            "from the primary source (akshare) only — never substituted.")
