"""Focus-list reconciliation and the type-to-filter helper."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import config, db, metrics
from ahmon.focus_list import apply_focus_list


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    for name, h, a, cls in [
        ("PetroChina", "857.HK", "601857.SS", config.FOCUS),
        ("Agricultural Bank of China", "1288.HK", "601288.SS", config.OTHER),
        ("BYD", "1211.HK", "002594.SZ", config.FOCUS),
        ("Anhui Conch", "914.HK", "600585.SS", config.WATCHLIST),
    ]:
        cid = db.upsert_company(c, name, h, a, "Financials")
        db.set_classification(c, cid, cls, source="seed")
    yield c
    c.close()


def write_list(tmp_path, tickers):
    p = tmp_path / "focus.csv"
    pd.DataFrame({"h_ticker": tickers,
                  "name_en": ["x"] * len(tickers)}).to_csv(p, index=False)
    return p


class TestApplyFocusList:
    def test_promotes_demotes_and_leaves_other_tiers(self, conn, tmp_path):
        res = apply_focus_list(conn, write_list(tmp_path,
                                                ["857.HK", "1288.HK"]))
        assert res["promoted"] == ["1288.HK"]      # was Other -> Focus
        assert res["demoted"] == ["1211.HK"]       # was Focus, not listed
        assert res["focus_size"] == 2
        comps = db.companies_df(conn).set_index("h_ticker")
        assert comps.loc["857.HK", "classification"] == config.FOCUS
        assert comps.loc["1288.HK", "classification"] == config.FOCUS
        assert comps.loc["1211.HK", "classification"] == config.OTHER
        # Watchlist member untouched (not Focus, not on list)
        assert comps.loc["914.HK", "classification"] == config.WATCHLIST

    def test_idempotent_and_audited(self, conn, tmp_path):
        p = write_list(tmp_path, ["857.HK", "1288.HK"])
        apply_focus_list(conn, p)
        before = len(db.audit_df(conn))
        res = apply_focus_list(conn, p)             # second run: no-op
        assert res["promoted"] == [] and res["demoted"] == []
        assert len(db.audit_df(conn)) == before
        srcs = set(db.audit_df(conn)["source"])
        assert "focus_list.csv" in srcs

    def test_unknown_ticker_reported_not_created(self, conn, tmp_path):
        res = apply_focus_list(conn, write_list(tmp_path,
                                                ["857.HK", "9999.HK"]))
        assert res["missing_from_db"] == ["9999.HK"]
        assert len(db.companies_df(conn)) == 4      # nothing invented

    def test_shipped_list_has_20_unique_tickers(self):
        lst = pd.read_csv(config.CONFIG_DIR / "focus_list.csv")
        assert len(lst) == 20
        assert lst["h_ticker"].is_unique
        assert "1211.HK" in set(lst["h_ticker"])       # BYD (owner request)


class TestFilterTable:
    T = pd.DataFrame({
        "Company": ["Bank of China", "PetroChina", "BYD"],
        "Name (ZH)": ["中国银行", "中国石油", "比亚迪"],
        "H Ticker": ["3988.HK", "857.HK", "1211.HK"],
        "A Ticker": ["601988.SS", "601857.SS", "002594.SZ"],
        "Classification": ["Focus Holding"] * 3,
        "Sector": ["Financials", "Oil & Gas", "Consumer"],
        "Premium calc (%)": [30.0, 20.0, 19.0],
    })

    def test_matches_across_text_columns_case_insensitive(self):
        assert list(metrics.filter_table(self.T, "bank")["Company"]) == \
            ["Bank of China"]
        assert list(metrics.filter_table(self.T, "601988")["Company"]) == \
            ["Bank of China"]
        assert list(metrics.filter_table(self.T, "中国")["Company"]) == \
            ["Bank of China", "PetroChina"]

    def test_single_column_scope(self):
        out = metrics.filter_table(self.T, "china", column="Sector")
        assert out.empty                            # 'china' not a sector
        out = metrics.filter_table(self.T, "oil", column="Sector")
        assert list(out["Company"]) == ["PetroChina"]

    def test_empty_text_returns_all_and_no_regex_surprises(self):
        assert len(metrics.filter_table(self.T, "")) == 3
        assert len(metrics.filter_table(self.T, "  ")) == 3
        assert len(metrics.filter_table(self.T, "(")) == 0  # literal, no crash
