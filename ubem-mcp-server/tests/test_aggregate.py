"""Tests for stock results aggregation, using small synthetic EnergyPlus SQLite files."""
import csv
import json
import sqlite3

import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import aggregate as agg
from ubem_mcp_server.urban.stock_manager import StockManager, scan_runs

HOURS = 8760


def _fake_sql(path, elec_kwh_per_h, gas_kwh_per_h, area_m2, end_uses):
    """Write the parts of an EnergyPlus SQLite file the aggregation reads.

    end_uses: {(end use row, fuel column): kWh}."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE TabularDataWithStrings (ReportName, TableName, RowName, ColumnName, Units, Value);
        CREATE TABLE EnvironmentPeriods (EnvironmentPeriodIndex, SimulationIndex, EnvironmentName, EnvironmentType);
        CREATE TABLE Time (TimeIndex, Month, Day, Hour, EnvironmentPeriodIndex, WarmupFlag);
        CREATE TABLE ReportDataDictionary (ReportDataDictionaryIndex, Name, ReportingFrequency);
        CREATE TABLE ReportData (TimeIndex, ReportDataDictionaryIndex, Value);
    """)
    rpt = "AnnualBuildingUtilityPerformanceSummary"
    for (use, fuel), kwh in end_uses.items():
        con.execute("INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?)", (rpt, "End Uses", use, fuel, "kWh", f"{kwh}"))
    for row, val in (("Total Building Area", area_m2), ("Net Conditioned Building Area", area_m2 * 0.9)):
        con.execute("INSERT INTO TabularDataWithStrings VALUES (?,?,?,?,?,?)", (rpt, "Building Area", row, "Area", "m2", f"{val}"))
    con.execute("INSERT INTO EnvironmentPeriods VALUES (1, 1, 'SIZING', 1)")
    con.execute("INSERT INTO EnvironmentPeriods VALUES (3, 1, 'ANNUAL', 3)")
    con.execute("INSERT INTO ReportDataDictionary VALUES (9, 'Electricity:Facility', 'Hourly')")
    con.execute("INSERT INTO ReportDataDictionary VALUES (10, 'NaturalGas:Facility', 'Hourly')")
    con.execute("INSERT INTO Time VALUES (0, 7, 21, 1, 1, 0)")  # sizing period: must be ignored
    con.execute("INSERT INTO ReportData VALUES (0, 9, ?)", (1e12,))
    for i in range(HOURS):
        con.execute("INSERT INTO Time VALUES (?, 0, 0, 0, 3, 0)", (i + 1,))
        con.execute("INSERT INTO ReportData VALUES (?, 9, ?)", (i + 1, elec_kwh_per_h(i) * 3.6e6))
        con.execute("INSERT INTO ReportData VALUES (?, 10, ?)", (i + 1, gas_kwh_per_h(i) * 3.6e6))
    con.commit()
    con.close()


def test_hour_to_date():
    assert agg._hour_to_date(0) == (1, 1, 1)
    assert agg._hour_to_date(23) == (1, 1, 24)
    assert agg._hour_to_date(24 * 31) == (2, 1, 1)
    assert agg._hour_to_date(HOURS - 1) == (12, 31, 24)


def test_building_result_reads_annual_and_hourly(tmp_path):
    sql = tmp_path / "b.sql"
    _fake_sql(sql, lambda i: 2.0 + (i == 5000) * 8.0, lambda i: 1.0, 100.0,
              {("Heating", "Natural Gas"): 8760.0, ("Cooling", "Electricity"): 10000.0,
               ("Interior Lighting", "Electricity"): 7530.0, ("Total End Uses", "Electricity"): 1.0})
    r = agg.building_result(str(sql))
    assert r["by_fuel_kwh"] == pytest.approx({"natural_gas": 8760.0, "electricity": 17530.0})
    assert r["by_end_use_kwh"]["cooling"] == pytest.approx(10000.0)
    assert r["site_eui_kwh_m2"] == pytest.approx((8760 + 17530) / 100.0)
    assert r["peak_kw"] == pytest.approx({"electricity": 10.0, "natural_gas": 1.0})
    assert len(r["hourly_kw"]["electricity"]) == HOURS  # sizing-period rows are excluded


def _stock_with_runs(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = str(tmp_path / "outputs")
    sm = StockManager(cfg)
    lon, lat, d = -110.95, 32.23, 0.0002
    feats = []
    for i, use in enumerate(["office", "house", "house"]):
        x = lon + i * 3 * d
        feats.append({"type": "Feature", "properties": {"id": f"b{i}", "building": use, "building:levels": 1},
                      "geometry": {"type": "Polygon", "coordinates": [[[x, lat], [x + d, lat], [x + d, lat + d],
                                                                       [x, lat + d], [x, lat]]]}})
    src = tmp_path / "s.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    sm.load_from_file(str(src), stock_id="s")
    stock = sm.get("s")
    # A hand-written model set: b0 and b1 were simulated, b2 failed to simulate.
    model_dir = sm.stock_dir("s") / "models" / "m"
    runs = model_dir / "runs"
    for n, (bid, ok) in enumerate([("b0", True), ("b1", True), ("b2", False)]):
        d_ = runs / f"{n:04d}_{bid}"
        d_.mkdir(parents=True)
        if ok:
            _fake_sql(d_ / f"{bid}.sql", (lambda i, k=n: (1.0 + k) * (2.0 if i == 4000 else 1.0)), (lambda i: 0.5),
                      200.0, {("Cooling", "Electricity"): 8760.0 * (1 + n), ("Heating", "Natural Gas"): 4380.0})
            (d_ / f"{bid}.err").write_text("EnergyPlus Completed Successfully-- 0 Warning; 0 Severe Errors")
        else:
            (d_ / f"{bid}.err").write_text("**  Fatal  ** something broke")
    models = [{"building_id": b.building_id, "archetype": b.archetype, "vintage": b.vintage,
               "num_floors": b.num_floors, "hvac_system": "psz", "inferred_fields": list(b.inferred_fields)}
              for b in stock.buildings]
    (model_dir / "manifest.json").write_text(json.dumps({
        "stock_id": "s", "model_set_id": "m", "climate_zone": "2B", "models": models,
        "failures": [{"building_id": "b9", "error": "ValueError: bad footprint"}]}))
    return sm


def test_aggregate_model_set(tmp_path):
    sm = _stock_with_runs(tmp_path)
    outputs = scan_runs(sm.stock_dir("s") / "models" / "m" / "runs", ["b0", "b1", "b2"])
    assert outputs["b0"]["status"] == "success" and outputs["b2"]["status"] == "failed"

    s = sm.aggregate_results("s", "m", emission_factors={"electricity": 0.5, "natural_gas": 0.2}, make_map=False)
    cov = s["coverage"]
    assert (cov["buildings_with_results"], cov["buildings_without_results"]) == (2, 2)
    assert (sm.stock_dir("s") / "models" / "m" / "results" / "latest" / "ATTRIBUTION.txt").exists()
    assert {m["building_id"]: m["status"] for m in s["without_results"]} == {
        "b2": "failed", "b9": "model generation failed"}

    dist = s["district"]
    assert dist["by_fuel_kwh"]["electricity"] == pytest.approx(8760.0 * 3)
    assert dist["total_kwh"] == pytest.approx(8760.0 * 3 + 4380.0 * 2)
    assert dist["site_eui_kwh_m2"] == pytest.approx(dist["total_kwh"] / 400.0)
    # Both buildings peak in hour 4001 (1 kW and 2 kW base, doubled) -> 6 kW district.
    assert dist["peak_kw"]["electricity"] == {"kw": pytest.approx(6.0), "hour_of_year": 4001,
                                              "month": 6, "day": 16, "hour": 17}
    assert dist["emissions_kg_co2e"] == pytest.approx(0.5 * 8760 * 3 + 0.2 * 4380 * 2)
    assert set(s["by_archetype"]) == {"small_office", "single_family"}

    out = sm.stock_dir("s") / "models" / "m" / "results" / "latest"
    with open(out / "buildings.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["building_id"] for r in rows] == ["b0", "b1"]
    assert float(rows[1]["peak_electricity_kw"]) == pytest.approx(4.0)
    with open(out / "district_hourly.csv", newline="") as f:
        hourly = list(csv.DictReader(f))
    assert len(hourly) == HOURS and float(hourly[0]["electricity_kw"]) == pytest.approx(3.0)
    geo = json.loads((out / "results.geojson").read_text())
    status = {ft["properties"]["building_id"]: ft["properties"]["result_status"] for ft in geo["features"]}
    assert status == {"b0": "success", "b1": "success", "b2": "failed"}
    assert json.loads((out / "summary.json").read_text())["coverage"] == cov


def test_aggregate_writes_map(tmp_path):
    pytest.importorskip("plotly")
    sm = _stock_with_runs(tmp_path)
    s = sm.aggregate_results("s", "m", results_id="r1")
    html = open(s["files"]["map_html"], encoding="utf-8").read()
    assert "Choroplethmap" in html or "choroplethmap" in html
    assert "b0" in html


def test_negative_emission_factor_rejected(tmp_path):
    sm = _stock_with_runs(tmp_path)
    with pytest.raises(ValueError):
        sm.aggregate_results("s", "m", emission_factors={"electricity": -1})
