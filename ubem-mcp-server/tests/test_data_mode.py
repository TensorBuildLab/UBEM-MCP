"""Tests for data-mode stocks: loading, query, export and assign_archetypes."""

import csv
import json

import pytest

from ubem_mcp_server.urban.stock import stock_from_geojson

from .test_urban_stock import _manager, fc, feature, rect


def _multi(*rects):
    return {"type": "MultiPolygon", "coordinates": [r["coordinates"] for r in rects]}


def _courtyard():
    outer = rect(0, 100, 40, 40)["coordinates"][0]
    inner = rect(10, 110, 20, 20)["coordinates"][0][::-1]
    return {"type": "Polygon", "coordinates": [outer, inner]}


INVENTORY = fc(
    feature(rect(0, 0, 12, 10), building="house", id="h1", PARCEL="111", LAND_USE="0131"),
    feature(rect(30, 0, 12, 10), building="house", id="h2", start_date="1954", PARCEL="112", LAND_USE="0131"),
    feature(rect(60, 0, 3, 3), building="shed", id="shed1", PARCEL="112", LAND_USE="0131"),
    feature(rect(90, 0, 3, 2), building="yes", id="tiny"),
    feature(_multi(rect(0, 50, 20, 20), rect(30, 50, 10, 10)), building="office", **{"building:levels": "3"},
            id="office1", PARCEL="200", LAND_USE="0310"),
    feature(_courtyard(), building="school", height="12", id="school1", PARCEL="300", LAND_USE="0710"),
)


def test_data_mode_keeps_inventory_as_described():
    stock = stock_from_geojson(INVENTORY, mode="data")
    assert stock.mode == "data"
    ids = {b.building_id for b in stock.buildings}
    assert ids == {"h1", "h2", "shed1", "tiny", "office1", "school1"}  # nothing excluded

    h1, h2 = stock.get("h1"), stock.get("h2")
    assert (h1.year_built, h1.vintage, h1.num_floors, h1.height_m) == (None, None, None, None)
    assert h1.inferred_fields == [] and h1.gross_floor_area_m2 is None
    assert (h2.year_built, h2.vintage) == (1954, "pre1980")
    assert stock.get("shed1").archetype is None and stock.get("shed1").use == "shed"

    office = stock.get("office1")
    assert office.full_geometry.geom_type == "MultiPolygon"
    assert office.footprint_area_m2 == pytest.approx(500, rel=0.01)  # both parts
    assert office.gross_floor_area_m2 == pytest.approx(1500, rel=0.01)
    school = stock.get("school1")
    assert school.footprint_area_m2 == pytest.approx(1600 - 400, rel=0.01)  # courtyard kept out
    assert school.height_m == 12 and school.num_floors is None

    s = stock.summary()
    assert s["mode"] == "data" and s["num_excluded"] == 0
    assert s["known_share"]["year_built"] == pytest.approx(1 / 6, abs=1e-3)
    assert s["gross_floor_area_known_for"] == 1
    assert s["by_vintage"]["unknown"] == 5


def test_model_mode_is_unchanged_by_default():
    stock = stock_from_geojson(INVENTORY)
    assert stock.mode == "model"
    assert {b.building_id for b in stock.buildings} == {"h1", "h2", "office1", "school1"}
    assert stock.get("h1").year_built == 1980 and "year_built" in stock.get("h1").inferred_fields
    assert stock.get("h1").use == "house"


def _data_stock(tmp_path, stock_id="inv"):
    src = tmp_path / "inventory.geojson"
    src.write_text(json.dumps(INVENTORY))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(src), stock_id=stock_id, mode="data", license="CC0-1.0")
    return mgr


def test_data_stock_persists_and_cannot_be_modeled(tmp_path):
    _data_stock(tmp_path)
    fresh = _manager(tmp_path)
    stock = fresh.get("inv")
    assert stock.mode == "data" and len(stock) == 6
    assert stock.get("office1").full_geometry.geom_type == "MultiPolygon"
    with pytest.raises(ValueError, match="assign_archetypes"):
        fresh.generate_models("inv", climate_zone="2B")


def test_query_filters_groups_and_pages(tmp_path):
    mgr = _data_stock(tmp_path)
    res = mgr.query_stock("inv", group_by=["LAND_USE"], metrics=["count", "sum:footprint_area_m2"])
    assert res["num_matching"] == 6 and res["num_groups"] == 4
    top = res["groups"][0]
    assert top["LAND_USE"] == "0131" and top["count"] == 3
    assert top["known_share"]["year_built"] == pytest.approx(1 / 3, abs=1e-3)
    assert res["groups"][-1]["LAND_USE"] == "unknown"

    res = mgr.query_stock("inv", filters={"use": ["house", "shed"], "footprint_area_m2": {"min": 50}})
    assert res["num_matching"] == 2
    res = mgr.query_stock("inv", filters={"year_built": {"is_null": True}}, sample=2, columns=["PARCEL"])
    assert res["num_matching"] == 5 and len(res["sample"]) == 2 and res["next_offset"] == 2
    assert "PARCEL" in res["sample"][0]
    res = mgr.query_stock("inv", group_by=["decade"])
    assert {g["decade"] for g in res["groups"]} == {1950, "unknown"}
    with pytest.raises(ValueError, match="Bad metric"):
        mgr.query_stock("inv", metrics=["avg:height_m"])


def test_export_writes_file_and_metadata(tmp_path):
    mgr = _data_stock(tmp_path)
    res = mgr.export_stock("inv", fmt="csv", columns=["PARCEL"], filters={"use": "house"}, name="houses")
    assert res["rows"] == 2
    rows = list(csv.DictReader(open(res["path"], encoding="utf-8")))
    assert {r["building_id"] for r in rows} == {"h1", "h2"} and rows[0]["PARCEL"]
    assert float(rows[0]["latitude"]) == pytest.approx(32.2226, abs=0.01)
    meta = json.loads(open(res["metadata_path"], encoding="utf-8").read())
    assert meta["sources"][0]["license"] == "CC0-1.0" and meta["filters"] == {"use": "house"}

    res = mgr.export_stock("inv", fmt="geojson", name="all")
    gj = json.loads(open(res["path"], encoding="utf-8").read())
    office = next(f for f in gj["features"] if f["id"] == "office1")
    assert office["geometry"]["type"] == "MultiPolygon" and office["properties"]["num_floors"] == 3
    with pytest.raises(ValueError, match="format"):
        mgr.export_stock("inv", fmt="xlsx")


def test_enrich_fills_unknown_values_in_data_stock(tmp_path):
    mgr = _data_stock(tmp_path)
    layer = tmp_path / "parcels.geojson"
    layer.write_text(json.dumps(fc(feature(rect(-2, -2, 16, 14), YEAR="1948", OWNER_NAME="X"))))
    res = mgr.enrich("inv", str(layer), {"year_built": "YEAR"}, new_stock_id="inv2")
    assert res["mode"] == "data"
    stock = mgr.get("inv2")
    assert stock.get("h1").year_built == 1948 and stock.get("h1").inferred_fields == []
    assert stock.get("h2").year_built == 1954  # known value untouched
    assert len(stock) == 6


def test_assign_archetypes_derives_modeling_stock(tmp_path):
    mgr = _data_stock(tmp_path)
    layer = tmp_path / "parcels.geojson"
    layer.write_text(json.dumps(fc(feature(rect(-2, -2, 16, 14), YEAR="1948"))))
    mgr.enrich("inv", str(layer), {"year_built": "YEAR"}, new_stock_id="inv2")

    res = mgr.assign_archetypes("inv2", new_stock_id="model", default_year=1960)
    assert res["mode"] == "model"
    model = mgr.get("model")
    assert {b.building_id for b in model.buildings} == {"h1", "h2", "office1", "school1"}
    h1 = model.get("h1")
    assert h1.year_built == 1948 and "year_built" not in h1.inferred_fields  # joined value kept
    assert h1.archetype == "single_family" and "num_floors" in h1.inferred_fields
    assert model.get("h2").year_built == 1954
    assert model.get("office1").num_floors == 3
    assert mgr.get_meta("model")["derived_from"] == "inv2"
    assert mgr.get_meta("model")["sources"][0]["license"] == "CC0-1.0"
    assert len(mgr.get("inv2")) == 6  # data stock unchanged
    with pytest.raises(ValueError, match="already a modeling stock"):
        mgr.assign_archetypes("model")


def test_generic_building_tag_is_unknown_use():
    stock = stock_from_geojson(INVENTORY, mode="data")
    assert stock.get("tiny").use is None  # building=yes says nothing about use
    assert stock.summary()["known_share"]["use"] == pytest.approx(5 / 6, abs=1e-3)


def test_enrich_gives_parcel_use_only_to_buildings_and_keeps_columns(tmp_path):
    mgr = _data_stock(tmp_path)
    layer = tmp_path / "parcels.geojson"
    # One parcel under the "yes" building and the shed: a house parcel.
    layer.write_text(json.dumps(fc(feature(rect(55, -5, 45, 20), USE_CLASS="house", PARCEL="112", PARCEL_USE="0131",
                                           MAIL1="1 Main St"))))
    res = mgr.enrich("inv", str(layer), {"use": "USE_CLASS"}, new_stock_id="inv2",
                     keep_columns=["PARCEL_USE", "PARCEL"])
    stock = mgr.get("inv2")
    tiny, shed = stock.get("tiny"), stock.get("shed1")
    assert tiny.use == "house" and tiny.archetype == "single_family"
    assert shed.archetype is None and shed.use == "shed"  # a shed stays a shed
    assert res["enrichment"]["filled"] == {"use": 1}
    assert res["enrichment"]["use_not_given_to_unconditioned"] == 1
    assert tiny.source_properties["PARCEL_USE"] == "0131"
    assert shed.source_properties["PARCEL"] == "112" and "layer_PARCEL" not in shed.source_properties  # same value
    assert "MAIL1" not in json.dumps(tiny.source_properties)
    q = mgr.query_stock("inv2", group_by=["PARCEL_USE"], sample=0)
    assert {g["PARCEL_USE"]: g["count"] for g in q["groups"]}["0131"] == 2

    with pytest.raises(ValueError, match="personal"):
        mgr.enrich("inv", str(layer), {"use": "USE_CLASS"}, new_stock_id="inv3", keep_columns=["MAIL1"])
