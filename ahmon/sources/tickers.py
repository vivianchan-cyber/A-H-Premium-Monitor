"""Ticker-format conversions between the DB convention and external feeds.

DB convention (seeded from config/portfolio_sample.csv, Yahoo-compatible):
    H share:  "939.HK"      (no zero padding)
    A share:  "601939.SS"   (Shanghai) / "000001.SZ" (Shenzhen)

Eastmoney (akshare stock_zh_ah_spot_em) uses bare zero-padded codes:
    H share:  "00939"
    A share:  "601939"
"""

from __future__ import annotations


def h_from_em(code: str) -> str:
    """Eastmoney H code -> DB H ticker.  '00939' -> '939.HK'"""
    return f"{int(str(code).strip())}.HK"


def em_from_h(h_ticker: str) -> str:
    """DB H ticker -> Eastmoney H code.  '939.HK' -> '00939'"""
    num = h_ticker.upper().replace(".HK", "").strip()
    return str(int(num)).zfill(5)


def a_from_em(code: str) -> str:
    """Eastmoney A code -> DB A ticker.  '601939' -> '601939.SS'"""
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return f"{code}.SS"
    if code[0] in "03":
        return f"{code}.SZ"
    raise ValueError(f"unrecognised A-share code: {code!r} "
                     "(expected SSE 6xxxxx or SZSE 0/3xxxxx)")


def yahoo_h_symbol(h_ticker: str) -> str:
    """DB H ticker -> Yahoo symbol.  '939.HK' -> '0939.HK' (Yahoo pads to 4)."""
    num = h_ticker.upper().replace(".HK", "").strip()
    return f"{int(num):04d}.HK"


def yahoo_a_symbol(a_ticker: str) -> str:
    """DB A tickers are already Yahoo symbols ('601939.SS')."""
    return a_ticker.strip()


def tx_from_a(a_ticker: str) -> str:
    """DB A ticker -> Tencent symbol.  '601939.SS' -> 'sh601939'"""
    code, _, suffix = a_ticker.strip().partition(".")
    prefix = {"SS": "sh", "SZ": "sz"}.get(suffix.upper())
    if prefix is None:
        raise ValueError(f"unrecognised A-share ticker: {a_ticker!r}")
    return f"{prefix}{code}"
