"""Tests for retrofit measures, scenarios and scenario comparison."""
import csv
import json
import os
import re
from types import SimpleNamespace

import pytest
from shapely.geometry import box

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import library, retrofit
from ubem_mcp_server.urban.aggregate import compare_results
from ubem_mcp_server.urban.modelgen import ModelBuilder
from ubem_mcp_server.urban.stock import Building

IDD_PATH = Config().energyplus.idd_path
needs_idd = pytest.mark.skipif(not os.path.exists(IDD_PATH), reason="EnergyPlus IDD not installed")


def _apply(archetype, vintage, measure, cz="2B"):
    p = library.resolve(archetype, vintage, cz)
    return p, *retrofit.MEASURES[measure["type"]]["apply"](p, {**measure})


# --------------------------------------------------------------------------- #
# Measures
# --------------------------------------------------------------------------- #

def test_lighting_target_and_reduction():
    p, new, note = _apply("small_office", "pre1980", {"type": "lighting", "target_w_m2": 6.5})
    assert new.lighting_w_m2 == 6.5 < p.lighting_w_m2 and "lighting" in note
    _, new, _ = _apply("small_office", "pre1980", {"type": "lighting", "reduction": 0.5})
    assert new.lighting_w_m2 == pytest.approx(p.lighting_w_m2 / 2)
    # Already below the target: not applied.
    _, same, note = _apply("small_office", "post2004", {"type": "lighting", "target_w_m2": 50})
    assert note is None and same.lighting_w_m2 == library.resolve("small_office", "post2004", "2B").lighting_w_m2


def test_envelope_measures():
    p, new, _ = _apply("small_office", "pre1980", {"type": "wall_insulation", "added_r": 2.0})
    assert new.wall_r == pytest.approx(p.wall_r + 2.0)
    _, new, note = _apply("small_office", "pre1980", {"type": "roof_insulation", "target_r": 0.1})
    assert note is None  # target below the existing roof
    _, new, _ = _apply("small_office", "pre1980", {"type": "windows", "u_factor": 1.4, "shgc": 0.25})
    assert (new.window_u, new.window_shgc) == (1.4, 0.25)
    _, new, _ = _apply("small_office", "pre1980", {"type": "cool_roof"})
    assert new.roof_solar_absorptance == 0.3


def test_air_sealing_uses_the_building_infiltration_model():
    p, new, _ = _apply("small_office", "pre1980", {"type": "air_sealing", "reduction": 0.4})
    assert new.infiltration_ach == pytest.approx(p.infiltration_ach * 0.6)
    p, new, note = _apply("single_family", "pre1980", {"type": "air_sealing", "reduction": 0.4})
    assert new.ela_cm2_per_m2_envelope == pytest.approx(p.ela_cm2_per_m2_envelope * 0.6) and "leakage" in note


def test_plug_loads_do_not_mutate_the_library():
    p, new, _ = _apply("small_office", "pre1980", {"type": "plug_loads", "reduction": 0.25})
    assert new.electric_equipment[0]["w_per_m2"] == pytest.approx(p.electric_equipment[0]["w_per_m2"] * 0.75)
    again = library.resolve("small_office", "pre1980", "2B")
    assert again.electric_equipment[0]["w_per_m2"] == p.electric_equipment[0]["w_per_m2"]


def test_heat_pump_applies_to_packaged_systems_only():
    p, new, note = _apply("single_family", "pre1980", {"type": "heat_pump", "heating_cop": 3.5})
    assert p.heating_fuel == "NaturalGas"
    assert (new.heat_pump_heating_cop, new.heating_fuel) == (3.5, "Electricity") and "heat pump" in note
    _, same, note = _apply("large_office", "post2004", {"type": "heat_pump"})
    assert note is None and same.heat_pump_heating_cop is None


def test_hvac_efficiency_and_water_heater():
    p, new, note = _apply("small_office", "pre1980", {"type": "hvac_efficiency", "cooling_cop": 4.5})
    assert new.cooling_cop == 4.5 > p.cooling_cop
    _, new, _ = _apply("single_family", "post2004", {"type": "heat_pump_water_heater"})
    assert (new.water_heater_fuel, new.water_heater_efficiency) == ("Electricity", 3.0)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("spec, msg", [
    ({"name": "x", "measures": [{"type": "solar_panels"}]}, "Unknown measure"),
    ({"name": "x", "measures": [{"type": "lighting", "watts": 5}]}, "unknown parameters"),
    ({"name": "x", "measures": [{"type": "windows"}]}, "missing parameter"),
    ({"name": "x", "measures": [{"type": "lighting", "reduction": 1.5}]}, "reduction"),
    ({"name": "x", "measures": [{"type": "lighting", "reduction": 0.5, "target_w_m2": 5}]}, "exactly one"),
    ({"name": "x y", "measures": [{"type": "cool_roof"}]}, "name"),
    ({"name": "x", "measures": [{"type": "cool_roof"}], "adoption_rate": 0}, "adoption_rate"),
    ({"name": "x", "measures": [{"type": "cool_roof"}], "vintages": ["1950s"]}, "vintages"),
    ({"name": "x", "measures": []}, "measures"),
    ({"name": "x", "measures": [{"type": "cool_roof"}], "budget": 5}, "Unknown scenario keys"),
])
def test_invalid_scenarios_are_rejected(spec, msg):
    with pytest.raises(ValueError, match=msg):
        retrofit.validate_scenario(spec)


def _b(bid, archetype="single_family", vintage="pre1980"):
    return SimpleNamespace(building_id=bid, archetype=archetype, vintage=vintage)


def test_adoption_is_targeted_and_reproducible():
    buildings = [_b(f"h{i}") for i in range(10)] + [_b("o1", "small_office"), _b("n1", vintage="post2004")]
    spec = retrofit.validate_scenario({"name": "s", "measures": [{"type": "cool_roof"}],
                                       "archetypes": ["single_family"], "vintages": ["pre1980"],
                                       "adoption_rate": 0.3, "seed": 7})
    adopters, targeted = retrofit.adopting_buildings(spec, buildings)
    assert targeted == 10 and len(adopters) == 3 and all(a.startswith("h") for a in adopters)
    assert retrofit.adopting_buildings(spec, reversed(buildings))[0] == adopters


def test_hook_applies_per_measure_filters():
    spec = retrofit.validate_scenario({"name": "s", "measures": [
        {"type": "lighting", "reduction": 0.5},
        {"type": "heat_pump", "archetypes": ["single_family"]}]})
    hook = retrofit.scenario_hook(spec, {"h", "o"})
    _, applied = hook(_b("h"), library.resolve("single_family", "pre1980", "2B"))
    assert [a["measure"] for a in applied] == ["lighting", "heat_pump"]
    p, applied = hook(_b("o", "small_office"), library.resolve("small_office", "pre1980", "2B"))
    assert [a["measure"] for a in applied] == ["lighting"] and p.heat_pump_heating_cop is None
    _, applied = hook(_b("x"), library.resolve("single_family", "pre1980", "2B"))
    assert applied == []  # not an adopter


# --------------------------------------------------------------------------- #
# IDF output of the new options
# --------------------------------------------------------------------------- #

@needs_idd
@pytest.mark.parametrize("archetype, template", [
    ("single_family", "HVACTemplate:System:UnitaryHeatPump:AirToAir"),
    ("small_hotel", "HVACTemplate:Zone:PTHP"),
])
def test_heat_pump_and_cool_roof_in_idf(archetype, template):
    from ubem_mcp_server.urban.idd import load_idd

    p = library.resolve(archetype, "pre1980", "2B")
    for m in ({"type": "heat_pump", "heating_cop": 3.4}, {"type": "cool_roof", "solar_absorptance": 0.25}):
        p, _ = retrofit.MEASURES[m["type"]]["apply"](p, m)
    b = Building("b", box(0, 0, 20, 12), archetype, 1970, "pre1980", 2, 3.0, 6.0)
    text = ModelBuilder(load_idd(IDD_PATH), p).build(b)
    assert re.search(rf"^{re.escape(template)},", text, re.M)
    assert "HVACTemplate:System:Unitary," not in text and "HVACTemplate:Zone:PTAC," not in text
    assert re.search(r"3\.4,\s*!- Heat Pump Heating Coil (Gross )?Rated COP", text)
    roof = text[text.index("Roof membrane,"):].split(";")[0]
    assert re.search(r"0\.25,\s*!- Solar Absorptance", roof)


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def _results(d, rows, hourly):
    d.mkdir(parents=True)
    cols = ["building_id", "archetype", "vintage", "floor_area_m2", "total_kwh", "site_eui_kwh_m2",
            "electricity_kwh", "natural_gas_kwh"]
    with open(d / "buildings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[0], r[1], "pre1980", 100.0, r[2] + r[3], (r[2] + r[3]) / 100, r[2], r[3]])
    with open(d / "district_hourly.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hour_of_year", "month", "day", "hour", "electricity_kw", "natural_gas_kw"])
        for i, (e, g) in enumerate(hourly):
            w.writerow([i + 1, 1, 1, i + 1, e, g])
    (d / "summary.json").write_text("{}")


def test_compare_results_reports_fuel_switching_and_peaks(tmp_path):
    _results(tmp_path / "base", [("a", "single_family", 4000, 6000), ("b", "small_office", 10000, 1000)],
             [(5, 9), (6, 2)])
    _results(tmp_path / "scen", [("a", "single_family", 6000, 0), ("b", "small_office", 10500, 1000)],
             [(7, 1), (6, 1)])
    manifest = {"scenario": {"name": "hp"}, "baseline_model_set_id": "base", "model_set_id": "base__hp",
                "adoption": {"buildings_retrofitted": 2},
                "models": [{"building_id": "a", "retrofit": [{"measure": "heat_pump", "change": "..."}]},
                           {"building_id": "b", "retrofit": [{"measure": "cool_roof", "change": "..."}]}]}
    s = compare_results(str(tmp_path / "base"), str(tmp_path / "scen"), str(tmp_path / "cmp"), manifest,
                        emission_factors={"electricity": 0.4, "natural_gas": 0.2})
    d = s["district"]
    assert d["baseline_kwh"] == 21000 and d["scenario_kwh"] == 17500
    assert d["savings_pct"] == pytest.approx(100 * 3500 / 21000)
    assert d["by_fuel"]["natural_gas"]["change_pct"] == pytest.approx(-100 * 6000 / 7000)
    assert d["by_fuel"]["electricity"]["scenario_kwh"] == 16500
    assert d["peak_kw"]["electricity"]["baseline"]["kw"] == 6 and d["peak_kw"]["electricity"]["scenario"]["kw"] == 7
    em = d["emissions_kg_co2e"]
    assert em["avoided"] == pytest.approx((0.4 * 14000 + 0.2 * 7000) - (0.4 * 16500 + 0.2 * 1000))
    assert [r["building_id"] for r in s["retrofitted_buildings_with_increased_use"]] == ["b"]
    assert s["by_archetype"]["single_family"]["savings_pct"] == pytest.approx(40.0)
    with open(s["files"]["buildings_comparison_csv"], newline="") as f:
        rows = {r["building_id"]: r for r in csv.DictReader(f)}
    assert float(rows["a"]["savings_kwh"]) == 4000 and rows["a"]["measures"] == "heat_pump"
    assert json.loads(open(s["files"]["comparison_json"]).read())["coverage"]["buildings_compared"] == 2
