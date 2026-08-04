"""A–H Premium Monitor — Streamlit dashboard (Phase 1: sample data)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ahmon import (alerts, auth, calc, commentary, config, db, metrics,
                   sample_data)
from ahmon.sources import csv_import

# ---------------------------------------------------------------- palette
# Validated dataviz palette (light mode) — see skill references/palette.md.
C = {"blue": "#2a78d6", "green": "#008300", "magenta": "#e87ba4",
     "yellow": "#eda100", "aqua": "#1baf7a", "orange": "#eb6834",
     "violet": "#4a3aa7", "red": "#e34948"}
SERIES = list(C.values())
INK = {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
       "grid": "#e1e0d9", "axis": "#c3c2b7", "surface": "#fcfcfb"}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a",
          "critical": "#d03b3b"}
SECTOR_COLOR = {s: SERIES[i % 8] for i, s in enumerate(config.SECTORS)}


def styled(fig: go.Figure, height=380) -> go.Figure:
    fig.update_layout(
        height=height, plot_bgcolor=INK["surface"], paper_bgcolor=INK["surface"],
        font=dict(family="system-ui, sans-serif", color=INK["secondary"],
                  size=12),
        margin=dict(l=10, r=10, t=36, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(gridcolor=INK["grid"], linecolor=INK["axis"],
                     zerolinecolor=INK["axis"], tickfont=dict(color=INK["muted"]))
    fig.update_yaxes(gridcolor=INK["grid"], linecolor=INK["axis"],
                     zerolinecolor=INK["axis"], tickfont=dict(color=INK["muted"]))
    return fig


RANGE_KEYS = {"3m (daily)": "3m", "1y (weekly)": "1y", "3y (monthly)": "3y",
              "5y (monthly)": "5y", "Max (monthly)": "max"}


def line_chart(s: pd.Series, name: str, color=C["blue"], height=380):
    fig = go.Figure(go.Scatter(x=s.index, y=s.values, name=name,
                               mode="lines", line=dict(color=color, width=2)))
    return styled(fig, height)


# ------------------------------------------------------------------ data

@st.cache_resource
def get_conn():
    if config.DATABASE_URL:                 # hosted mode: PostgreSQL
        return db.connect()
    if not config.DB_PATH.exists():
        if config.DB_PATH == config.SAMPLE_DB_PATH:
            with st.spinner("Building sample database (first run)…"):
                sample_data.build()
        else:
            st.error(f"Database {config.DB_PATH} does not exist. For live "
                     "data run `python -m ahmon.refresh` first (writes "
                     f"{config.LIVE_DB_PATH.name}).")
            st.stop()
    return db.connect(config.DB_PATH)


@st.cache_data(ttl=300, show_spinner="Computing monitor table…")
def load(version: int):
    """Heavy per-company metrics, cached per data version: widget
    interactions rerun the script but reuse this result; refreshes and
    imports bump st.session_state['data_version'] to force a recompute.
    The 5-minute ttl bounds staleness when data changes outside the app
    (the cron refresh service writes to the shared database directly)."""
    conn = get_conn()
    table = metrics.monitor_table(conn)
    att = metrics.attribution_table(conn, table)
    return table, att


st.set_page_config(page_title="A–H Premium Monitor", page_icon="📈",
                   layout="wide")
user = auth.require_login()
IS_ADMIN = user["role"] == "admin"
conn = get_conn()
st.session_state.setdefault("data_version", 0)
table, att = load(st.session_state["data_version"])
hsahp = db.hsahp_series(conn)


def sidebar_user_badge():
    if user.get("local_dev"):
        st.caption("🔓 local development mode — no login configured")
    else:
        c1, c2 = st.columns([3, 1])
        c1.caption(f"👤 {user['email']} · **{user['role']}**")
        if c2.button("Log out"):
            auth.logout()
            st.rerun()


# ------------------------------------------------- empty-database first run
if table.empty:
    with st.sidebar:
        st.title("A–H Premium Monitor")
        sidebar_user_badge()
    st.info("📭 **No data available yet — migrate or refresh the "
            "database.** The application is running and connected; its "
            "database just has no market data in it.", icon="ℹ️")
    if IS_ADMIN:
        st.markdown(
            "**To load data (admin):**\n\n"
            "1. *Best*: migrate your existing local history — on your own "
            "machine run\n"
            "   ```bash\n"
            "   DATABASE_URL=\"<DATABASE_PUBLIC_URL from Railway>\" \\\n"
            "     python -m ahmon.migrate --sqlite data/ahmon_live.db "
            "--replace\n"
            "   ```\n"
            "   (DEPLOYMENT.md §5), then reload this page; **or**\n"
            "2. start fresh with a first live fetch (today's prices only — "
            "the 5-year metrics need the backfill afterwards, Health tab):")
        if st.button("🔄 Fetch live data now", type="primary"):
            from ahmon import refresh as _refresh
            with st.spinner("Seeding classifications and fetching live "
                            "quotes…"):
                try:
                    _refresh.seed_portfolio(conn)
                    _refresh.refresh_live(conn)
                except Exception as e:   # noqa: BLE001 — shown, not hidden
                    st.error(f"First fetch failed: {e}")
                else:
                    st.session_state["data_version"] += 1
                    st.rerun()
    else:
        st.caption("Your account is read-only — please ask an admin to "
                   "load the data.")
    st.stop()

# ----------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("A–H Premium Monitor")
    sidebar_user_badge()
    n_sample = int((table["Quality"] == "sample").sum())
    if n_sample == len(table):
        st.warning("**SAMPLE DATA** — Phase 1 synthetic data. "
                   "No live feed is connected yet.", icon="⚠️")
    elif n_sample:
        st.warning(f"**MIXED DATA** — {n_sample} of {len(table)} companies "
                   "still show sample data.", icon="⚠️")
    else:
        st.success("Live data — akshare/Eastmoney prices, Yahoo FX & "
                   "verification.", icon="🟢")
    db_label = "PostgreSQL" if config.DATABASE_URL else config.DB_PATH.name
    st.caption(f"Timezone: Asia/Singapore · "
               f"{datetime.now(config.TZ):%Y-%m-%d %H:%M} · "
               f"DB: `{db_label}`")
    if n_sample == 0 and IS_ADMIN:
        # Admin-only by owner decision (the cron auto-refresh serves
        # everyone else): the admin role is granted via AHMON_USERS and
        # currently belongs solely to the owner — no email address is
        # ever written into this source, per the project's rules.
        if st.button("🔄 Refresh live data now", type="primary",
                     use_container_width=True):
            from ahmon import refresh as _refresh
            with st.spinner("Fetching live quotes (akshare + Yahoo)…"):
                try:
                    s = _refresh.refresh_live(conn)
                except Exception as e:      # noqa: BLE001 — shown, not hidden
                    st.error(f"Refresh failed — see Health tab. {e}")
                else:
                    st.session_state["data_version"] += 1
                    st.success(f"{s['upserted']} companies refreshed for "
                               f"{s['obs_date']} · FX {s['fx']:.4f} · "
                               f"{s['calc_vs_source_flags']} calc-vs-source "
                               "flags")
                    st.rerun()
    if IS_ADMIN:
        st.divider()
        st.subheader("Manual CSV import")
        up = st.file_uploader("Emergency fallback (see template in "
                              "config/)", type="csv")
        if up is not None and st.button("Import CSV"):
            res = csv_import.import_csv(conn, up)
            st.session_state["data_version"] += 1
            st.success(f"Imported {res['imported']} rows; "
                       f"skipped {len(res['skipped'])} unknown tickers.")
            st.rerun()

# -------------------------------------------------------------------- tabs
tabs = st.tabs(["Stock Monitor", "Buy-level watch", "Market Overview",
                "Attribution", "Sectors", "Charts", "Commentary",
                "Alerts", "Health"])

# ------------------------------------------------------- 3 Market Overview
with tabs[2]:
    st.subheader("Hang Seng Stock Connect China AH Premium Index (HSAHP)")
    if hsahp.empty:
        st.error("No HSAHP data stored.")
    else:
        ch = calc.series_changes(hsahp)
        r3, r5 = (calc.rolling_stats(hsahp, w)
                  for w in (config.WINDOW_3Y, config.WINDOW_5Y))
        v = metrics.hsahp_valuation(hsahp)
        wl = v["window_label"] or "5-year"
        status = metrics.valuation_status(v["percentile"])
        NO_HIST = "Insufficient five-year history"
        PCTILE_HELP = (
            "A lower HSAHP percentile means the A-share premium over H "
            "shares is smaller than during most of the historical period. "
            "This can suggest that H shares are less deeply discounted "
            "relative to A shares. It does not by itself mean that H "
            "shares are cheap in absolute valuation terms.")
        STATUS_HELP = (
            "Historical relative valuation only — where today's premium "
            "sits within the index's own history (percentile bands: "
            "≤10 very attractive · ≤25 attractive · ≤50 moderately "
            "attractive · ≤75 moderately expensive · ≤90 expensive · "
            ">90 very expensive). It is not a buy or sell "
            "recommendation. " + PCTILE_HELP)

        def change_help(period_desc: str) -> str:
            return (f"Index level now minus the level {period_desc}, in "
                    "index points. Positive = the average A-share premium "
                    "widened (A shares got more expensive relative to "
                    "their H twins, i.e. the H discount grew); negative = "
                    "the gap narrowed.")

        # ------- executive summary: six primary figures
        cols = st.columns(6)
        cols[0].metric("HSAHP Index", f"{v['current']:.2f}",
                       help="The Hang Seng Stock Connect China AH Premium "
                            "Index: the size-weighted average price gap "
                            "between the A and H shares of the largest "
                            "dual-listed companies. 100 = A and H cost "
                            "the same on average; above 100 = A shares "
                            "cost more. Published once per day at the "
                            "close.")
        cols[1].metric("Implied A-share premium",
                       f"{v['current'] - 100:.2f}%",
                       help="HSAHP Index − 100. The average extra price "
                            "of the A share over the same company's "
                            "H share, across the index constituents. "
                            "The index is published in premium terms — "
                            "the next tile translates it to the "
                            "H-buyer's view.")
        _imp_disc = metrics.premium_to_discount(v["current"] - 100)
        cols[2].metric("Implied H discount",
                       f"{_imp_disc:.2f}%",
                       help="The same index fact from the H-buyer's "
                            "side: with the implied average premium in "
                            "the previous tile, the typical index "
                            "constituent's H share trades at this "
                            "discount to its A twin (discount = premium "
                            "÷ (100 + premium) × 100). Derived by "
                            "arithmetic from the published index value.")
        cols[3].metric(f"{wl} percentile" if v["sufficient"]
                       else "Percentile",
                       f"{v['percentile']:.2f}%" if v["sufficient"]
                       else NO_HIST, help=PCTILE_HELP)
        # tile shows the short form; the label + tooltip carry the full
        # "relative to history" qualification
        status_short = (status.replace(" relative to history", "")
                        if status else NO_HIST)
        cols[4].metric("Historical relative valuation", status_short,
                       help=STATUS_HELP)
        cols[5].metric(f"Distance from {wl} median" if v["sufficient"]
                       else "Distance from median",
                       (metrics.format_change_pts(v["diff_pts"])
                        if v["sufficient"] else NO_HIST),
                       help="Current index level minus the median of the "
                            "trailing window, in index points. The same "
                            "gap as a percentage of the median is in the "
                            "context line below.")

        # ------- recent direction, shortest to longest window
        cc = st.columns(5)
        for col, (label, key, desc) in zip(cc, [
            ("Change over 1 week", "1w", "5 trading days ago"),
            ("Change over 1 month", "1m", "21 trading days (≈1 month) ago"),
            ("Change over 3 months", "3m",
             "63 trading days (≈3 months) ago"),
            ("Year-to-date", "ytd",
             "at the final trading day of last year"),
            ("Change over 1 year", "1y",
             "252 trading days (≈1 year) ago"),
        ]):
            col.metric(label, metrics.format_change_pts(ch[key]),
                       help=change_help(desc))

        # ------- historical context (secondary, quiet)
        ctx = []
        if v["sufficient"]:
            ctx.append(f"{wl} median **{v['median']:.2f}** · difference "
                       f"**{v['diff_pts']:+.2f} points** "
                       f"({v['diff_pct']:+.2f}%)")
        if r3["high"] is not None:
            ctx.append(f"3-year high {r3['high']:.2f} / low {r3['low']:.2f}")
        if r5["high"] is not None:
            ctx.append(f"5-year high {r5['high']:.2f} / low {r5['low']:.2f}")
        if ctx:
            st.caption(" · ".join(ctx))

        # ------- rules-based interpretation (no inference, points only)
        with st.container(border=True):
            st.markdown("**Current position** — "
                        + metrics.hsahp_interpretation(v, ch))

        rng = st.radio("Range", list(RANGE_KEYS), index=2, horizontal=True,
                       key="hsahp_rng")
        s = calc.resample_for_range(hsahp, RANGE_KEYS[rng])
        fig = line_chart(s, "HSAHP Index")
        if v["sufficient"]:
            fig.add_hline(y=v["median"],
                          line=dict(color=INK["secondary"], width=2,
                                    dash="dash"),
                          annotation_text=f"{wl} median {v['median']:.2f}",
                          annotation_position="top left",
                          annotation_font_color=INK["secondary"])
            if r5["high"] is not None:
                for y_val, lbl, pos in ((r5["high"], "5-year high",
                                         "top right"),
                                        (r5["low"], "5-year low",
                                         "bottom right")):
                    fig.add_hline(y=y_val,
                                  line=dict(color=INK["axis"], width=1,
                                            dash="dot"),
                                  annotation_text=f"{lbl} {y_val:.2f}",
                                  annotation_position=pos,
                                  annotation_font_color=INK["muted"])
        st.plotly_chart(fig, use_container_width=True)
        health = db.source_health_df(conn)
        hs_row = health[health["source"].str.contains("HSAHP")]
        status = hs_row.iloc[0]["status"] if not hs_row.empty else "unknown"
        st.caption(f"Latest observation: {hsahp.index[-1]:%Y-%m-%d} · "
                   f"source status: **{status}** · daily EOD series "
                   f"(index publishes once per day — no intraday values). "
                   "Reference lines use daily closes; the 1y/3y/5y/Max "
                   "views plot week- or month-end closes, so daily "
                   "extremes can sit beyond the plotted line.")

# ------------------------------------------------------- 2 Stock Monitor
DISPLAY_COLS = [
    "Company", "Name (ZH)", "H Ticker", "A Ticker",
    "Sector", "H Price (HKD)", "H % change today",
    "A Price (CNY)", "A % change today", "FX (HKD per 1 CNY)",
    "H discount to A (%)", "H upside to 5y median (%)",
    "H div yield (%)", "P/E (H)", "A div yield (%)", "P/E (A)",
    "Mkt cap H (HKD bn)", "Mkt cap A (CNY bn)", "P/B (H)", "P/B (A)",
    "3y median (pp)", "5y median (pp)", "Dist from 5y median (pp)",
    "5y percentile", "Dist from 3y median (pp)", "52w percentile",
    # premium level + its changes live together at the right end
    "Premium calc (%)",
    "Δ1d (pp)", "Δ1w (pp)", "Δ1m (pp)", "Δ3m (pp)", "ΔYTD (pp)", "Δ1y (pp)",
    "Updated", "Quality"]

# The owner's go-to columns get a standing highlight in every table.
HIGHLIGHT_COLS = ["H discount to A (%)", "H div yield (%)", "P/E (H)"]
HIGHLIGHT_CSS = "background-color: #eda10026"      # soft amber, ~15%


TEXT_COLS = {"Company", "Name (ZH)", "Classification", "Sector", "H Ticker",
             "A Ticker", "Updated", "Quality"}

PREMIUM_HELP = ("How much more the A share costs than the H share, "
                "computed by this dashboard from the three columns to the "
                "left:  (A price × HKD-per-CNY ÷ H price − 1) × 100.  "
                "Example: (¥10.00 × 1.16 ÷ HK$5.80 − 1) × 100 = +100%.")
FX_HELP = ("Exchange rate, direction matters: HK dollars per 1 yuan "
           "(≈1.16, i.e. ¥1 ≈ HK$1.16). The A price is multiplied by this "
           "to express it in HK dollars before comparing with the H price.")

# Hover explanation for every column in the Stock Monitor tables.
DAY_CHANGE_HELP = ("Today's move of the {leg} share: current price vs the "
                   "previous close, in %. Outside trading hours this is "
                   "the last session's full-day change.")
COLUMN_HELP = {
    "Company": "The company's English name.",
    "Name (ZH)": "The company's Chinese name.",
    "H Ticker": "Hong Kong stock code of the H share.",
    "A Ticker": "Mainland stock code of the A share "
                "(.SS = Shanghai, .SZ = Shenzhen).",
    "Sector": "One of eight sector buckets from config/sector_map.csv "
              "(utilities & transport sit under Industrials; property "
              "under Financials; autos & appliances under Consumer).",
    "H Price (HKD)": "Price of the Hong Kong (H) listing, in HK dollars.",
    "A Price (CNY)": "Price of the mainland (A) listing, in yuan (CNY).",
    "H % change today": DAY_CHANGE_HELP.format(leg="H"),
    "A % change today": DAY_CHANGE_HELP.format(leg="A"),
    "FX (HKD per 1 CNY)": FX_HELP,
    "Premium calc (%)": PREMIUM_HELP,
    "H discount to A (%)":
        "The same price gap as the premium, viewed from the H side: how "
        "much less the H share costs than its A twin.  "
        "(1 − H price ÷ (A price × HKD-per-CNY)) × 100.  "
        "Example: a +100% premium means A costs 2× H, so H is 50% off. "
        "Related by: discount = premium ÷ (100 + premium) × 100. "
        "Note the premium is the industry's standard convention (HSAHP, "
        "AASTOCKS, Eastmoney all quote premiums); the discount is the "
        "shopper's view of the identical fact.",
    "H upside to 5y median (%)":
        "What the H share would gain if today's premium merely went back "
        "to this company's own 5-year median premium, holding the A price "
        "and FX still: (1 + premium/100) ÷ (1 + median/100) − 1. The "
        "conservative version of the convergence bet (full convergence "
        "to the A price would return the premium itself).",
    "Δ1d (pp)": "Change in the premium vs the previous trading day, in "
                "percentage points. Positive = the gap widened (the H "
                "share got relatively cheaper vs its A twin); negative "
                "= the gap narrowed.",
    "Δ1w (pp)": "Change in the premium vs 5 trading days ago, in "
                "percentage points. Positive = the gap widened (H "
                "relatively cheaper); negative = it narrowed.",
    "Δ1m (pp)": "Change in the premium vs 21 trading days (≈1 month) "
                "ago, in percentage points. Positive = the gap widened "
                "(H relatively cheaper); negative = it narrowed.",
    "Δ3m (pp)": "Change in the premium vs 63 trading days (≈3 months) "
                "ago, in percentage points. Positive = the gap widened "
                "(H relatively cheaper); negative = it narrowed.",
    "ΔYTD (pp)": "Change in the premium since the final trading day of "
                 "last year, in percentage points. Positive = the gap "
                 "widened (H relatively cheaper); negative = it "
                 "narrowed.",
    "Δ1y (pp)": "Change in the premium vs 252 trading days (≈1 year) "
                "ago, in percentage points. Positive = the gap widened "
                "(H relatively cheaper); negative = it narrowed.",
    "Mkt cap H (HKD bn)": "Total market value priced off the H share, "
                          "in billions of HK dollars.",
    "Mkt cap A (CNY bn)": "Total market value priced off the A share, "
                          "in billions of yuan.",
    "H div yield (%)": "Dividend yield of the H share (last 12 months' "
                       "dividends ÷ H price). Blank = no dividend or "
                       "not reported.",
    "A div yield (%)": "Dividend yield of the A share. Usually lower "
                       "than the H yield by exactly the premium factor — "
                       "same dividend, higher share price.",
    "P/E (H)": "H price ÷ last-12-months earnings per share. Blank = "
               "loss-making or not reported.",
    "P/E (A)": "A price ÷ last-12-months earnings per share.",
    "P/B (H)": "H price ÷ book value per share.",
    "P/B (A)": "A price ÷ book value per share.",
    "3y median (pp)": "This company's typical premium over the last 3 "
                      "years (median of ~756 daily values).",
    "5y median (pp)": "This company's typical premium over the last 5 "
                      "years (median of ~1,260 daily values).",
    "Dist from 3y median (pp)": "Today's premium minus the 3-year "
                                "median: positive = wider than usual.",
    "Dist from 5y median (pp)": "Today's premium minus the 5-year "
                                "median: positive = wider than usual "
                                "(more H-side reversion upside).",
    "5y percentile": "Share of the last 5 years' days with a premium at "
                     "or below today's. 90 = wider than on 90% of days "
                     "(rare, H unusually cheap vs A); 10 = narrower than "
                     "usual.",
    "52w percentile": "Same idea over the last 52 weeks (252 trading "
                      "days).",
    "Updated": "When this row's data was last refreshed "
               "(Asia/Singapore time).",
    "Quality": "Data label: live = fresh feed · eod = historical daily "
               "close · stale = older than the freshness threshold for "
               "its tier · sample = synthetic demo data (never mixed "
               "with live).",
}

NUM_CONFIG = {}
for c in DISPLAY_COLS:
    if c in TEXT_COLS:
        NUM_CONFIG[c] = st.column_config.TextColumn(
            help=COLUMN_HELP.get(c),
            # widest sector name ("Mining & Materials") must not truncate
            width=170 if c == "Sector" else None)
    else:
        NUM_CONFIG[c] = st.column_config.NumberColumn(
            format="%.3f" if c == "FX (HKD per 1 CNY)" else "%.2f",
            help=COLUMN_HELP.get(c))


# Sector cells wear a light tint of the sector's series color (same hue
# as its line in the Sectors chart); text stays in normal ink so the
# name, not the color, carries the identity.
SECTOR_TINT = {s: c + "2e" for s, c in SECTOR_COLOR.items()}   # ~18% alpha


def sector_css(v):
    tint = SECTOR_TINT.get(v)
    return f"background-color: {tint}" if tint else None


def show_table(t: pd.DataFrame, key: str = "tbl"):
    if t.empty:
        st.info("No companies in this group.")
        return
    st.dataframe(t[DISPLAY_COLS].style.map(sector_css, subset=["Sector"])
                 .map(lambda _: HIGHLIGHT_CSS, subset=HIGHLIGHT_COLS),
                 column_config=NUM_CONFIG,
                 use_container_width=True, row_height=40,
                 height=min(620, 70 + 40 * len(t)), hide_index=True)
    c1, c2 = st.columns([1, 3])
    c1.download_button(
        "⬇️ Download Excel (.xlsx)",
        data=metrics.table_to_xlsx_bytes(t[DISPLAY_COLS], sheet=key),
        file_name=f"ahmon_{key}_{datetime.now(config.TZ):%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet",
        key=f"xlsx_{key}")
    c2.caption("Click any column header to sort (click again to reverse).")


# ------------------------------------------------------ 2 Buy-level watch
with tabs[1]:
    st.markdown("#### 💡 Buy-level watch — yield-based top-up monitor")
    st.caption(
        "Covers the 20 Focus holdings. Target yields: **5.0%** for "
        "stable payers, **6.0%** for cyclical payers (wider because "
        "cyclical earnings and dividends move with commodity prices "
        "and the economic cycle). **Top-up price = DPS ÷ target "
        "yield.** Each name's DPS defaults to its **trailing 12-month "
        "declared dividends**; a **manually normalised DPS** can "
        "override it (recommended for cyclicals near a cycle peak) — "
        "the *DPS basis* column shows which is in use. Status: "
        "**WAIT** = price more than 5% above the top-up price · "
        "**NEAR RANGE** = within 5% above it · **IN RANGE — REVIEW** = "
        "at or below it — the yield threshold is met, but dividend "
        "sustainability still needs your review before acting · "
        "**DATA REVIEW** = dividend data missing. China Life, BYD and "
        "SMIC are shown but are not yield-based.")
    from ahmon import divwatch as _divwatch
    dw = _divwatch.build_topup_table(conn)

    # ------- adjustable target-yield calculator (pure what-if: writes
    # nothing; the watch table's saved 5%/6% thresholds are untouched)
    _yb = dw[dw["Target yield (%)"].notna()] if not dw.empty else dw
    if not _yb.empty:
        with st.container(border=True):
            st.markdown("##### 🎯 Target-yield calculator — what-if")
            csel, cslide, cnum = st.columns([2, 2, 1])
            _pick = csel.selectbox(
                "Stock", list(_yb["Company"] + "  (" + _yb["Ticker"] + ")"),
                key="calc_pick",
                help="Yield-based names only. BYD, China Life and SMIC "
                     "are not watched on dividend yield, so they are "
                     "not offered here.")
            _tick = _pick.rsplit("(", 1)[1].rstrip(")")
            _row = dw.set_index("Ticker").loc[_tick]
            _default = float(_row["Target yield (%)"])
            _ks, _kn = f"calc_s_{_tick}", f"calc_n_{_tick}"
            st.session_state.setdefault(_ks, _default)
            st.session_state.setdefault(_kn, _default)

            def _from_slider(ks=_ks, kn=_kn):
                st.session_state[kn] = st.session_state[ks]

            def _from_num(ks=_ks, kn=_kn):
                st.session_state[ks] = st.session_state[kn]

            cslide.slider("Target yield (%)", min_value=1.0,
                          max_value=12.0, step=0.1, key=_ks,
                          on_change=_from_slider)
            cnum.number_input("or type it", min_value=1.0, max_value=12.0,
                              step=0.1, format="%.1f", key=_kn,
                              on_change=_from_num)
            _target = float(st.session_state[_ks])

            _dps = _row["DPS (HKD)"]
            _price = _row["Price (HKD)"]
            if pd.isna(_dps):
                st.warning("**DPS required** — this name has no dividend "
                           "figure yet (fetch dividend history or set a "
                           "manual DPS in the admin section below).")
            elif pd.isna(_price):
                st.warning("No current price stored for this name yet.")
            else:
                _req = _divwatch.required_price(float(_dps), _target)
                _pos_text, _ = _divwatch.position_vs_required(
                    float(_price), _req)
                m = st.columns(6)
                m[0].metric("DPS used", f"HK${float(_dps):,.3f}",
                            help=f"Basis: {_row['DPS basis']}.")
                m[1].metric("Current price", f"HK${float(_price):,.2f}",
                            help=f"As of {_row['As of']}.")
                m[2].metric("Current yield",
                            f"{float(_dps) / float(_price) * 100:.2f}%",
                            help="DPS used ÷ current price — what the "
                                 "stock yields at today's price.")
                m[3].metric("Target yield", f"{_target:.1f}%")
                m[4].metric("Required price", f"HK${_req:,.2f}",
                            help="DPS ÷ target yield: the price at "
                                 "which this DPS pays exactly the "
                                 "target.")
                m[5].metric("Position vs required",
                            _pos_text.replace(" target price", ""),
                            help="'Below' = today's price is under the "
                                 "required price, so the yield at "
                                 "today's price already beats the "
                                 "target. 'Above' = the price must "
                                 "fall that far to reach the target "
                                 "yield.")
                if abs(_target - _default) > 1e-9:
                    st.caption(f"Temporary what-if only — this name's "
                               f"saved watch threshold stays at "
                               f"{_default:.1f}% ({_row['Classification']})"
                               " and nothing is written to the database.")

    if dw.empty:
        st.info("Watchlist empty — an admin can seed it in the admin "
                "section below.")
    else:
        _status_css = {
            _divwatch.ST_REVIEW: "background-color: #0083002e",
            _divwatch.ST_NEAR: "background-color: #eda1002e",
            _divwatch.ST_DATA: "background-color: #e3494820"}
        _basis_css = {
            _divwatch.BASIS_TRAILING_FLAG: "background-color: #eda1002e"}
        # Streamlit renders null numeric cells as literal 'None' whatever
        # the Styler's na_rep says — so numbers are pre-formatted to
        # strings ('—' for missing) and re-right-aligned via cell CSS.
        _num_cols = ["Price (HKD)", "DPS (HKD)", "Current yield (%)",
                     "Target yield (%)", "Top-up price (HKD)"]
        disp = dw.copy()
        for _c in _num_cols:
            disp[_c] = dw[_c].map(
                lambda v: "—" if pd.isna(v) else f"{v:,.2f}")
        st.dataframe(
            disp.style
            .map(lambda v: _status_css.get(v, ""), subset=["Status"])
            .map(lambda v: _basis_css.get(v, ""), subset=["DPS basis"])
            # the number you read first — same standing highlight as the
            # Stock Monitor focus columns
            .map(lambda _: HIGHLIGHT_CSS, subset=["Current yield (%)"])
            .map(lambda _: "text-align: right", subset=_num_cols),
            hide_index=True, use_container_width=True, row_height=40,
            height=min(620, 70 + 40 * len(dw)),
            column_config={
                "Status": st.column_config.TextColumn(help=(
                    "IN RANGE — REVIEW: the trailing-yield threshold "
                    "has been reached — but check dividend "
                    "sustainability before acting (is the DPS "
                    "normalised? is the payout one-off?). NEAR RANGE: "
                    "price within 5% above the top-up level. WAIT: "
                    "more than 5% above. DATA REVIEW: no reliable "
                    "DPS yet.")),
                "Position vs top-up price": st.column_config.TextColumn(
                    help="Where today's price sits relative to the "
                         "top-up price. 'Below' means the price is "
                         "under the top-up level — the yield already "
                         "clears the target."),
                "DPS basis": st.column_config.TextColumn(help=(
                    "Which DPS the row uses. 'Trailing 12m DPS' = sum "
                    "of the last 12 months' declared dividends. "
                    "'Trailing DPS — review special' = that sum is "
                    "more than 1.6× the previous 12 months, so it may "
                    "contain a special or unusual payment — consider "
                    "a manual normalised override. 'Manual normalised "
                    "DPS' = your approved figure.")),
            })
        n_review = int((dw["Status"] == "DATA REVIEW").sum())
        n_manual = int((dw["DPS basis"] == _divwatch.BASIS_MANUAL).sum())
        bits = []
        if n_review:
            bits.append(f"{n_review} names need a dividend-history "
                        "fetch or a manual DPS")
        if n_manual:
            bits.append(f"{n_manual} names use a manual normalised DPS")
        st.caption(("Trailing DPS sums the last 12 months of declared "
                    "dividends (Yahoo does not label special payouts — "
                    "override any figure that looks one-off). ")
                   + (" · ".join(bits) + "." if bits else ""))
    if IS_ADMIN:
        with st.expander("🛠️ Admin — dividend data & DPS overrides"):
            c1, c2 = st.columns(2)
            if c1.button("Fetch declared dividends (Yahoo, ~30 s)"):
                with st.spinner("Fetching dividend history…"):
                    r = _divwatch.refresh_dps(conn)
                st.success(f"{r['stored']} dividends stored · failures: "
                           f"{r['failed'] or 'none'}")
                st.rerun()
            if c2.button("Seed / update watchlist from CSV"):
                r = _divwatch.seed_watchlist(conn)
                st.success(f"{r['total']} names on the watchlist")
                st.rerun()
            st.markdown("**Manual normalised DPS (override)** — wins "
                        "over the trailing figure; use it where a "
                        "trailing payout looks peak-cycle or one-off. "
                        "Every save records who and when.")
            if not dw.empty:
                _needs = dw[(dw["Status"] == "DATA REVIEW")
                            | (dw["DPS basis"] == _divwatch.BASIS_MANUAL)]
                _show_all = st.checkbox(
                    "Choose from all yield-based names", value=_needs.empty,
                    help="Unticked: only names needing review or already "
                         "overridden.")
                _pool = dw[dw["Target yield (%)"].notna()] if _show_all \
                    else _needs
                if _pool.empty:
                    st.caption("Nothing needs review right now.")
                else:
                    c1, c2, c3 = st.columns([2, 1, 1])
                    pick_t = c1.selectbox(
                        "Company",
                        list(_pool["Company"] + "  (" + _pool["Ticker"]
                             + ")"), key="dps_pick")
                    _ticker = pick_t.rsplit("(", 1)[1].rstrip(")")
                    _row = dw.set_index("Ticker").loc[_ticker]
                    _cur = _row["DPS (HKD)"]
                    _new = c2.number_input(
                        "Annual DPS (HKD)", min_value=0.0, step=0.01,
                        value=float(_cur) if pd.notna(_cur) else 0.0,
                        format="%.4f", key="dps_val",
                        help="Current basis: " + str(_row["DPS basis"]))
                    if c3.button("Save override", type="primary"):
                        if _new > 0:
                            db.set_approved_dps(
                                conn, _ticker, _new,
                                source=f"dashboard:{user['email']}")
                            st.success(f"{_ticker}: manual normalised "
                                       f"DPS = {_new:.4f} HKD")
                            st.rerun()
                        else:
                            st.warning("DPS must be greater than zero.")

# --------------------------------------------------------- 1 Stock Monitor
with tabs[0]:
    fc1, fc2 = st.columns([3, 1])
    filter_text = fc1.text_input(
        "🔎 Filter — type letters to narrow the tables below",
        placeholder="e.g. bank · 601988 · 中国 · Financials",
        key="monitor_filter")
    filter_col = fc2.selectbox(
        "in column", ["All text columns"] + metrics.TEXT_FILTER_COLUMNS,
        key="monitor_filter_col")
    ftable = metrics.filter_table(table, filter_text, filter_col)
    if filter_text and len(ftable) < len(table):
        st.caption(f"Showing {len(ftable)} of {len(table)} companies "
                   f"matching “{filter_text}”. (The magnifier icon on any "
                   "table also searches within it.)")

    def by_sector(t: pd.DataFrame,
                  ticker_rank: dict | None = None) -> pd.DataFrame:
        # grouped view: sectors together (config.SECTORS order); within a
        # sector, rows follow ticker_rank if given (the owner's list
        # order), otherwise company name A→Z
        order = {s: i for i, s in enumerate(config.SECTORS)}

        def key(col):
            if col.name == "Sector":
                return col.map(order).fillna(99)
            if col.name == "H Ticker" and ticker_rank is not None:
                return col.map(ticker_rank).fillna(9999)
            return col

        cols = ["Sector",
                "H Ticker" if ticker_rank is not None else "Company"]
        return t.sort_values(cols, key=key)

    # Focus rows keep the order of config/focus_list.csv within their
    # sector, so the owner controls row order by editing the list.
    focus_rank = {t: i for i, t in enumerate(
        pd.read_csv(config.CONFIG_DIR / "focus_list.csv")["h_ticker"])}

    groups = {
        "Focus A-H Holdings":
            by_sector(ftable[ftable["Classification"] == config.FOCUS],
                      focus_rank),
        "Other A-H Stocks":
            by_sector(ftable[ftable["Classification"] != config.FOCUS]),
        "Full A-H Universe": ftable,
    }
    sub = st.tabs(list(groups))
    for stab, (name, t) in zip(sub, groups.items()):
        with stab:
            show_table(t.reset_index(drop=True),
                       key=name.replace(" ", "_").lower())
            if name == "Focus A-H Holdings" and not t.empty:
                n_focus = int(
                    (table["Classification"] == config.FOCUS).sum())
                st.markdown("##### Full A-H Universe (summary)")
                st.caption(f"All {len(table)} A–H companies "
                           f"= {n_focus} Focus A-H Holdings "
                           f"+ {len(table) - n_focus} Other A-H Stocks.")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric(
                    "Companies", len(table),
                    help="Every dual-listed company tracked — each has "
                         "both a Hong Kong H share and a mainland A "
                         "share. The count grows automatically when a "
                         "new A–H pair starts trading.")
                med_disc = table["H discount to A (%)"].median()
                med_prem = table["Premium calc (%)"].median()
                c2.metric(
                    "Median H discount to A",
                    "—" if pd.isna(med_disc) else f"{med_disc:.1f}%",
                    help="The middle company's H-share discount to its A "
                         "twin: half the universe trades at a bigger "
                         "discount, half at a smaller one. Equivalent to "
                         f"a median A-share premium of {med_prem:.1f}% "
                         "(the industry's usual convention).")
                med_d1 = table["Δ1d (pp)"].median()
                c3.metric(
                    "Median Δ1d",
                    "—" if pd.isna(med_d1) else f"{med_d1:+.2f} pp",
                    help="The middle company's one-day change in the "
                         "A-share premium, in percentage points. "
                         "Positive = price gaps widened since yesterday "
                         "(H shares got relatively cheaper); negative = "
                         "gaps narrowed. Changes are measured on the "
                         "premium because that is the industry "
                         "convention. Needs at least two days of stored "
                         "history.")
                c4.metric(
                    ">5pp movers today",
                    int((table["Δ1d (pp)"].abs() > 5).sum()),
                    help="How many companies' premium moved more than 5 "
                         "percentage points since yesterday, in either "
                         "direction — a quick gauge of how turbulent "
                         "the A–H gap is today.")

    st.divider()
    if IS_ADMIN:
        st.markdown("##### Focus list")
        st.caption("The Focus list is stored in the database and "
                   "audit-logged. This button reconciles it against "
                   "`config/focus_list.csv` (the standard 20 names): "
                   "listed companies become Focus, current Focus members "
                   "not on the list move to Other A-H Stock.")
        if st.button("Apply standard Focus list"):
            from ahmon.focus_list import apply_focus_list
            res = apply_focus_list(conn,
                                   source=f"focus_list:{user['email']}")
            st.session_state["data_version"] += 1
            st.success(f"Focus Holdings now {res['focus_size']} · "
                       f"promoted {res['promoted'] or 'none'} · "
                       f"demoted {res['demoted'] or 'none'}")
            if res["missing_from_db"]:
                st.warning(f"On the list but not in the database: "
                           f"{res['missing_from_db']}")
            st.rerun()
        st.markdown("##### Sector map")
        st.caption("Sectors are stored in the database and reconciled "
                   "against `config/sector_map.csv`, which tags every "
                   "company in the universe into one of the eight "
                   "sectors. The live refresh re-applies the map "
                   "automatically; this button applies it right now. "
                   "Changes are recorded in the constituent log.")
        if st.button("Apply sector map"):
            from ahmon.sector_map import apply_sector_map
            res = apply_sector_map(conn,
                                   source=f"sector_map:{user['email']}")
            st.session_state["data_version"] += 1
            st.success(f"{len(res['updated'])} companies re-tagged · "
                       f"{res['mapped_size']} in the map")
            if res["unmapped"]:
                st.warning(f"In the database but not in the map "
                           f"(left as-is): {res['unmapped']}")
            st.rerun()
        st.markdown("##### Reclassify a company")
        c1, c2, c3 = st.columns([2, 2, 1])
        pick = c1.selectbox("Company", table["Company"].sort_values())
        row = table[table["Company"] == pick].iloc[0]
        current = row["Classification"]
        target = c2.selectbox("Move to", [c for c in config.CLASSIFICATIONS
                                          if c != current],
                              help=f"Currently: {current}")
        if c3.button("Apply", type="primary"):
            db.set_classification(conn, int(row["company_id"]), target,
                                  source=f"dashboard:{user['email']}")
            st.session_state["data_version"] += 1
            st.success(f"{pick}: {current} → {target} (audit-logged)")
            st.rerun()
        b1, b2 = st.columns(2)
        if current != config.FOCUS and \
                b1.button(f"⭐ Promote {pick} to Focus"):
            db.set_classification(conn, int(row["company_id"]),
                                  config.FOCUS,
                                  source=f"dashboard:{user['email']}")
            st.session_state["data_version"] += 1
            st.rerun()
        if current == config.FOCUS and \
                b2.button(f"Remove {pick} from Focus "
                          f"(→ Other Portfolio Holding)"):
            db.set_classification(conn, int(row["company_id"]),
                                  config.PORTFOLIO,
                                  source=f"dashboard:{user['email']}")
            st.session_state["data_version"] += 1
            st.rerun()
    else:
        st.caption("Classification changes require an admin account.")
    with st.expander("Classification audit log"):
        st.dataframe(db.audit_df(conn), use_container_width=True)

# --------------------------------------------------------- 3 Attribution
with tabs[3]:
    st.subheader("Premium attribution — what caused each premium move")
    st.markdown(
        "The premium can only change for three reasons: the **A-share price** "
        "moved, the **H-share price** moved, or the **CNY/HKD rate** moved. "
        "This table splits each company's 1-day premium change into those "
        "three parts (using log returns, scaled so the three contributions "
        "add up exactly to the actual percentage-point move). The **Driver** "
        "column names the dominant part — e.g. if the premium narrowed "
        "mainly because the H share rallied, the driver is *H-share "
        "outperformance*; when no single factor dominates it says "
        "*Combination of factors*. Everything is arithmetic from actual "
        "prices and FX — nothing is inferred.\n\n"
        "*Why this tab uses premium points while the rest of the "
        "dashboard speaks H-discount:* the three contributions add up "
        "exactly to the total move only in premium terms — the discount "
        "is a non-linear transform of the same fact, so discount-space "
        "contributions would not sum. This is deliberately the one "
        "premium-denominated page; the direction reading is unchanged "
        "(a negative move = gap narrowing = H catching up).")
    st.dataframe(att.style.format(precision=2, na_rep="—"),
                 use_container_width=True, height=480)
    if not att.empty:
        counts = att["Driver"].value_counts()
        fig = go.Figure(go.Bar(
            x=counts.values, y=counts.index, orientation="h",
            marker=dict(color=C["blue"], cornerradius=4)))
        fig.update_layout(title="Companies by main driver (today)")
        st.plotly_chart(styled(fig, 300), use_container_width=True)

# ------------------------------------------------------------- 4 Sectors
with tabs[4]:
    st.markdown(
        "Each company is assigned to one of eight sectors in the "
        "sector map (`config/sector_map.csv`, applied automatically by "
        "the live refresh and re-appliable from the Stock Monitor admin "
        "panel). The numbers below come from the same per-company "
        "prices as the Stock Monitor — Eastmoney A/H quotes with Yahoo "
        "FX — expressed in the **H-buyer's view**: *Median H discount* "
        "= the middle company's H-share discount to its A twin within "
        "the sector (the identical fact as the A-share premium, since "
        "discount = premium ÷ (100 + premium) × 100)" +
        (" — currently **synthetic sample data**" if n_sample else "") +
        ". Δ columns are the change of that median discount, in "
        "percentage points.")
    st.dataframe(metrics.sector_stats(conn, table)
                 .style.format(precision=2, na_rep="—")
                 .map(sector_css, subset=["Sector"]),
                 use_container_width=True,
                 column_config={
                     "Sector": st.column_config.TextColumn(
                         help="One of the eight sector buckets from "
                              "config/sector_map.csv."),
                     "Companies": st.column_config.NumberColumn(
                         help="How many A–H companies the sector "
                              "currently holds."),
                     "Median H discount (%)": st.column_config.NumberColumn(
                         help="TODAY's snapshot (latest refresh): the "
                              "middle company's H-share discount to its "
                              "A twin — half the sector is deeper, half "
                              "shallower. Not an average over time."),
                     "Avg H discount (%)": st.column_config.NumberColumn(
                         help="TODAY's snapshot: the simple average of "
                              "the sector's per-company H discounts."),
                     "Δ1m median (pp)": st.column_config.NumberColumn(
                         help="Change of the median discount vs one "
                              "month ago, in percentage points. "
                              "Positive = discounts widened (H got "
                              "relatively cheaper); negative = "
                              "narrowed (convergence)."),
                     "Δ1y median (pp)": st.column_config.NumberColumn(
                         help="Change of the median discount vs one "
                              "year ago, in percentage points — the "
                              "change over the year, not the year's "
                              "median."),
                 })
    hist = metrics.sector_history(conn)
    fig = go.Figure()
    for i, sector in enumerate(config.SECTORS):
        g = hist[hist["sector"] == sector]
        if g.empty:
            continue
        s = pd.Series(g["median_discount"].values,
                      index=pd.DatetimeIndex(g["date"])).resample("ME").last()
        fig.add_scatter(x=s.index, y=s.values, name=sector, mode="lines",
                        line=dict(color=SECTOR_COLOR[sector], width=2))
    fig.update_layout(title="Sector median H-share discount to A (monthly)")
    st.plotly_chart(styled(fig, 420), use_container_width=True)
    st.caption("Each line = the median H-share discount of that sector's "
               "companies at each month-end, computed from the stored "
               "daily price history. A falling line means the discount "
               "is shrinking — H shares catching up with their A twins "
               "(the convergence an H holder benefits from); a rising "
               "line means H shares getting relatively cheaper.")

# -------------------------------------------------------------- 6 Charts
with tabs[5]:
    pick = st.selectbox("Company", table["Company"].sort_values(),
                        key="chart_pick")
    cid = int(table[table["Company"] == pick].iloc[0]["company_id"])
    g = db.daily_df(conn, cid)
    disc = pd.Series(
        metrics.premium_to_discount(g["premium_calc"]).values,
        index=g["date"])
    rng = st.radio("Range", list(RANGE_KEYS), index=1, horizontal=True,
                   key="stock_rng",
                   help="Time window for the discount-history chart below. "
                        "3m plots daily observations, 1y weekly, 3y/5y/max "
                        "monthly. Daily history is always stored regardless "
                        "of what is displayed.")
    s = calc.resample_for_range(disc, RANGE_KEYS[rng])
    fig = line_chart(s, f"{pick} H discount to A (%)")
    fig.update_layout(title=f"{pick} — H-share discount to A, history (%)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"This chart is specific to {pick}: how much less its H "
               "share cost than its A twin over the selected range. A "
               "falling line = the discount shrinking (convergence an H "
               "holder benefits from); a rising line = the H share "
               "getting relatively cheaper.")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Current H discount vs 5-year range")
        r5 = calc.rolling_stats(disc, config.WINDOW_5Y)
        if r5["median"] is not None:
            fig = go.Figure()
            fig.add_shape(type="line", x0=r5["low"], x1=r5["high"], y0=0, y1=0,
                          line=dict(color=INK["axis"], width=6))
            fig.add_scatter(x=[r5["median"]], y=[0], mode="markers",
                            name="5y median",
                            marker=dict(color=INK["muted"], size=14,
                                        symbol="line-ns-open"))
            fig.add_scatter(x=[disc.iloc[-1]], y=[0], mode="markers",
                            name="Current",
                            marker=dict(color=C["blue"], size=16))
            fig.update_yaxes(visible=False)
            fig.update_xaxes(title="H discount to A (%)")
            st.plotly_chart(styled(fig, 200), use_container_width=True)
            st.caption("Blue dot right of the tick mark = today's "
                       "discount is wider than this company's own "
                       "5-year norm.")
    with c2:
        st.markdown("##### A vs H rebased to 100 (1y)")
        cutoff = g["date"].max() - pd.DateOffset(years=1)
        gg = g[g["date"] >= cutoff]
        a = gg["a_close"] / gg["a_close"].iloc[0] * 100
        h = gg["h_close"] / gg["h_close"].iloc[0] * 100
        fig = go.Figure()
        fig.add_scatter(x=gg["date"], y=a, name="A share (CNY)", mode="lines",
                        line=dict(color=C["blue"], width=2))
        fig.add_scatter(x=gg["date"], y=h, name="H share (HKD)", mode="lines",
                        line=dict(color=C["green"], width=2))
        st.plotly_chart(styled(fig, 240), use_container_width=True)

    st.markdown("##### H discount distribution across the A–H universe "
                "(today)")
    fig = go.Figure(go.Histogram(
        x=table["H discount to A (%)"], nbinsx=30,
        marker=dict(color=C["blue"],
                    line=dict(color=INK["surface"], width=2))))
    fig.update_xaxes(title="H-share discount to A (%)")
    fig.update_yaxes(title="Companies")
    st.plotly_chart(styled(fig, 300), use_container_width=True)
    st.caption("How today's H discounts spread across all companies: "
               "bars far right = names selling at the steepest H "
               "discounts; near zero (or negative) = the two listings "
               "price almost alike, or the H share is the dearer one.")

# -------------------------------------------------------------- 8 Alerts
with tabs[7]:
    if st.button("Evaluate alert rules now", type="primary"):
        fired = alerts.evaluate(conn, table)
        st.session_state["fired"] = fired
    fired = st.session_state.get("fired")
    if fired is not None:
        if fired.empty:
            st.success("No alerts fired.")
        else:
            for sev, icon in (("critical", "🟥"), ("serious", "🟧"),
                              ("warning", "🟨"), ("info", "🟦")):
                for _, a in fired[fired["severity"] == sev].iterrows():
                    st.markdown(f"{icon} **{a['company'] or '—'}** · "
                                f"`{a['rule']}` — {a['message']}")
    with st.expander("Alert log (persisted)"):
        st.dataframe(db.alerts_df(conn), use_container_width=True)

# ---------------------------------------------------------- 7 Commentary
@st.cache_data(ttl=300, show_spinner=False)
def att_over(version: int, days: int) -> pd.DataFrame:
    """Window attribution for the commentary cause sentences, cached so
    the history pull doesn't rerun on every widget interaction."""
    t, _ = load(version)
    return metrics.attribution_over(get_conn(), t, days)


with tabs[6]:
    st.subheader("Daily commentary")
    st.markdown(commentary.daily_commentary(table, att))
    st.divider()
    st.subheader("Closing summary")
    st.markdown(commentary.closing_summary(
        table, att, metrics.sector_stats(conn, table)))
    st.divider()
    st.subheader("Weekly commentary")
    st.markdown(commentary.period_commentary(
        table, "1w", att_over(st.session_state["data_version"], 7)))
    st.divider()
    st.subheader("Monthly commentary")
    st.markdown(commentary.period_commentary(
        table, "1m", att_over(st.session_state["data_version"], 30)))
    st.divider()
    st.subheader("Yearly commentary")
    st.markdown(commentary.period_commentary(
        table, "1y", att_over(st.session_state["data_version"], 365)))
    st.caption("Every 'cause' sentence is counted from the Attribution "
               "tab's arithmetic decomposition over the matching window "
               "— daily causes from the 1-day decomposition, weekly and "
               "monthly from the same math applied across those "
               "windows. Nothing is inferred or written by an AI.")
    st.caption("The scheduler (`python -m ahmon.scheduler`) stores these "
               "automatically: daily + closing after the HK close, weekly "
               "on Friday, monthly on the month's last trading day.")
    with st.expander("Stored commentary history (written by the scheduler)"):
        hist = db.commentary_df(conn)
        if hist.empty:
            st.info("Nothing stored yet — start the scheduler to record "
                    "commentary after each HK close.")
        else:
            for _, r in hist.iterrows():
                st.markdown(f"**{r['ts']} · {r['kind']}**\n\n{r['body']}")
                st.divider()

# --------------------------------------------------------------- 9 Health
with tabs[8]:
    st.subheader("Data-source health")
    st.markdown(
        "This panel tells you **whether each data feed is actually working** "
        "so a broken feed can never masquerade as live prices. Each source "
        "shows its status (🟢 live · 🟡 sample · 🟠 degraded/stale · "
        "🔴 failed), when it last succeeded and its last error. Sample data "
        "is never relabelled as live: synthetic rows stay 🟡 in their own "
        "database file, and if a live feed breaks mid-day you'll see it "
        "here (plus a stale-data alert) instead of silently looking at old "
        "numbers.")
    health = db.source_health_df(conn)
    icon = {"ok": "🟢", "sample": "🟡", "degraded": "🟠", "stale": "🟠",
            "failed": "🔴"}
    for _, h in health.iterrows():
        st.markdown(f"{icon.get(h['status'], '⚪')} **{h['source']}** — "
                    f"{h['status']} · {h['message'] or ''}  \n"
                    f"<span style='color:{INK['muted']}'>last success: "
                    f"{h['last_success'] or '—'} · last error: "
                    f"{h['last_error'] or '—'}</span>",
                    unsafe_allow_html=True)
    if IS_ADMIN:
        st.divider()
        st.markdown("##### Admin: history backfill top-up")
        st.caption("Fetches ~6y of daily history for up to 10 companies "
                   "that don't have it yet (resumable — click again to "
                   "continue). The first full backfill is faster as a "
                   "one-off job: `python -m ahmon.backfill` "
                   "(see DEPLOYMENT.md).")
        if st.button("Backfill 10 companies now"):
            from ahmon.backfill import backfill as _backfill
            with st.spinner("Fetching history (≈30 s per company)…"):
                res = _backfill(conn, limit=10, verify_n=0,
                                log=lambda *_: None)
            st.session_state["data_version"] += 1
            st.success(f"{res['done']} backfilled, "
                       f"{res['skipped_companies']} already done, "
                       f"{res['failed']} failed.")
    st.divider()
    st.markdown("##### Freshness by classification tier")
    fresh = table.groupby("Classification").agg(
        companies=("Company", "count"),
        stale=("Quality", lambda q: int((q == "stale").sum())),
        sample=("Quality", lambda q: int((q == "sample").sum())))
    st.dataframe(fresh, use_container_width=True)
    st.caption("Refresh cadence (Phase 4 scheduler): Focus 5 min during "
               "overlapping HK/mainland hours, 10 min 15:00–16:00 HK; "
               "Portfolio/Watchlist 15 min; Other 30 min; EOD snapshot "
               "after HK close. Sample data is never relabelled as live.")
