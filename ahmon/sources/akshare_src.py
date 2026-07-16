"""akshare / Eastmoney — primary live source for the A–H universe, A/H
prices and the source-reported premium.

Endpoint: ak.stock_zh_ah_spot_em() — Eastmoney's "AH股比价" table
(push2.eastmoney.com). One row per dual-listed company with the H code,
A code, both prices and a premium column (溢价).

Convention (verified live 2026-07-15, re-checked arithmetically on every
fetch by `verify_premium_convention`): Eastmoney's 溢价 is the A-share
premium — positive means the A share trades above the H share — i.e. the
SAME convention as this dashboard and the OPPOSITE of AASTOCKS. If the
column ever flips to the H-relative convention the guard converts it via
calc.a_premium_from_h_premium; if it matches neither convention the fetch
fails loudly (ConventionError) rather than storing a wrong sign.
"""

from __future__ import annotations

import time

import pandas as pd
import requests

from .. import calc
from . import ConventionError, Source, check_schema, with_retries
from . import tickers

# Eastmoney AH comparison table — required columns (schema guard).
EM_REQUIRED = ["名称", "H股代码", "最新价-HKD", "A股代码", "最新价-RMB", "溢价"]

# Delayed mirror of the same clist API, used only when the realtime host
# is down (push2.eastmoney.com 502s in long bursts). Quotes are ~15 min
# delayed; the switch is surfaced in source_health, never silent.
EM_MIRROR_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
EM_PARAMS = {
    "np": "1", "fltt": "1", "invt": "2", "fs": "b:DLMK0101",
    "fields": "f193,f191,f192,f12,f13,f14,f1,f2,f4,f3,f152,"
              "f186,f190,f187,f189,f188",
    "fid": "f3", "pn": "1", "pz": "100", "po": "1", "dect": "1",
    "wbp2u": "|0|0|0|web",
}
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0.0.0 Safari/537.36"}


# Per-stock fundamentals from the same quote API (ulist variant, explicit
# secids). Field ids verified empirically 2026-07-15 — including via the
# premium identity: H yield / A yield reproduced (1 + premium) exactly.
#   f9  = P/E (TTM, ×100)      f23  = P/B (×100)
#   f20 = total market cap in the leg's own currency (raw)
#   f133 = dividend yield in % (raw float)
EM_ULIST_URL = "/api/qt/ulist.np/get"
EM_STATS_FIELDS = "f12,f13,f14,f9,f20,f23,f133"
EM_HOSTS = ["https://push2.eastmoney.com", "https://push2delay.eastmoney.com"]


def parse_ulist_stats(rows: list[dict]) -> pd.DataFrame:
    """Decode raw ulist rows into per-leg stats. f13: 116=HK, 1=SH, 0=SZ."""
    out = []
    for r in rows:
        num = lambda k, scale=1.0: (  # noqa: E731
            None if r.get(k) in (None, "-", "") else float(r[k]) / scale)
        out.append({
            "code": str(r["f12"]),
            "leg": "H" if r.get("f13") == 116 else "A",
            "pe_ttm": num("f9", 100.0),
            "pb": num("f23", 100.0),
            "mktcap": num("f20"),
            "div_yield_pct": num("f133"),
        })
    return pd.DataFrame(out)


def _fetch_em_mirror() -> pd.DataFrame:
    """Same AH comparison table from the delayed mirror, reproducing
    akshare's column names and scaling exactly (verified against the
    realtime host's output)."""
    frames, pn, total = [], 1, None
    while total is None or (pn - 1) * 100 < total:
        params = dict(EM_PARAMS, pn=str(pn))
        r = requests.get(EM_MIRROR_URL, params=params, headers=EM_HEADERS,
                         timeout=20)
        r.raise_for_status()
        data = r.json()["data"]
        total = data["total"]
        frames.append(pd.DataFrame(data["diff"]))
        pn += 1
        time.sleep(0.5)
    df = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "名称": df["f193"],
        "H股代码": df["f12"],
        "最新价-HKD": pd.to_numeric(df["f2"], errors="coerce") / 1000,
        "H股-涨跌幅": pd.to_numeric(df["f3"], errors="coerce") / 100,
        "A股代码": df["f191"],
        "最新价-RMB": pd.to_numeric(df["f186"], errors="coerce") / 100,
        "A股-涨跌幅": pd.to_numeric(df["f187"], errors="coerce") / 100,
        "比价": pd.to_numeric(df["f189"], errors="coerce") / 100,
        "溢价": pd.to_numeric(df["f188"], errors="coerce") / 100,
    })
    return out

# Median |reported − recomputed| tolerance in pp for accepting a convention.
# Generous because EM's own FX snapshot differs slightly from ours.
CONVENTION_TOL_PP = 2.0


def verify_premium_convention(df: pd.DataFrame,
                              hkd_per_cny: float) -> tuple[pd.Series, str]:
    """Recompute the premium from the table's own prices and decide which
    convention the reported figure uses. Returns (premium in OUR A-share
    convention, detected convention name). Raises ConventionError when the
    reported column matches neither convention."""
    ours = df.apply(lambda r: calc.a_share_premium(
        r["a_price_cny"], r["h_price_hkd"], hkd_per_cny), axis=1)
    raw = df["premium_src_raw"].astype(float)
    err_a = (raw - ours).abs().median()
    err_h = (raw.map(calc.a_premium_from_h_premium) - ours).abs().median()
    if err_a <= CONVENTION_TOL_PP and err_a <= err_h:
        return raw, "a_premium"
    if err_h <= CONVENTION_TOL_PP:
        return raw.map(calc.a_premium_from_h_premium), "h_premium"
    raise ConventionError(
        f"akshare/Eastmoney premium matches neither convention "
        f"(median |Δ| A-convention {err_a:.2f}pp, H-convention {err_h:.2f}pp, "
        f"tolerance {CONVENTION_TOL_PP}pp) — refusing to store it")


class AkshareSource(Source):
    name = "akshare (A-H universe & prices)"

    def _fetch_table(self) -> pd.DataFrame:
        import akshare as ak  # deferred: keeps offline tests import-free
        # push2.eastmoney.com 502s in bursts; be patient (5 tries, 3s base
        # backoff → up to ~45s), then fall back to the delayed mirror —
        # recorded via attrs and surfaced in source_health, never silent.
        host = "push2 (realtime)"
        try:
            raw = with_retries(ak.stock_zh_ah_spot_em, attempts=5,
                               base_delay=3.0)
        except Exception:
            raw = with_retries(_fetch_em_mirror, attempts=3, base_delay=3.0)
            host = "push2delay (15-min delayed mirror)"
        check_schema(raw, EM_REQUIRED, "akshare stock_zh_ah_spot_em")
        df = pd.DataFrame({
            "name_zh": raw["名称"],
            "h_ticker": raw["H股代码"].map(tickers.h_from_em),
            "a_ticker": raw["A股代码"].map(tickers.a_from_em),
            "h_price_hkd": pd.to_numeric(raw["最新价-HKD"], errors="coerce"),
            "a_price_cny": pd.to_numeric(raw["最新价-RMB"], errors="coerce"),
            "premium_src_raw": pd.to_numeric(raw["溢价"], errors="coerce"),
        })
        # Suspended/blank lines: drop rather than ingest zeros.
        before = len(df)
        df = df[(df["h_price_hkd"] > 0) & (df["a_price_cny"] > 0)
                & df["premium_src_raw"].notna()]
        # The EM endpoint paginates a live-resorting list, so one stock can
        # appear on two pages within a single fetch — keep the first row.
        df = df.drop_duplicates("h_ticker", keep="first").reset_index(drop=True)
        df.attrs["dropped_rows"] = before - len(df)
        df.attrs["em_host"] = host
        return df

    def fetch_universe(self) -> pd.DataFrame:
        """Current A–H universe: name_zh, h_ticker, a_ticker."""
        return self._fetch_table()[["name_zh", "h_ticker", "a_ticker"]]

    def fetch_history(self, h_ticker: str, a_ticker: str,
                      start_year: int) -> pd.DataFrame:
        """Unadjusted daily closes for both legs from Tencent (the Eastmoney
        history API returns empty replies from some networks — see
        docs/source_notes.md). Returns date / a_close / h_close on the dates
        where BOTH markets traded (inner join): the premium is only defined
        when both legs have a same-day close."""
        import akshare as ak
        start = f"{start_year}0101"
        end = pd.Timestamp.now(tz=None).strftime("%Y%m%d")
        h = with_retries(lambda: ak.stock_zh_ah_daily(
            symbol=tickers.em_from_h(h_ticker), start_year=str(start_year),
            end_year=end[:4], adjust=""), attempts=5, base_delay=3.0)
        check_schema(h, ["日期", "收盘"], f"stock_zh_ah_daily {h_ticker}")
        a = with_retries(lambda: ak.stock_zh_a_hist_tx(
            symbol=tickers.tx_from_a(a_ticker), start_date=start,
            end_date=end, adjust=""), attempts=5, base_delay=3.0)
        check_schema(a, ["date", "close"], f"stock_zh_a_hist_tx {a_ticker}")
        hh = pd.DataFrame({"date": pd.to_datetime(h["日期"]),
                           "h_close": pd.to_numeric(h["收盘"],
                                                    errors="coerce")})
        aa = pd.DataFrame({"date": pd.to_datetime(a["date"]),
                           "a_close": pd.to_numeric(a["close"],
                                                    errors="coerce")})
        out = aa.merge(hh, on="date", how="inner").dropna()
        out = out[(out["a_close"] > 0) & (out["h_close"] > 0)]
        # Tencent's year-chunked pagination repeats boundary dates in both
        # legs; keep one close per date so row counts are truthful (the
        # idempotent upsert would collapse them anyway).
        return out.drop_duplicates("date", keep="last") \
            .sort_values("date").reset_index(drop=True)

    def fetch_quotes(self, h_tickers: list[str] | None = None,
                     hkd_per_cny: float | None = None) -> pd.DataFrame:
        """Latest A/H prices + source premium (normalised to our A-share
        convention — see verify_premium_convention). `hkd_per_cny` (from the
        independent FX source) is required for the convention check.
        Adds `fx_implied`: the FX rate embedded in EM's own premium figure,
        for the cross-source FX divergence check."""
        if hkd_per_cny is None:
            raise ValueError("hkd_per_cny is required to verify the premium "
                             "convention before the figure can be trusted")
        table = self._fetch_table()
        df = table
        if h_tickers is not None:
            df = df[df["h_ticker"].isin(h_tickers)].reset_index(drop=True)
        df.attrs.update(table.attrs)   # filtering can drop DataFrame attrs
        premium, convention = verify_premium_convention(df, hkd_per_cny)
        df["premium_src"] = premium
        df["fx_implied"] = ((1.0 + df["premium_src"] / 100.0)
                            * df["h_price_hkd"] / df["a_price_cny"])
        df.attrs["convention"] = convention
        return df

    def fetch_stats(self, pairs: list[tuple[str, str]]) -> pd.DataFrame:
        """Per-company fundamentals for both legs: P/E (TTM), P/B, market
        cap (leg currency) and dividend yield. `pairs` is a list of
        (h_ticker, a_ticker). Returns one row per h_ticker with h_*/a_*
        columns; missing values stay None (never guessed)."""
        secid_to_h: dict[str, str] = {}
        secids = []
        for h, a in pairs:
            h_code = tickers.em_from_h(h)
            a_code, _, suffix = a.partition(".")
            a_mkt = "1" if suffix.upper() == "SS" else "0"
            secids.append(f"116.{h_code}")
            secids.append(f"{a_mkt}.{a_code}")
            secid_to_h[f"H:{h_code}"] = h
            secid_to_h[f"A:{a_code}"] = h

        raw_rows: list[dict] = []
        for i in range(0, len(secids), 50):
            batch = ",".join(secids[i:i + 50])

            def call(batch=batch):
                last = None
                for base in EM_HOSTS:
                    try:
                        r = requests.get(
                            base + EM_ULIST_URL,
                            params={"fltt": "1", "invt": "2", "np": "1",
                                    "secids": batch,
                                    "fields": EM_STATS_FIELDS},
                            headers=EM_HEADERS, timeout=20)
                        r.raise_for_status()
                        return r.json()["data"]["diff"]
                    except Exception as e:   # noqa: BLE001
                        last = e
                raise last

            raw_rows.extend(with_retries(call, attempts=3, base_delay=2.0))
            time.sleep(0.3)

        stats = parse_ulist_stats(raw_rows)
        stats["h_ticker"] = (stats["leg"] + ":" + stats["code"]).map(secid_to_h)
        stats = stats.dropna(subset=["h_ticker"])
        wide = {}
        for _, s in stats.iterrows():
            leg = s["leg"].lower()
            row = wide.setdefault(s["h_ticker"], {"h_ticker": s["h_ticker"]})
            row[f"{leg}_pe"] = s["pe_ttm"]
            row[f"{leg}_pb"] = s["pb"]
            row[f"{leg}_mktcap"] = s["mktcap"]
            row[f"{leg}_div_yield"] = s["div_yield_pct"]
        return pd.DataFrame(list(wide.values()))
