"""Tests for data provenance: personal-field removal, source records and licence warnings."""

import json

import pytest

from ubem_mcp_server.urban import provenance
from ubem_mcp_server.urban.stock import stock_from_geojson

from .test_urban_stock import _manager, fc, feature, rect


@pytest.mark.parametrize("name", ["OWNER_NAME", "owner", "MAIL_ADDR1", "mailing_address", "MAIL_ZIP",
                                  "Email", "e-mail", "phone", "TAXPAYER", "first_name", "Grantee",
                                  "MAIL1", "mail5"])
def test_personal_fields_are_recognized(name):
    assert provenance.is_personal_field(name)


@pytest.mark.parametrize("name", ["PARCEL_USE", "building", "addr:street", "SITUS_ADDR", "residential_units",
                                  "year_built", "height", "name", "building:levels", "PARCEL", "LAND_VALUE",
                                  "MAILBOX_COUNT"])
def test_building_fields_are_kept(name):
    assert not provenance.is_personal_field(name)


def test_scrub_handles_saved_source_properties():
    props = {"building": "house", "OWNER_NAME": "A. Person",
             "source_properties": {"MAIL_ADDR1": "1 Main St", "PARCEL_USE": "0131"}}
    clean, removed = provenance.scrub_personal_fields(props)
    assert clean == {"building": "house", "source_properties": {"PARCEL_USE": "0131"}}
    assert sorted(removed) == ["MAIL_ADDR1", "OWNER_NAME"]


def test_loader_drops_personal_columns_by_default():
    data = fc(feature(rect(0, 0, 20, 20), building="house", OWNER_NAME="A. Person", MAIL_ZIP="85719"),
              feature(rect(40, 0, 20, 20), building="house", OWNER_NAME="B. Person"))
    stock = stock_from_geojson(data)
    assert all("OWNER_NAME" not in b.source_properties for b in stock.buildings)
    assert stock.summary()["personal_fields_removed"] == ["MAIL_ZIP", "OWNER_NAME"]

    kept = stock_from_geojson(data, keep_personal_fields=True)
    assert kept.buildings[0].source_properties["OWNER_NAME"] == "A. Person"
    assert kept.summary()["personal_fields_removed"] == []


def test_load_records_source_licence_and_warnings(tmp_path):
    src = tmp_path / "parcels.geojson"
    src.write_text(json.dumps(fc(feature(rect(0, 0, 20, 20), building="house", OWNER_NAME="A. Person"))))
    mgr = _manager(tmp_path)
    res = mgr.load_from_file(str(src), stock_id="s1")
    assert res["sources"] == [{"name": "parcels.geojson", "license": "unknown"}]
    assert any("licence of parcels.geojson is unknown" in w for w in res["warnings"])
    assert any("OWNER_NAME" in w for w in res["warnings"])
    saved = json.loads((mgr.stock_dir("s1") / "buildings.geojson").read_text())
    assert "OWNER_NAME" not in json.dumps(saved)

    res = mgr.load_from_file(str(src), stock_id="s2", license="CC-BY-4.0", attribution="City of Tucson")
    assert res["attribution"] == "City of Tucson"
    assert not any("licence" in w for w in res["warnings"])
    saved = json.loads((mgr.stock_dir("s2") / "buildings.geojson").read_text())
    assert saved["ubem_mcp"]["attribution"] == "City of Tucson"


def test_keep_personal_fields_survives_restart(tmp_path):
    src = tmp_path / "parcels.geojson"
    src.write_text(json.dumps(fc(feature(rect(0, 0, 20, 20), building="house", OWNER_NAME="A. Person", id="b1"))))
    _manager(tmp_path).load_from_file(str(src), stock_id="kept", keep_personal_fields=True)
    fresh = _manager(tmp_path)
    assert fresh.get("kept").buildings[0].source_properties["OWNER_NAME"] == "A. Person"

    _manager(tmp_path).load_from_file(str(src), stock_id="dropped")
    fresh = _manager(tmp_path)
    assert fresh.get("dropped").summary()["personal_fields_removed"] == ["OWNER_NAME"]


def test_enrich_merges_sources_and_flags_odbl_mixing(tmp_path):
    base = tmp_path / "osm.geojson"
    base.write_text(json.dumps(fc(feature(rect(0, 0, 20, 20), building="yes", osm_id="p"))))
    layer = tmp_path / "assessor.geojson"
    layer.write_text(json.dumps(fc(feature(rect(-5, -5, 30, 30), YEAR="1955", OWNER="X"))))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(base), stock_id="base", license="ODbL-1.0", attribution="© OpenStreetMap contributors")
    res = mgr.enrich("base", str(layer), {"year_built": "YEAR"}, new_stock_id="joined")
    assert [s["name"] for s in res["sources"]] == ["osm.geojson", "assessor.geojson"]
    assert any("ODbL" in w and "assessor.geojson" in w for w in res["warnings"])

    res = mgr.enrich("base", str(layer), {"year_built": "YEAR"}, new_stock_id="joined_pd",
                     layer_license="public domain", layer_attribution="Pima County")
    assert not any("ODbL" in w for w in res["warnings"])
    assert res["attribution"] == "© OpenStreetMap contributors; Pima County"


def test_osm_source_record():
    s = provenance.osm_source(osm_timestamp="2026-10-16T22:23:34Z")
    assert s["license"] == "ODbL-1.0" and "OpenStreetMap contributors" in s["attribution"]
    assert "2026-10-16" in s["notes"]
