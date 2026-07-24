"""A–H Premium Monitor — Streamlit dashboard (Phase 1: sample data)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ahmon import (alerts, auth, calc, commentary, config, db, metrics,
                   sample_data, signals)
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
        font=dict(family="system-ui, sans-serif", color=INK["secondary"], size=12),
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


@st.cache_data(show_spinner="Computing monitor table…")
def load(version: int):
    """Heavy per-company metrics, cached per data version: widget
    interactions rerun the script but reuse this result; refreshes and
    imports bump st.session_state['data_version'] to force a recompute."""
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
tabs = st.tabs(["Market Overview", "Stock Monitor", "Attribution",
                "Rankings", "Sectors", "Charts", "Alerts", "Commentary",
                "Health"])

# ------------------------------------------------------- 1 Market Overview
with tabs[0]:
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

        # ------- executive summary: five primary figures
        cols = st.columns(5)
        cols[0].metric("HSAHP Index", f"{v['current']:.2f}")
        cols[1].metric("Implied A-share premium",
                       f"{v['current'] - 100:.2f}%",
                       help="HSAHP Index − 100. The average extra price "
                            "of the A share over the same company's "
                            "H share, across the index constituents.")
        cols[2].metric(f"{wl} percentile" if v["sufficient"]
                       else "Percentile",
                       f"{v['percentile']:.2f}%" if v["sufficient"]
                       else NO_HIST, help=PCTILE_HELP)
        # tile shows the short form; the label + tooltip carry the full
        # "relative to history" qualification
        status_short = (status.replace(" relative to history", "")
                        if status else NO_HIST)
        cols[3].metric("Historical relative valuation", status_short,
                       help=STATUS_HELP)
        cols[4].metric(f"Distance from {wl} median" if v["sufficient"]
                       else "Distance from median",
                       (metrics.format_change_pts(v["diff_pts"])
                        if v["sufficient"] else NO_HIST),
                       help="Current index level minus the median of the "
                            "trailing window, in index points. The same "
                            "gap as a percentage of the median is in the "
                            "context line below.")

        # ------- recent direction: three primary changes + the rest folded
        cc = st.columns([1, 1, 1, 2])
        cc[0].metric("Change over 1 month",
                     metrics.format_change_pts(ch["1m"]))
        cc[1].metric("Year-to-date", metrics.format_change_pts(ch["ytd"]))
        cc[2].metric("Change over 1 year",
                     metrics.format_change_pts(ch["1y"]))
        with cc[3].expander("More periods"):
            m1, m2 = st.columns(2)
            m1.metric("Change over 1 week",
                      metrics.format_change_pts(ch["1w"]))
            m2.metric("Change over 3 months",
                      metrics.format_change_pts(ch["3m"]))

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
    "Premium calc (%)", "H discount to A (%)",
    "H upside to 5y median (%)",
    "Δ1d (pp)", "Δ1w (pp)", "Δ1m (pp)", "Δ3m (pp)", "ΔYTD (pp)", "Δ1y (pp)",
    "Mkt cap H (HKD bn)", "Mkt cap A (CNY bn)",
    "H div yield (%)", "A div yield (%)", "P/E (H)", "P/E (A)",
    "P/B (H)", "P/B (A)", "3y median (pp)",
    "5y median (pp)", "Dist from 5y median (pp)", "5y percentile",
    "Dist from 3y median (pp)", "52w percentile",
    "Updated", "Quality"]


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
    "Sector": "Dashboard sector grouping (editable by an admin).",
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
                "percentage points.",
    "Δ1w (pp)": "Change in the premium vs 5 trading days ago, in "
                "percentage points.",
    "Δ1m (pp)": "Change in the premium vs 21 trading days (≈1 month) "
                "ago, in percentage points.",
    "Δ3m (pp)": "Change in the premium vs 63 trading days (≈3 months) "
                "ago, in percentage points.",
    "ΔYTD (pp)": "Change in the premium since the final trading day of "
                 "last year, in percentage points.",
    "Δ1y (pp)": "Change in the premium vs 252 trading days (≈1 year) "
                "ago, in percentage points.",
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
        NUM_CONFIG[c] = st.column_config.TextColumn(help=COLUMN_HELP.get(c))
    else:
        NUM_CONFIG[c] = st.column_config.NumberColumn(
            format="%.3f" if c == "FX (HKD per 1 CNY)" else "%.2f",
            help=COLUMN_HELP.get(c))


def show_table(t: pd.DataFrame, key: str = "tbl"):
    if t.empty:
        st.info("No companies in this group.")
        return
    st.dataframe(t[DISPLAY_COLS], column_config=NUM_CONFIG,
                 use_container_width=True,
                 height=min(560, 60 + 35 * len(t)), hide_index=True)
    c1, c2 = st.columns([1, 3])
    c1.download_button(
        "⬇️ Download Excel (.xlsx)",
        data=metrics.table_to_xlsx_bytes(t[DISPLAY_COLS], sheet=key),
        file_name=f"ahmon_{key}_{datetime.now(config.TZ):%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument."
             "spreadsheetml.sheet",
        key=f"xlsx_{key}")
    c2.caption("Click any column header to sort (click again to reverse).")


with tabs[1]:
    st.markdown("#### 💡 Buy-level watch")
    watch = signals.buy_watch(table)
    if watch.empty:
        st.info("No company currently meets the buy-level screen "
                "(thresholds in `ahmon/config.py`).")
    else:
        watch_cols = [c for c in watch.columns if c != "Why flagged"]
        st.dataframe(
            watch[watch_cols], hide_index=True, use_container_width=True,
            height=min(420, 60 + 35 * len(watch)),
            column_config={
                **{c: st.column_config.NumberColumn(format="%.2f")
                   for c in watch_cols
                   if watch[c].dtype.kind in "fi"},
                "H Price (HKD)": st.column_config.NumberColumn(
                    format="%.2f",
                    help="Price of the Hong Kong (H) listing, in HK "
                         "dollars."),
                "A Price (CNY)": st.column_config.NumberColumn(
                    format="%.2f",
                    help="Price of the mainland (A) listing, in yuan "
                         "(CNY)."),
                "FX (HKD per 1 CNY)": st.column_config.NumberColumn(
                    format="%.3f",
                    help="Exchange rate, direction matters: this is HK "
                         "dollars per 1 yuan (≈1.16, i.e. ¥1 ≈ "
                         "HK$1.16). The A price is multiplied by this "
                         "to express it in HK dollars before comparing "
                         "with the H price."),
                "Premium calc (%)": st.column_config.NumberColumn(
                    format="%.2f",
                    help="How much more the A share costs than the H "
                         "share, computed by this dashboard from the "
                         "three columns to the left:  "
                         "(A price × HKD-per-CNY ÷ H price − 1) × 100.  "
                         "Example: (¥10.00 × 1.16 ÷ HK$5.80 − 1) × 100 "
                         "= +100%."),
            })
        st.markdown("**Why each company is flagged:**")
        for _, r in watch.iterrows():
            st.markdown(f"- **{r['Company']}** ({r['H Ticker']} · "
                        f"{r['Name (ZH)']}): {r['Why flagged']}")
    st.caption(
        "**How this list is built — automatic rules, not investment "
        "advice.** A company appears here only when all three are true: "
        f"**(1)** its H share is unusually cheap next to its own A share "
        f"— the price gap is in the widest "
        f"{100 - config.SIGNAL_MIN_5Y_PERCENTILE:.0f}% of that company's "
        f"last 5 years; **(2)** the H share would gain at least "
        f"{config.SIGNAL_MIN_H_UPSIDE_PCT:.0f}% just from that gap going "
        f"back to the company's own normal level; **(3)** it passes at "
        f"least {config.SIGNAL_MIN_QUALITY_HITS} of 3 quality checks — "
        f"dividend at least {config.SIGNAL_MIN_H_DIV_YIELD:.0f}%, price "
        f"no more than {config.SIGNAL_MAX_H_PE:.0f}× yearly earnings, "
        f"company worth at least "
        f"HK${config.SIGNAL_MIN_H_MKTCAP_HKD / 1e9:.0f}bn. Companies "
        "with out-of-date prices never appear. Every flagged name is "
        "also recorded once per day in the Alerts tab. An admin can "
        "adjust these rules.")
    st.divider()
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

    groups = {
        "Focus A-H Holdings":
            ftable[ftable["Classification"] == config.FOCUS],
        "Other A-H Stocks":
            ftable[ftable["Classification"] != config.FOCUS],
        "Full A-H Universe": ftable,
    }
    sub = st.tabs(list(groups))
    for stab, (name, t) in zip(sub, groups.items()):
        with stab:
            show_table(t.reset_index(drop=True),
                       key=name.replace(" ", "_").lower())
            if name == "Focus A-H Holdings" and not t.empty:
                rest = table[table["Classification"] != config.FOCUS]
                st.markdown("##### Rest of the A–H universe (summary)")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Companies", len(rest))
                c2.metric("Median premium",
                          f"{rest['Premium calc (%)'].median():.1f}%")
                med_d1 = rest["Δ1d (pp)"].median()
                c3.metric("Median Δ1d",
                          "—" if pd.isna(med_d1) else f"{med_d1:+.2f} pp",
                          help="Needs at least two days of stored history.")
                c4.metric(">5pp movers today",
                          int((rest["Δ1d (pp)"].abs() > 5).sum()))

    st.divider()
    if IS_ADMIN:
        st.markdown("##### Focus list")
        st.caption("The Focus list is stored in the database and "
                   "audit-logged. This button reconciles it against "
                   "`config/focus_list.csv` (the standard 19 names): "
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
with tabs[2]:
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
        "prices and FX — nothing is inferred.")
    st.dataframe(att.style.format(precision=2, na_rep="—"),
                 use_container_width=True, height=480)
    if not att.empty:
        counts = att["Driver"].value_counts()
        fig = go.Figure(go.Bar(
            x=counts.values, y=counts.index, orientation="h",
            marker=dict(color=C["blue"], cornerradius=4)))
        fig.update_layout(title="Companies by main driver (today)")
        st.plotly_chart(styled(fig, 300), use_container_width=True)

# ------------------------------------------------------------ 4 Rankings
with tabs[3]:
    # Same three groups as the Stock Monitor (always unfiltered here).
    rank_scopes = {
        "Focus A-H Holdings":
            table[table["Classification"] == config.FOCUS],
        "Other A-H Stocks":
            table[table["Classification"] != config.FOCUS],
        "Full A-H Universe": table,
    }
    st.markdown(
        "**Read everything as H-share upside** (the H leg is the one a "
        "southbound/foreign investor can buy, and the working thesis is "
        "that H converges toward A over time). *H discount to A* is how "
        "far the H share trades below its A twin — a 100% premium means "
        "the H share costs half the A price, so full convergence would "
        "return the premium itself. Full convergence has been rare "
        "historically, so the conservative anchor is each company's own "
        "5-year median premium: **H upside to 5y median** is the H-share "
        "return if today's premium merely reverts to that norm (A price "
        "and FX unchanged). Positive = the discount is wider than usual "
        "→ extra reversion upside; negative = the premium is already "
        "narrower than its own norm — the convergence you are betting on "
        "has largely played out. Always compare a company against its "
        "*own* history: absolute premium levels differ hugely across "
        "companies.")
    c1, c2 = st.columns(2)
    scope = c1.selectbox("Universe", list(rank_scopes))
    key = c2.selectbox("Ranking", list(metrics.RANKINGS))
    rk = metrics.rankings(rank_scopes[scope], key)
    st.dataframe(
        rk, hide_index=True, use_container_width=True,
        column_config={c: st.column_config.NumberColumn(format="%.2f")
                       for c in rk.columns
                       if rk[c].dtype.kind in "fi"})
    st.caption("Click any column header to re-sort.")
    st.markdown("##### 52-week premium extremes (full universe)")
    ext = metrics.extremes_52w(table)
    if ext.empty:
        st.info("No company is at a 52-week premium extreme today.")
    else:
        st.dataframe(ext.style.format(precision=2), use_container_width=True)

# ------------------------------------------------------------- 5 Sectors
with tabs[4]:
    st.markdown(
        "Each company is assigned to one of eight sectors in the editable "
        "classification file (`config/portfolio_sample.csv`). The numbers "
        "below are computed from the same per-company premiums shown in the "
        "Stock Monitor" +
        (" — currently **synthetic sample data**" if n_sample else "") +
        ". *Median premium* = the middle company's A-share premium within "
        "the sector.")
    st.dataframe(metrics.sector_stats(conn, table)
                 .style.format(precision=2, na_rep="—"),
                 use_container_width=True)
    hist = metrics.sector_history(conn)
    fig = go.Figure()
    for i, sector in enumerate(config.SECTORS):
        g = hist[hist["sector"] == sector]
        if g.empty:
            continue
        s = pd.Series(g["median_premium"].values,
                      index=pd.DatetimeIndex(g["date"])).resample("ME").last()
        fig.add_scatter(x=s.index, y=s.values, name=sector, mode="lines",
                        line=dict(color=SECTOR_COLOR[sector], width=2))
    fig.update_layout(title="Sector median A-share premium (monthly)")
    st.plotly_chart(styled(fig, 420), use_container_width=True)
    st.caption("Each line = the median A-share premium of that sector's "
               "companies at each month-end. A falling line means the "
               "sector's A shares got cheaper relative to their H shares "
               "(premium narrowing); a rising line means widening.")

# -------------------------------------------------------------- 6 Charts
with tabs[5]:
    pick = st.selectbox("Company", table["Company"].sort_values(),
                        key="chart_pick")
    cid = int(table[table["Company"] == pick].iloc[0]["company_id"])
    g = db.daily_df(conn, cid)
    prem = pd.Series(g["premium_calc"].values, index=g["date"])
    rng = st.radio("Range", list(RANGE_KEYS), index=1, horizontal=True,
                   key="stock_rng",
                   help="Time window for the premium-history chart below. "
                        "3m plots daily observations, 1y weekly, 3y/5y/max "
                        "monthly. Daily history is always stored regardless "
                        "of what is displayed.")
    s = calc.resample_for_range(prem, RANGE_KEYS[rng])
    fig = line_chart(s, f"{pick} A-share premium (%)")
    fig.update_layout(title=f"{pick} — A-share premium history (%)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"This chart is specific to {pick}: its calculated A-share "
               "premium over the selected range.")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### Current premium vs 5-year range")
        r5 = calc.rolling_stats(prem, config.WINDOW_5Y)
        if r5["median"] is not None:
            fig = go.Figure()
            fig.add_shape(type="line", x0=r5["low"], x1=r5["high"], y0=0, y1=0,
                          line=dict(color=INK["axis"], width=6))
            fig.add_scatter(x=[r5["median"]], y=[0], mode="markers",
                            name="5y median",
                            marker=dict(color=INK["muted"], size=14,
                                        symbol="line-ns-open"))
            fig.add_scatter(x=[prem.iloc[-1]], y=[0], mode="markers",
                            name="Current",
                            marker=dict(color=C["blue"], size=16))
            fig.update_yaxes(visible=False)
            fig.update_xaxes(title="Premium (%)")
            st.plotly_chart(styled(fig, 200), use_container_width=True)
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

    st.markdown("##### Premium distribution across the A–H universe (today)")
    fig = go.Figure(go.Histogram(
        x=table["Premium calc (%)"], nbinsx=30,
        marker=dict(color=C["blue"],
                    line=dict(color=INK["surface"], width=2))))
    fig.update_xaxes(title="A-share premium (%)")
    fig.update_yaxes(title="Companies")
    st.plotly_chart(styled(fig, 300), use_container_width=True)

# -------------------------------------------------------------- 7 Alerts
with tabs[6]:
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

# ---------------------------------------------------------- 8 Commentary
with tabs[7]:
    st.subheader("Automated daily commentary (template, calculated facts)")
    st.markdown(commentary.daily_commentary(table, att))
    st.divider()
    st.subheader("Closing summary")
    st.markdown(commentary.closing_summary(
        table, att, metrics.sector_stats(conn, table)))
    st.divider()
    st.subheader("Automated weekly commentary")
    st.markdown(commentary.period_commentary(table, "1w"))
    st.divider()
    st.subheader("Automated monthly commentary")
    st.markdown(commentary.period_commentary(table, "1m"))
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
