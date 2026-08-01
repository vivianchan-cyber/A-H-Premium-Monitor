"""Sector map: shipped CSV integrity and database reconciliation."""

from __future__ import annotations

import pandas as pd
import pytest

from ahmon import config, db
from ahmon.sector_map import apply_sector_map, load_sector_map


class TestShippedMap:
    def test_covers_universe_with_valid_sectors_only(self):
        smap = load_sector_map()                 # raises on invalid sectors
        assert len(smap) >= 200                  # full A–H universe
        assert set(smap.values()) <= set(config.SECTORS)
        assert config.SECTOR_UNCLASSIFIED not in smap.values()

    def test_focus_list_fully_covered(self):
        smap = load_sector_map()
        focus = pd.read_csv(config.CONFIG_DIR / "focus_list.csv")
        missing = set(focus["h_ticker"]) - set(smap)
        assert not missing, f"Focus names without a sector: {missing}"

    def test_rejects_unknown_sector(self, tmp_path):
        p = tmp_path / "bad.csv"
        pd.DataFrame({"h_ticker": ["1.HK"], "name_en": ["x"],
                      "sector": ["Utilities"]}).to_csv(p, index=False)
        with pytest.raises(ValueError, match="unknown sectors"):
            load_sector_map(p)

    def test_rejects_duplicate_ticker(self, tmp_path):
        p = tmp_path / "dup.csv"
        pd.DataFrame({"h_ticker": ["1.HK", "1.HK"], "name_en": ["x", "y"],
                      "sector": ["Telecom", "Consumer"]}).to_csv(
            p, index=False)
        with pytest.raises(ValueError, match="duplicate"):
            load_sector_map(p)


class TestApplySectorMap:
    @pytest.fixture
    def conn(self, tmp_path):
        c = db.connect(tmp_path / "t.db")
        for name, h, a, sector in [
            ("PetroChina", "857.HK", "601857.SS", "Oil & Gas"),
            ("Huaneng Power", "902.HK", "600011.SS",
             config.SECTOR_UNCLASSIFIED),
            ("Mystery Co", "9998.HK", "600000.SS",
             config.SECTOR_UNCLASSIFIED),
        ]:
            db.upsert_company(c, name, h, a, sector)
        yield c
        c.close()

    def write_map(self, tmp_path, rows):
        p = tmp_path / "map.csv"
        pd.DataFrame(rows).to_csv(p, index=False)
        return p

    def test_updates_only_mismatches_and_reports_unmapped(
            self, conn, tmp_path):
        p = self.write_map(tmp_path, {
            "h_ticker": ["857.HK", "902.HK"], "name_en": ["x", "y"],
            "sector": ["Oil & Gas", "Industrials"]})
        res = apply_sector_map(conn, p)
        assert res["updated"] == ["902.HK: Unclassified → Industrials"]
        assert res["unmapped"] == ["9998.HK"]    # visible, never guessed
        comps = db.companies_df(conn).set_index("h_ticker")
        assert comps.loc["902.HK", "sector"] == "Industrials"
        assert comps.loc["9998.HK", "sector"] == config.SECTOR_UNCLASSIFIED

    def test_curated_english_name_replaces_feed_placeholder(
            self, conn, tmp_path):
        # the feed adds new companies with the Chinese name as the
        # English placeholder; the CSV corrects it (e.g. 中际旭创 →
        # Zhongji Innolight)
        db.upsert_company(conn, "中际旭创", "3308.HK", "300308.SZ",
                          config.SECTOR_UNCLASSIFIED)
        p = self.write_map(tmp_path, {
            "h_ticker": ["3308.HK"],
            "name_en": ["Zhongji Innolight Co., Ltd."],
            "sector": ["Technology"]})
        res = apply_sector_map(conn, p)
        assert res["updated"] == ["3308.HK: Unclassified → Technology"]
        assert res["renamed"] == \
            ["3308.HK: 中际旭创 → Zhongji Innolight Co., Ltd."]
        comps = db.companies_df(conn).set_index("h_ticker")
        assert comps.loc["3308.HK", "name_en"] == \
            "Zhongji Innolight Co., Ltd."
        assert comps.loc["3308.HK", "sector"] == "Technology"
        res2 = apply_sector_map(conn, p)               # second run: no-op
        assert res2["updated"] == [] and res2["renamed"] == []

    def test_shipped_map_has_innolight(self):
        smap = load_sector_map()
        assert smap["3308.HK"] == "Technology"

    def test_idempotent_and_logged(self, conn, tmp_path):
        p = self.write_map(tmp_path, {
            "h_ticker": ["902.HK"], "name_en": ["y"],
            "sector": ["Industrials"]})
        apply_sector_map(conn, p)
        res = apply_sector_map(conn, p)          # second run: no-op
        assert res["updated"] == []
        log = conn.read_df(
            "SELECT * FROM constituent_log WHERE action='sector_set'")
        assert len(log) == 1                     # only the real change
        assert "Unclassified → Industrials" in log.iloc[0]["detail"]
