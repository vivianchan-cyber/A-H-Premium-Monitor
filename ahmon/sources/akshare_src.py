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

import pandas as pd

from .. import calc
from . import ConventionError, Source, check_schema, with_retries
from . import tickers

# Eastmoney AH comparison table — required columns (schema guard).
EM_REQUIRED = ["名称", "H股代码", "最新价-HKD", "A股代码", "最新价-RMB", "溢价"]

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
        # backoff → up to ~45s) before declaring the source failed.
        raw = with_retries(ak.stock_zh_ah_spot_em, attempts=5, base_delay=3.0)
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
        return out[(out["a_close"] > 0) & (out["h_close"] > 0)] \
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
        df = self._fetch_table()
        if h_tickers is not None:
            df = df[df["h_ticker"].isin(h_tickers)].reset_index(drop=True)
        premium, convention = verify_premium_convention(df, hkd_per_cny)
        df["premium_src"] = premium
        df["fx_implied"] = ((1.0 + df["premium_src"] / 100.0)
                            * df["h_price_hkd"] / df["a_price_cny"])
        df.attrs["convention"] = convention
        return df
