"""ComStock/ResStock reference data: download format, weighting, savings, benchmark."""

import asyncio
import csv
import gzip
import io
import json
import os
import tarfile

import pytest

from ubem_mcp_server.urban import buildstock as bs

from .test_urban_stock import _manager

TB = bs.TBTU_TO_KWH


def _tar_gz(name, rows):
    """A .csv.gz the way OEDI publishes them: a gzipped tar holding one CSV."""
    text = io.StringIO()
    w = csv.DictWriter(text, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    data = text.getvalue().encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


def _comstock(upgrade=0):
    rows = []
    for i, (btype, w, sqft, kwh) in enumerate([("SmallOffice", 2.0, 5000, 100_000), ("SmallOffice", 1.0, 10000, 300_000),
                                               ("Warehouse", 3.0, 20000, 200_000)]):
        e = kwh * (0.8 if upgrade else 1.0)
        rows.append({"bldg_id": i + 1, "upgrade": upgrade, "weight": w, "in.sqft..ft2": sqft,
                     "applicability": "True" if (not upgrade or btype == "SmallOffice") else "False",
                     "in.comstock_building_type": btype, "in.vintage": "1980 to 1989",
                     "in.nhgis_county_gisjoin": "G0400190", "in.ashrae_iecc_climate_zone_2006": "2B",
                     "in.heating_fuel": "NaturalGas",
                     "calc.weighted.electricity.total.energy_consumption..tbtu": 0.75 * e * w / TB,
                     "calc.weighted.natural_gas.total.energy_consumption..tbtu": 0.25 * e * w / TB,
                     "calc.weighted.site_energy.total.energy_consumption..tbtu": e * w / TB})
    return _tar_gz(f"AZ_G0400190_upgrade{upgrade}_basic.csv", rows)


def _resstock():
    rows = []
    for i, (county, btype, sqft, kwh, status) in enumerate([
            ("G0400190", "Single-Family Detached", 1500, 12000, "Success"),
            ("G0400190", "Single-Family Detached", 2000, 18000, "Success"),
            ("G0400190", "Mobile Home", 1000, 9000, "Success"),
            ("G0400130", "Single-Family Detached", 1800, 15000, "Success"),
            ("G0400190", "Single-Family Detached", 1600, 0, "Fail")]):
        rows.append({"bldg_id": 10 + i, "completed_status": status, "upgrade": 0, "weight": 250.0,
                     "applicability": "True", "in.sqft..ft2": sqft, "in.geometry_building_type_recs": btype,
                     "in.vintage": "1980s", "in.county": county, "in.ashrae_iecc_climate_zone_2004": "2B",
                     "out.electricity.total.energy_consumption..kwh": kwh * 0.7,
                     "out.natural_gas.total.energy_consumption..kwh": kwh * 0.3,
                     "out.site_energy.total.energy_consumption..kwh": kwh})
    return _tar_gz("AZ_upgrade0.csv", rows)


def _fake_fetch(calls):
    def fetch(url, timeout=None):
        calls.append(url)
        if url.endswith("upgrades_lookup.json"):
            return json.dumps({"0": "Baseline", "1": "Heat pump RTU"}).encode()
        if "comstock" in url:
            return _comstock(1 if "upgrade1" in url else 0)
        return _resstock()
    return fetch


@pytest.fixture
def mgr(tmp_path):
    m = _manager(tmp_path)
    m.calls = []
    m.fetch = _fake_fetch(m.calls)
    return m


def test_urls_and_county_codes():
    info = bs.release_info("comstock")
    assert bs.county_gisjoin("AZ", "04019") == bs.county_gisjoin("AZ", "019") == "G0400190"
    url = bs.file_url(info, "az", "04019", 0)
    assert url.endswith("state=AZ/county=G0400190/AZ_G0400190_upgrade0_basic.csv.gz")
    assert bs.file_url(bs.release_info("resstock"), "AZ", None, 4).endswith("state=AZ/AZ_upgrade4.csv.gz")
    with pytest.raises(ValueError, match="county"):
        bs.file_url(info, "AZ", None, 0)
    with pytest.raises(ValueError, match="state"):
        bs.file_url(info, "XX", "04019", 0)
    with pytest.raises(ValueError, match="dataset"):
        bs.release_info("buildingstock")


def test_normalize_reads_tar_csv_and_derives_kwh_from_weighted_totals():
    df = bs.normalize(_comstock(), bs.release_info("comstock"))
    assert list(df["bldg_id"]) == [1, 2, 3]
    assert df.loc[0, "site_total_kwh"] == pytest.approx(100_000)
    assert df.loc[0, "electricity_kwh"] == pytest.approx(75_000)
    assert df.loc[0, "eui_kwh_m2"] == pytest.approx(100_000 / (5000 * bs.FT2_TO_M2))
    assert df.loc[0, "building_type"] == "SmallOffice" and "in.heating_fuel" in df.columns
    rs = bs.normalize(_resstock(), bs.release_info("resstock"), county="G0400190")
    assert len(rs) == 3  # other county and the failed run dropped


def test_fetch_keeps_a_compact_copy_with_attribution(mgr):
    meta = mgr.fetch_reference("comstock", "AZ", "04019")
    assert meta["dataset_id"] == "comstock_az_g0400190_upgrade0" and meta["samples"] == 3
    assert meta["buildings_represented"] == 6.0 and meta["building_types"] == {"SmallOffice": 2, "Warehouse": 1}
    assert meta["upgrade_name"] == "Baseline" and meta["source"]["license"] == "CC-BY-4.0"
    assert "NREL ComStock" in meta["attribution"] and meta["data_dictionary"].endswith("data_dictionary.tsv")
    n = len(mgr.calls)
    assert mgr.fetch_reference("comstock", "AZ", "04019")["reused"] is True and len(mgr.calls) == n
    with pytest.raises(KeyError, match="fetched so far"):
        mgr.query_reference("nope")


def test_weighted_summary_flags_small_groups(mgr):
    did = mgr.fetch_reference("comstock", "AZ", "04019")["dataset_id"]
    q = mgr.query_reference(did, group_by=["building_type"], min_samples=2)
    office = next(g for g in q["groups"] if g["group"]["building_type"] == "SmallOffice")
    assert office["samples"] == 2 and office["buildings_represented"] == 3.0
    assert office["energy_kwh"]["site_total"] == pytest.approx(2 * 100_000 + 1 * 300_000, abs=1)
    area = (2 * 5000 + 1 * 10000) * bs.FT2_TO_M2
    assert office["eui_kwh_m2"] == pytest.approx(500_000 / area, abs=0.1)
    warehouse = next(g for g in q["groups"] if g["group"]["building_type"] == "Warehouse")
    assert warehouse["few_samples"] and "fewer than 2" in q["sample_warning"]
    only = mgr.query_reference(did, filters={"building_type": "Warehouse"})
    assert only["overall"]["samples"] == 1
    by_fuel = mgr.query_reference(did, group_by=["heating_fuel"])  # any in.* column, without the prefix
    assert by_fuel["groups"][0]["group"] == {"heating_fuel": "NaturalGas"}


def test_upgrade_savings_only_where_it_applies(mgr):
    base = mgr.fetch_reference("comstock", "AZ", "04019")["dataset_id"]
    upg = mgr.fetch_reference("comstock", "AZ", "04019", upgrade=1)
    assert upg["upgrade_name"] == "Heat pump RTU"
    s = mgr.query_reference(upg["dataset_id"], baseline_dataset_id=base)
    assert s["applicable_samples"] == 2 and s["overall"]["site_energy_savings_percent"] == pytest.approx(20.0)
    assert s["overall"]["site_total_savings_kwh"] == pytest.approx(0.2 * 500_000, abs=1)
    rs = mgr.fetch_reference("resstock", "AZ", "04019")["dataset_id"]
    with pytest.raises(ValueError, match="same dataset"):
        mgr.query_reference(upg["dataset_id"], baseline_dataset_id=rs)


def test_benchmark_places_modeled_eui_in_reference(mgr):
    cs = mgr.fetch_reference("comstock", "AZ", "04019")["dataset_id"]
    rs = mgr.fetch_reference("resstock", "AZ", "04019")["dataset_id"]
    res = mgr.root / "campus" / "models" / "baseline" / "results" / "latest"
    res.mkdir(parents=True)
    (res / "summary.json").write_text("{}")
    with open(res / "buildings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["building_id", "archetype", "site_eui_kwh_m2"])
        w.writerows([["a", "small_office", 200], ["b", "small_office", 260], ["c", "single_family", 90],
                     ["d", "hospital", 500]])
    out = mgr.benchmark_reference("campus", "baseline", [cs, rs])
    rows = {r["archetype"]: r for r in out["archetypes"]}
    assert rows["small_office"]["modeled_median_eui_kwh_m2"] == 230.0
    assert rows["small_office"]["reference"]["building_type"] == "SmallOffice"
    assert rows["single_family"]["reference"]["dataset"] == "resstock"
    assert 0 <= rows["single_family"]["modeled_median_percentile_in_reference"] <= 100
    assert rows["hospital"]["reference"]["samples"] == 0  # type absent from this county's sample
    assert "not whether either is right" in out["note"] and "ResStock" in out["attribution"]


def test_weighted_quantiles():
    assert bs.weighted_quantiles([1, 2, 3, 4], [1, 1, 1, 1], (0.5,)) == [2]
    assert bs.weighted_quantiles([1, 10], [1, 9], (0.5,)) == [10]
    assert bs.weighted_percentile_of(5, [1, 10], [1, 3]) == 25.0


def test_tools_through_mcp(mgr, monkeypatch):
    from ubem_mcp_server import server

    monkeypatch.setattr(server, "stock_manager", mgr)

    async def run():
        _, f = await server.mcp.call_tool("fetch_comstock_resstock", {"dataset": "comstock", "state": "AZ",
                                                                      "county": "04019"})
        assert f["ok"], f
        _, q = await server.mcp.call_tool("query_comstock_resstock", {"dataset_id": f["dataset_id"],
                                                                      "group_by": ["building_type"]})
        assert q["ok"] and q["overall"]["samples"] == 3
        _, bad = await server.mcp.call_tool("fetch_comstock_resstock", {"dataset": "comstock", "state": "AZ"})
        assert bad["ok"] is False and bad["error_type"] == "invalid_input"
    asyncio.run(run())


@pytest.mark.skipif(os.getenv("UBEM_NETWORK_TESTS") != "1", reason="set UBEM_NETWORK_TESTS=1 to download from OEDI")
def test_live_oedi_download(tmp_path):
    m = _manager(tmp_path)
    meta = m.fetch_reference("resstock", "AZ", "04019")
    assert meta["samples"] > 500 and meta["buildings_represented"] > 100_000
    q = m.query_reference(meta["dataset_id"], group_by=["building_type"])
    sfd = next(g for g in q["groups"] if g["group"]["building_type"] == "Single-Family Detached")
    assert 40 < sfd["eui_kwh_m2"] < 300
