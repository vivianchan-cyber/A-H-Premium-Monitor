"""HSAHP — Hang Seng Stock Connect China AH Premium Index.

The official Hang Seng Indexes page blocks automated access and the daily
series is licensed (docs/source_notes.md §2), so this module reads the
**Eastmoney mirror** of the index (secid 100.HSAHP), which was validated
on 2026-07-16 before being trusted (§3b of the source notes):
history depth matches the official index (back to 2006), and
(HSAHP − 100) tracks this project's own independently computed
cap-weighted average premium with correlation 0.945 and a stable,
methodology-explained offset (free-float vs total-cap weighting).

Reading: >100 ⇒ A shares trade at a premium; implied average A-share
premium = HSAHP − 100. The official series is EOD; the mirror also shows
an intraday value for today, which the daily upsert overwrites until the
close becomes final. The monthly Hang Seng factsheet remains the manual
cross-check, and CSV import stays the emergency fallback.
"""

from __future__ import annotations

import pandas as pd
import requests

from . import SchemaChangeError, with_retries

SECID = "100.HSAHP"
KLINE_PATH = "/api/qt/stock/kline/get"
SPOT_PATH = "/api/qt/ulist.np/get"
HOSTS = ["https://push2his.eastmoney.com", "https://push2.eastmoney.com"]
SPOT_HOSTS = ["https://push2.eastmoney.com",
              "https://push2delay.eastmoney.com"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0.0.0 Safari/537.36"}
# Plausibility band: the index has ranged roughly 88–213 since 2006.
# Anything outside this band means the mirror changed meaning — refuse it.
BAND = (60.0, 260.0)
SOURCE_LABEL = "eastmoney_mirror(100.HSAHP)"


def parse_klines(klines: list[str]) -> pd.Series:
    """'2026-07-16,123.40' lines -> date-indexed close series, validated."""
    if not klines:
        raise SchemaChangeError("HSAHP mirror: empty kline payload")
    dates, closes = [], []
    for line in klines:
        d, c = line.split(",")[:2]
        dates.append(pd.Timestamp(d))
        closes.append(float(c))
    s = pd.Series(closes, index=pd.DatetimeIndex(dates), name="HSAHP")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    bad = s[(s < BAND[0]) | (s > BAND[1])]
    if len(bad):
        raise SchemaChangeError(
            f"HSAHP mirror: {len(bad)} values outside plausible band "
            f"{BAND} (e.g. {bad.index[0]:%Y-%m-%d} = {bad.iloc[0]}) — "
            "refusing the series")
    return s


class HsahpSource:
    name = "HSAHP index series"

    def fetch_history(self, beg: str = "0") -> pd.Series:
        """Daily closes from `beg` (YYYYMMDD, '0' = full history to 2006)."""
        def call():
            last = None
            for base in HOSTS:
                try:
                    r = requests.get(
                        base + KLINE_PATH,
                        params={"secid": SECID, "klt": "101", "fqt": "0",
                                "beg": beg, "end": "20500101",
                                "fields1": "f1", "fields2": "f51,f53"},
                        headers=HEADERS, timeout=30)
                    r.raise_for_status()
                    klines = r.json()["data"]["klines"]
                    if not klines:
                        # push2 answers kline requests with an empty list
                        # rather than an error — that is a failure, not a
                        # result, or it would mask the real history host.
                        raise SchemaChangeError(
                            f"{base}: empty kline payload for {SECID}")
                    return klines
                except Exception as e:      # noqa: BLE001
                    last = e
            raise last

        return parse_klines(with_retries(call, attempts=4, base_delay=2.0))

    def fetch_spot(self) -> dict:
        """Latest (intraday) level and previous official close."""
        def call():
            last = None
            for base in SPOT_HOSTS:
                try:
                    r = requests.get(
                        base + SPOT_PATH,
                        params={"fltt": "2", "invt": "2", "np": "1",
                                "secids": SECID, "fields": "f12,f14,f2,f18"},
                        headers=HEADERS, timeout=20)
                    r.raise_for_status()
                    return r.json()["data"]["diff"][0]
                except Exception as e:      # noqa: BLE001
                    last = e
            raise last

        row = with_retries(call, attempts=3, base_delay=2.0)
        level, prev = float(row["f2"]), float(row["f18"])
        for v in (level, prev):
            if not BAND[0] <= v <= BAND[1]:
                raise SchemaChangeError(
                    f"HSAHP spot {v} outside plausible band {BAND}")
        return {"level": level, "prev_close": prev}
