"""Phase 2 manual validation: pull live A price, H price and CNY/HKD for
N stocks, compute OUR premium independently from both sources' prices, and
compare against the source-reported figure. Any |calc − source| > 1pp is
flagged (the calc_vs_source rule).

Two independent computations per stock:
  premium (EM prices)    = calc.a_share_premium(A_em,    H_em,    fx_yahoo)
  premium (Yahoo prices) = calc.a_share_premium(A_yahoo, H_yahoo, fx_yahoo)
The second uses no Eastmoney number at all, so agreement between the two
confirms both the prices and the premium convention end to end. Yahoo HK
quotes are ~15 min delayed, so small timing differences are expected.

Run:  python -m ahmon.validate            # default: 10 Focus names
      python -m ahmon.validate --out docs/validation_phase2.md
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import calc, config
from .sources.akshare_src import AkshareSource
from .sources.yahoo import YahooSource

DEFAULT_N = 10


def build_validation_table(n: int = DEFAULT_N,
                           ak_source=None, yahoo_source=None
                           ) -> tuple[pd.DataFrame, dict]:
    ak_source = ak_source or AkshareSource()
    yahoo_source = yahoo_source or YahooSource()

    fx_info = yahoo_source.fetch_fx()
    fx = fx_info["hkd_per_cny"]
    quotes = ak_source.fetch_quotes(hkd_per_cny=fx)

    port = pd.read_csv(config.CONFIG_DIR / "portfolio_sample.csv")
    focus = port[port["classification"] == config.FOCUS]
    picks = focus[focus["h_ticker"].isin(quotes["h_ticker"])].head(n)
    if len(picks) < n:   # top up from the rest of the portfolio file
        more = port[port["h_ticker"].isin(quotes["h_ticker"])
                    & ~port["h_ticker"].isin(picks["h_ticker"])]
        picks = pd.concat([picks, more.head(n - len(picks))])

    yq = yahoo_source.fetch_quotes(list(picks["h_ticker"]),
                                   list(picks["a_ticker"])).set_index("h_ticker")
    em = quotes.set_index("h_ticker")

    rows = []
    for _, p in picks.iterrows():
        h = p["h_ticker"]
        e = em.loc[h]
        prem_em = calc.a_share_premium(e["a_price_cny"], e["h_price_hkd"], fx)
        y = yq.loc[h]
        prem_y = None
        if not y["error"] and y["h_price_hkd"] and y["a_price_cny"]:
            prem_y = calc.a_share_premium(y["a_price_cny"], y["h_price_hkd"],
                                          fx)
        diff = prem_em - e["premium_src"]
        rows.append({
            "Company": p["name_en"], "H Ticker": h, "A Ticker": p["a_ticker"],
            "A (CNY, EM)": e["a_price_cny"], "H (HKD, EM)": e["h_price_hkd"],
            "A (CNY, Yahoo)": y["a_price_cny"],
            "H (HKD, Yahoo)": y["h_price_hkd"],
            "Our premium, EM prices (%)": round(prem_em, 2),
            "Our premium, Yahoo prices (%)":
                None if prem_y is None else round(prem_y, 2),
            "Source premium, EM (%)": round(float(e["premium_src"]), 2),
            "Calc−src diff (pp)": round(diff, 2),
            ">1pp flag": "⚠️ FLAG" if calc.discrepancy_flag(
                prem_em, float(e["premium_src"])) else "ok",
        })
    meta = {"fx": fx, "fx_asof": fx_info["asof"],
            "convention": quotes.attrs.get("convention"),
            "universe_rows": len(quotes)}
    return pd.DataFrame(rows), meta


def to_markdown(table: pd.DataFrame, meta: dict) -> str:
    lines = [
        "# Phase 2 — 10-stock live validation",
        "",
        f"Run at {pd.Timestamp.now(tz=config.TZ):%Y-%m-%d %H:%M %Z} · "
        f"CNY/HKD = **{meta['fx']:.4f}** (Yahoo CNYHKD=X, HKD per 1 CNY, "
        f"as of {meta['fx_asof']:%H:%M}) · Eastmoney AH table: "
        f"{meta['universe_rows']} companies · detected premium convention: "
        f"**{meta['convention']}** (positive = A above H — same as the "
        "dashboard standard, opposite of AASTOCKS).",
        "",
        "Our premium is computed independently twice — once from Eastmoney "
        "prices, once from Yahoo prices (no Eastmoney number involved) — "
        "with `((A_CNY × HKD_per_CNY) / H_HKD − 1) × 100`. "
        "`Calc−src diff` compares our EM-price computation with Eastmoney's "
        "own displayed premium; > 1pp fires the `calc_vs_source` rule. "
        "Yahoo quotes are ~15 min delayed, so the Yahoo column can differ "
        "slightly when prices are moving.",
        "",
        table.to_markdown(index=False),
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Phase 2 live validation table")
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--out", default=None,
                   help="also write a markdown report to this path")
    args = p.parse_args(argv)
    table, meta = build_validation_table(args.n)
    md = to_markdown(table, meta)
    print(md)
    if args.out:
        with open(args.out, "w") as f:
            f.write(md)
        print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
