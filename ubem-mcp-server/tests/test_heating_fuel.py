"""Per-building heating fuel: read from data, joined from a layer, applied to models."""

import json
import re

import pytest

from ubem_mcp_server.urban.archetypes import normalize_heating_fuel
from ubem_mcp_server.urban.library import resolve, with_heating_fuel
from ubem_mcp_server.urban.stock import stock_from_geojson

from .test_project_record import EPW, needs_idd
from .test_urban_stock import _manager, fc, feature, rect


def test_fuel_spellings():
    for v in ("electricity", "Electric", "ELEC", "all-electric", "heat pump"):
        assert normalize_heating_fuel(v) == "electricity", v
    for v in ("natural_gas", "Natural Gas", "gas", "NG", "naturalgas"):
        assert normalize_heating_fuel(v) == "natural_gas", v
    for v in (None, "", "oil", "steam", "e"):
        assert normalize_heating_fuel(v) is None, v


def test_electric_building_drops_every_gas_use():
    base = resolve("midrise_apartment", "pre1980", "4C")
    assert base.heating_fuel == "NaturalGas" and base.water_heater_fuel == "NaturalGas"
    p = with_heating_fuel(base, "electricity")
    assert (p.heating_fuel, p.heating_efficiency) == ("Electricity", 1.0)
    assert (p.water_heater_fuel, p.water_heater_efficiency) == ("Electricity", 1.0)
    assert not p.gas_equipment and p.ahu_heating_coil != "Gas"
    assert "heating fuel electricity from the building data" in p.assumptions[-1]
    assert len(base.assumptions) == len(p.assumptions) - 1  # the original is not changed
    assert with_heating_fuel(base, "natural_gas") is base  # already gas
    assert with_heating_fuel(base, None) is base

    kitchen = resolve("full_service_restaurant", "pre1980", "4C")
    assert kitchen.gas_equipment
    pk = with_heating_fuel(kitchen, "electricity")
    assert not pk.gas_equipment
    assert sum(e["w_per_m2"] for e in pk.electric_equipment) == pytest.approx(
        sum(e["w_per_m2"] for e in kitchen.electric_equipment) + sum(g["w_per_m2"] for g in kitchen.gas_equipment))
    assert "gas process loads as electric" in pk.assumptions[-1]


def test_loader_reads_column_and_notes_unknown_values():
    stock = stock_from_geojson(fc(
        feature(rect(0, 0, 12, 10), building="house", id="a", heating_fuel="Electric"),
        feature(rect(30, 0, 12, 10), building="house", id="b", HEATING_FUEL="oil"),
        feature(rect(60, 0, 12, 10), building="house", id="c"),
    ))
    assert stock.get("a").heating_fuel == "electricity"
    assert stock.get("b").heating_fuel is None and any("oil" in n for n in stock.get("b").notes)
    assert stock.get("c").heating_fuel is None
    again = stock_from_geojson(stock.to_geojson())  # survives a save and reload
    assert again.get("a").heating_fuel == "electricity"


def test_enrich_joins_fuel_and_archetype_step_keeps_it(tmp_path):
    src = tmp_path / "inv.geojson"
    src.write_text(json.dumps(fc(
        feature(rect(0, 0, 30, 25), building="apartments", id="apt", **{"building:levels": "4"}),
        feature(rect(60, 0, 12, 10), building="house", id="h"),
    )))
    layer = tmp_path / "meters.geojson"
    layer.write_text(json.dumps(fc(
        feature({"type": "Point", "coordinates": rect(10, 10, 1, 1)["coordinates"][0][0]}, FUEL="electric"),
        feature({"type": "Point", "coordinates": rect(65, 5, 1, 1)["coordinates"][0][0]}, FUEL="wood"),
    )))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(src), stock_id="inv", mode="data", license="CC0-1.0")
    res = mgr.enrich("inv", str(layer), {"heating_fuel": "FUEL"}, new_stock_id="inv2")
    assert res["enrichment"]["filled"] == {"heating_fuel": 1}
    assert res["enrichment"]["unrecognized_heating_fuel_values"] == 1
    assert mgr.get("inv2").get("apt").heating_fuel == "electricity"
    q = mgr.query_stock("inv2", group_by=["heating_fuel"], sample=0)
    assert {g["heating_fuel"]: g["count"] for g in q["groups"]} == {"electricity": 1, "unknown": 1}
    mgr.assign_archetypes("inv2", new_stock_id="model", default_year=1960)
    assert mgr.get("model").get("apt").heating_fuel == "electricity"


def _block(idf: str, obj: str) -> str:
    m = re.search(re.escape(obj) + r",(.*?);", idf, re.S)
    return m.group(1) if m else ""


@needs_idd
def test_generated_models_follow_the_building_fuel(tmp_path):
    src = tmp_path / "inv.geojson"
    src.write_text(json.dumps(fc(
        feature(rect(0, 0, 12, 10), building="house", id="gas_house"),
        feature(rect(40, 0, 12, 10), building="house", id="elec_house", heating_fuel="electricity"),
        feature(rect(0, 80, 70, 70), building="office", id="tower", heating_fuel="electricity",
                **{"building:levels": "12"}),
    )))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(src), stock_id="s", license="CC0-1.0")
    mgr.resolve_weather("s", weather_file=EPW)
    res = mgr.generate_models("s", model_set_id="m")
    man = json.loads(open(res["manifest"], encoding="utf-8").read())
    by_id = {m["building_id"]: m for m in man["models"]}
    assert by_id["gas_house"]["heating_fuel"] == "natural_gas" and "heating_fuel_from" not in by_id["gas_house"]
    assert by_id["elec_house"]["heating_fuel"] == "electricity"
    assert by_id["elec_house"]["heating_fuel_from"] == "building data"
    assert any("heating fuel electricity" in a for a in by_id["elec_house"]["assumptions"])

    gas_idf = open(by_id["gas_house"]["idf_path"], encoding="utf-8").read()
    elec_idf = open(by_id["elec_house"]["idf_path"], encoding="utf-8").read()
    assert "NaturalGas" in gas_idf.replace("NaturalGas:Facility", "")
    assert "NaturalGas" not in elec_idf.replace("NaturalGas:Facility", "")  # the meter is still reported
    assert "Electricity" in _block(elec_idf, "WaterHeater:Mixed")

    tower = by_id["tower"]
    assert tower["archetype"] == "large_office"  # central plant: the boiler follows the fuel
    boiler = _block(open(tower["idf_path"], encoding="utf-8").read(), "HVACTemplate:Plant:Boiler")
    assert "Electricity" in boiler and "NaturalGas" not in boiler
