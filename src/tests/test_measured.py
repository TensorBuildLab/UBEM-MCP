"""Measured energy: loading, matching, units, Guideline 14 metrics, comparison."""

import asyncio
import csv
import json
import os
import time
from pathlib import Path

import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import measured
from ubem_mcp_server.urban.stock import stock_from_geojson

from .test_data_mode import INVENTORY
from .test_project_record import EPW
from .test_urban_stock import _manager

needs_ep = pytest.mark.skipif(not os.path.exists(Config().energyplus.idd_path), reason="EnergyPlus not installed")


def _rows(*dicts):
    keys = list(dicts[0])
    return [{k: str(d.get(k, "")) for k in keys} for d in dicts]


def test_units_numbers_and_months():
    assert measured.detect_unit("Electricity Use - Grid Purchase (kWh)") == "kwh"
    assert measured.detect_unit("Natural Gas Use (therms)") == "therm"
    assert measured.detect_unit("Site Energy Use (kBtu)") == "kbtu"
    assert measured.detect_unit("Property GFA - Self-Reported (ft²)") == "ft2"
    assert measured.detect_unit("Largest Property Use Type") is None
    # snake_case column names, as in many open-data portals
    assert measured.detect_unit("electricity_kwh") == "kwh"
    assert measured.detect_unit("naturalgas_therms") == "therm"
    assert measured.detect_unit("gas_gj_2019") == "gj"
    assert measured.detect_unit("bldg_gfa_sf") == "ft2"
    assert measured.detect_unit("propertygfabuildings") is None
    assert measured._number("1,234.5") == 1234.5 and measured._number("Not Available") is None
    assert [measured._month(v) for v in ("7", "2023-07", "Jul", "july", "13", "")] == [7, 7, 7, 7, None, None]
    assert measured._norm_id("123.0") == measured._norm_id(" 123 ") == "123"


def test_match_by_id_groups_buildings_and_never_keeps_other_columns():
    stock = stock_from_geojson(INVENTORY, mode="data")
    rows = _rows({"PID": "111", "Owner Name": "Jane Doe", "Elec (kWh)": "10,000", "Gas (therms)": "100"},
                 {"PID": "112", "Owner Name": "John Roe", "Elec (kWh)": "20000", "Gas (therms)": ""},
                 {"PID": "999", "Owner Name": "X", "Elec (kWh)": "5", "Gas (therms)": "5"},
                 {"PID": "200", "Owner Name": "Y", "Elec (kWh)": "Not Available", "Gas (therms)": ""})
    data = measured.load_measured(stock, rows, {"electricity": "Elec (kWh)", "natural_gas": "Gas (therms)"},
                                  id_column="PID", match_field="PARCEL")
    m = data["match"]
    assert (m["rows_matched"], m["rows_unmatched"], m["rows_without_data"]) == (2, 1, 1)
    assert m["unmatched_examples"] == ["999"]
    groups = {tuple(g["building_ids"]): g for g in data["groups"]}
    assert groups[("h1",)]["annual_kwh"] == {"electricity": 10000.0, "natural_gas": pytest.approx(2930.7107)}
    assert ("h2", "shed1") in groups and m["groups_with_several_buildings"] == 1  # one parcel, two structures
    assert "Doe" not in json.dumps(data) and "Owner" not in json.dumps(data)


def test_match_by_location_and_rows_sharing_buildings_merge():
    stock = stock_from_geojson(INVENTORY, mode="data")
    from shapely.geometry import mapping
    office = stock.get("office1")
    c = stock.frame.to_wgs84(office.footprint.representative_point())
    rows = _rows({"lat": c.y, "lon": c.x, "kwh": 500, "acct": "a"},
                 {"lat": c.y, "lon": c.x, "kwh": 700, "acct": "b"},
                 {"lat": 0, "lon": 0, "kwh": 1, "acct": "c"})
    data = measured.load_measured(stock, rows, {"electricity": "kwh"}, lat_column="lat", lon_column="lon")
    assert data["match"]["rows_matched"] == 2 and data["match"]["rows_unmatched"] == 1
    assert len(data["groups"]) == 1 and data["groups"][0]["annual_kwh"]["electricity"] == 1200
    assert data["groups"][0]["rows"] == 2
    assert mapping  # imported for parity with GIS use; keeps flake quiet


def test_monthly_rows_and_floor_area():
    stock = stock_from_geojson(INVENTORY, mode="data")
    rows = _rows(*[{"id": "h1", "month": f"2023-{k:02d}", "kwh": 100 + k, "area (ft2)": 1000} for k in range(1, 13)])
    data = measured.load_measured(stock, rows, {"electricity": "kwh", "floor_area": "area (ft2)"},
                                  id_column="id", month_column="month")
    g = data["groups"][0]
    assert data["monthly"] and g["monthly_kwh"]["electricity"][6] == 107
    assert g["annual_kwh"]["electricity"] == sum(100 + k for k in range(1, 13))
    assert g["floor_area_m2"] == pytest.approx(92.90304)


def test_input_errors_are_clear():
    stock = stock_from_geojson(INVENTORY, mode="data")
    rows = _rows({"id": "h1", "kwh": 1, "x": 2})
    with pytest.raises(KeyError, match="nope"):
        measured.load_measured(stock, rows, {"electricity": "nope"}, id_column="id")
    with pytest.raises(ValueError, match="Unit"):
        measured.load_measured(stock, rows, {"electricity": "x"}, id_column="id")
    with pytest.raises(ValueError, match="id_column"):
        measured.load_measured(stock, rows, {"electricity": "kwh"})
    with pytest.raises(ValueError, match="energy column"):
        measured.load_measured(stock, rows, {"floor_area": "x"}, units={"floor_area": "m2"}, id_column="id")
    with pytest.raises(ValueError, match="Unknown measured"):
        measured.load_measured(stock, rows, {"water": "x"}, id_column="id")


def test_guideline14_formulas():
    s = measured.g14([100, 100, 100, 100], [90, 110, 100, 100], p=1)
    assert s == {"n": 4, "nmbe_percent": 0.0, "cvrmse_percent": pytest.approx(8.16, abs=0.01)}
    s = measured.g14([100, 100], [80, 80], p=0)
    assert s["nmbe_percent"] == 20.0 and s["cvrmse_percent"] == 20.0  # positive NMBE = model too low
    assert measured.g14([100], [90], p=1) is None


def test_compare_reports_district_spread_archetypes_and_caveats():
    data = {"groups": [
        {"group_id": "g1", "building_ids": ["a"], "annual_kwh": {"electricity": 1000.0, "natural_gas": 500.0},
         "floor_area_m2": 100.0},
        {"group_id": "g2", "building_ids": ["b", "c"], "annual_kwh": {"electricity": 2000.0},
         "floor_area_m2": 100.0},
        {"group_id": "g3", "building_ids": ["d"], "annual_kwh": {"electricity": 10.0}, "floor_area_m2": None},
    ]}
    modeled = {
        "a": {"annual_kwh": {"electricity": 1100.0, "natural_gas": 400.0}, "monthly_kwh": {}, "floor_area_m2": 100},
        "b": {"annual_kwh": {"electricity": 900.0}, "monthly_kwh": {}, "floor_area_m2": 80},
        "c": {"annual_kwh": {"electricity": 900.0, "natural_gas": 50.0}, "monthly_kwh": {}, "floor_area_m2": 80},
    }
    out = measured.compare(data, modeled, {"a": "small_office", "b": "single_family", "c": "single_family"},
                           weather_note="weather differs")
    s = out["summary"]
    assert s["meters_compared"] == 2 and s["meters_skipped"] == 1
    row_a = next(r for r in out["rows"] if r["group_id"] == "g1")
    assert row_a["percent_error"]["electricity"] == 10.0 and row_a["percent_error"]["natural_gas"] == -20.0
    assert row_a["percent_error"]["total_of_measured_fuels"] == 0.0
    row_b = next(r for r in out["rows"] if r["group_id"] == "g2")
    assert row_b["modeled_kwh"]["electricity"] == 1800.0  # two buildings summed
    assert row_b["modeled_kwh"]["total_of_measured_fuels"] == 1800.0  # gas not measured, not counted
    assert row_b["area_ratio"] == 1.6
    assert s["district"]["electricity"] == {"meters": 2, "measured_kwh": 3000, "modeled_kwh": 2900,
                                            "percent_error": -3.3}
    assert s["by_archetype"]["single_family"]["median_percent_error"] == -10.0
    assert s["largest_differences"][0]["group_id"] == "g2"
    assert any("weather" in c for c in s["caveats"]) and any("floor area" in c for c in s["caveats"])
    assert any("not compared" in c for c in s["caveats"])


def test_compare_monthly_guideline14():
    meas = [100.0] * 12
    data = {"groups": [{"group_id": "g1", "building_ids": ["a"], "annual_kwh": {"electricity": 1200.0},
                        "monthly_kwh": {"electricity": meas}, "floor_area_m2": None}]}
    modeled = {"a": {"annual_kwh": {"electricity": 1236.0}, "monthly_kwh": {"electricity": [103.0] * 12},
                     "floor_area_m2": 50}}
    out = measured.compare(data, modeled, {"a": "x"})
    stats = out["rows"][0]["monthly"]["electricity"]
    assert stats["nmbe_percent"] == pytest.approx(-3.27, abs=0.01) and stats["meets_guideline14_monthly"]
    assert out["summary"]["monthly_guideline14"]["meeting_thresholds"] == 1
    assert len(measured.monthly_from_hourly([1.0] * 8760)) == 12
    assert measured.monthly_from_hourly([1.0] * 8760)[1] == 28 * 24


def test_load_measured_saves_dataset_with_licence(tmp_path):
    (tmp_path / "inv.geojson").write_text(json.dumps(INVENTORY))
    (tmp_path / "bench.csv").write_text("PID,Owner,Site Energy Use (kBtu)\n111,Jane Doe,34121\n999,X,1\n")
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(tmp_path / "inv.geojson"), stock_id="inv", mode="data")
    out = mgr.load_measured("inv", str(tmp_path / "bench.csv"), {"total": "Site Energy Use (kBtu)"},
                            id_column="PID", match_field="PARCEL", year=2023, license="CC0-1.0")
    assert out["measured_id"] == "measured_2023" and out["match"]["rows_matched"] == 1
    assert any("matched no building" in w for w in out["warnings"])
    saved = json.loads((Path(out["measured_directory"]) / "measured.json").read_text())
    assert saved["groups"][0]["annual_kwh"]["total"] == pytest.approx(10000, rel=1e-3)
    assert saved["source"]["license"] == "CC0-1.0" and "Jane" not in json.dumps(saved)
    with pytest.raises(ValueError, match="already exist"):
        mgr.load_measured("inv", str(tmp_path / "bench.csv"), {"total": "Site Energy Use (kBtu)"},
                          id_column="PID", match_field="PARCEL", year=2023)
    with pytest.raises(KeyError, match="No measured data"):
        mgr.compare_measured("inv", "baseline", "nope")


@pytest.mark.energyplus
@needs_ep
def test_compare_to_measured_end_to_end(tmp_path, monkeypatch):
    """Simulate, then compare with 'measured' data that are 10% above the model."""
    from ubem_mcp_server import server
    from ubem_mcp_server.batch_manager import BatchManager

    (tmp_path / "inv.geojson").write_text(json.dumps(INVENTORY))
    mgr = _manager(tmp_path)
    mgr.config.batch.batch_output_dir = str(tmp_path / "batches")
    monkeypatch.setattr(server, "stock_manager", mgr)
    monkeypatch.setattr(server, "batch_manager", BatchManager(mgr.config))
    mgr.load_from_file(str(tmp_path / "inv.geojson"), stock_id="inv", default_year=1970)

    async def run():
        _, sim = await server.mcp.call_tool("simulate_building_stock", {
            "stock_id": "inv", "climate_zone": "3C", "weather_file": EPW, "building_ids": ["h1", "h2"],
            "model_set_id": "baseline"})
        assert sim["ok"], sim
        deadline = time.time() + 900
        while not (st := (await server.mcp.call_tool("get_batch_status",
                                                     {"batch_id": sim["batch"]["batch_id"]}))[1])["is_complete"]:
            assert time.time() < deadline
            await asyncio.sleep(2)
        assert st["counts"] == {"success": 2}
        _, agg = await server.mcp.call_tool("aggregate_stock_results", {"batch_id": sim["batch"]["batch_id"],
                                                                       "make_map": False})
        model = {r["building_id"]: r for r in csv.DictReader(open(agg["files"]["buildings_csv"]))}
        with open(tmp_path / "bills.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "Electricity (kWh)", "Gas (kWh)"])
            for bid in ("h1", "h2"):
                w.writerow([bid, float(model[bid]["electricity_kwh"]) * 1.1, float(model[bid]["natural_gas_kwh"]) * 1.1])
        _, load = await server.mcp.call_tool("load_measured_energy", {
            "stock_id": "inv", "csv_path": str(tmp_path / "bills.csv"), "year": 2023, "id_column": "id",
            "columns": {"electricity": "Electricity (kWh)", "natural_gas": "Gas (kWh)"}})
        assert load["ok"] and load["match"]["rows_matched"] == 2, load
        _, cmp_ = await server.mcp.call_tool("compare_to_measured", {
            "stock_id": "inv", "model_set_id": "baseline", "measured_id": "measured_2023"})
        return cmp_

    cmp_ = asyncio.run(run())
    assert cmp_["ok"] and cmp_["meters_compared"] == 2, json.dumps(cmp_, indent=1)[:3000]
    for fuel in ("electricity", "natural_gas"):
        assert cmp_["district"][fuel]["percent_error"] == pytest.approx(-9.1, abs=0.2)
    assert cmp_["across_meters_guideline14"]["electricity"]["nmbe_percent"] > 0  # model below measured
    assert any("2023" in c for c in cmp_["caveats"])
    assert Path(cmp_["files"]["meters_csv"]).exists()


def test_scan_runs_reads_the_main_err_file_not_the_sqlite_one(tmp_path):
    from ubem_mcp_server.urban.stock_manager import scan_runs

    run = tmp_path / "runs" / "0000_b1"
    run.mkdir(parents=True)
    (run / "b1.err").write_text("   ************* EnergyPlus Completed Successfully-- 0 Warning; 0 Severe Errors\n")
    (run / "b1Sqlite.err").write_text("SQLite3 message, Opening SQLite database\n")
    (run / "b1.sql").write_text("")
    assert scan_runs(tmp_path / "runs", ["b1"])["b1"]["status"] == "success"


def test_implausible_measured_eui_is_listed_not_counted():
    data = {"groups": [
        {"group_id": "ok", "building_ids": ["a"], "annual_kwh": {"total": 15000.0}, "floor_area_m2": 100.0},
        {"group_id": "bad", "building_ids": ["b"], "annual_kwh": {"total": 200.0}, "floor_area_m2": 100.0},
    ]}
    modeled = {"a": {"annual_kwh": {"electricity": 18000.0}, "monthly_kwh": {}, "floor_area_m2": 100},
               "b": {"annual_kwh": {"electricity": 30000.0}, "monthly_kwh": {}, "floor_area_m2": 100}}
    out = measured.compare(data, modeled, {"a": "small_office", "b": "small_office"})
    s = out["summary"]
    assert s["meters_compared"] == 1 and s["district"]["total"]["measured_kwh"] == 15000
    assert s["meters_suspect"][0]["group_id"] == "bad" and "2.0 kWh/m2" in s["meters_suspect"][0]["reason"]
    assert any("implausible" in c for c in s["caveats"])
    assert {r["group_id"] for r in out["rows"]} == {"ok", "bad"}  # still in the per-meter table
