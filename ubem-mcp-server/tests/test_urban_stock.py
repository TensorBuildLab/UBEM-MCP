"""Tests for urban building-stock intake: GeoJSON/OSM parsing, attribute
normalization and inference, and stock persistence. No network access."""
import json
import math

import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import archetypes, osm
from ubem_mcp_server.urban.stock import load_geojson, stock_from_geojson
from ubem_mcp_server.urban.stock_manager import StockManager

# Downtown Tucson, AZ
LON0, LAT0 = -110.9747, 32.2226
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))


def rect(x_m, y_m, w_m, d_m):
    """Rectangle footprint (lon/lat ring) offset x_m east, y_m north of the origin."""
    def ll(x, y):
        return [LON0 + x / M_PER_DEG_LON, LAT0 + y / M_PER_DEG_LAT]
    return {"type": "Polygon", "coordinates": [[
        ll(x_m, y_m), ll(x_m + w_m, y_m), ll(x_m + w_m, y_m + d_m), ll(x_m, y_m + d_m), ll(x_m, y_m),
    ]]}


def feature(geom, **props):
    return {"type": "Feature", "geometry": geom, "properties": props}


def fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


# --------------------------------------------------------------------------- #
# archetype rules
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("uses, area, floors, expected", [
    (["house"], 150, 1, "single_family"),
    (["residential"], 150, 2, "single_family"),
    (["residential"], 800, 5, "midrise_apartment"),
    (["apartments"], 800, 12, "highrise_apartment"),
    (["office"], 400, 2, "small_office"),
    (["commercial"], 2000, 4, "medium_office"),
    (["office"], 3000, 15, "large_office"),
    ([None, "restaurant", "yes"], 300, 1, "full_service_restaurant"),
    (["Large Office"], 10, 1, "large_office"),  # explicit archetype label wins over size rules
    (["garage"], 30, 1, "_unconditioned"),
])
def test_classify_use(uses, area, floors, expected):
    key, _ = archetypes.classify_use(uses, area, floors)
    assert key == expected


def test_classify_use_unknown_returns_none():
    assert archetypes.classify_use(["yes", None, "spaceport"], 100, 1)[0] is None


@pytest.mark.parametrize("raw, year", [
    (1975, 1975), ("1975-06-01", 1975), ("~1962", 1962), ("1970s", 1970), ("C1950", 1950),
    ("unknown", None), (None, None), (12, None),
])
def test_parse_year(raw, year):
    assert archetypes.parse_year(raw) == year


def test_vintage_bins():
    assert [archetypes.vintage_for_year(y) for y in (1979, 1980, 2003, 2004)] == [
        "pre1980", "1980_2004", "1980_2004", "post2004"]


# --------------------------------------------------------------------------- #
# GeoJSON loading and normalization
# --------------------------------------------------------------------------- #

def test_geometry_is_projected_to_local_metres():
    stock = stock_from_geojson(fc(feature(rect(0, 0, 20, 10), building="house")))
    b = stock.buildings[0]
    assert b.footprint_area_m2 == pytest.approx(200, rel=0.01)
    minx, miny, maxx, maxy = b.footprint.bounds
    assert maxx - minx == pytest.approx(20, rel=0.01)
    assert maxy - miny == pytest.approx(10, rel=0.01)
    assert b.footprint.exterior.is_ccw


def test_attributes_are_read_and_inferred_fields_recorded():
    stock = stock_from_geojson(fc(
        feature(rect(0, 0, 20, 20), building="office", height="12", **{"building:levels": "3"}, start_date="1995"),
        feature(rect(50, 0, 20, 20), building="office", height="40 ft"),
        feature(rect(100, 0, 15, 12), building="yes"),
    ), default_year=1965)
    a, b, c = stock.buildings

    assert (a.archetype, a.num_floors, a.height_m, a.year_built, a.vintage) == ("small_office", 3, 12.0, 1995, "1980_2004")
    assert a.inferred_fields == []

    assert b.height_m == pytest.approx(12.19, abs=0.01)  # 40 ft
    assert b.num_floors == 4  # 12.19 m / 3.05 m small-office floor height
    assert set(b.inferred_fields) == {"num_floors", "year_built"}

    assert c.archetype == "single_family"  # default for unrecognized 'yes'
    assert c.year_built == 1965 and c.vintage == "pre1980"
    assert set(c.inferred_fields) == {"archetype", "num_floors", "height_m", "year_built"}

    summary = stock.summary()
    assert summary["num_buildings"] == 3
    assert summary["inferred_share"]["year_built"] == pytest.approx(2 / 3, abs=1e-3)


def test_explicit_field_names_and_feet():
    stock = stock_from_geojson(
        fc(feature(rect(0, 0, 20, 20), LANDUSE="Retail", HGT=20, YR="2010")),
        use_field="LANDUSE", height_field="HGT", year_field="YR", height_unit="ft",
    )
    b = stock.buildings[0]
    assert b.archetype == "standalone_retail"
    assert b.height_m == pytest.approx(6.096, abs=1e-3)
    assert b.vintage == "post2004"


def test_exclusions_and_geometry_repairs():
    multi = {"type": "MultiPolygon", "coordinates": [rect(0, 0, 20, 20)["coordinates"], rect(40, 0, 5, 5)["coordinates"]]}
    with_courtyard = rect(100, 0, 40, 40)
    with_courtyard["coordinates"].append(rect(110, 10, 10, 10)["coordinates"][0])
    stock = stock_from_geojson(fc(
        feature(rect(0, 50, 3, 3), building="house"),  # 9 m2 -> too small
        feature(rect(0, 100, 10, 10), building="garage"),  # unconditioned
        feature(None, building="house"),
        feature({"type": "Point", "coordinates": [LON0, LAT0]}, building="house"),
        feature(multi, building="house", osm_id="way/1"),
        feature(with_courtyard, building="office", osm_id="way/2"),
    ))
    reasons = stock.summary()["excluded_by_reason"]
    assert reasons == {"too_small": 1, "unconditioned": 1, "no_geometry": 1, "not_a_polygon": 1}
    multi_b, court_b = stock.buildings
    assert multi_b.footprint_area_m2 == pytest.approx(400, rel=0.01)
    assert any("multipolygon" in n for n in multi_b.notes)
    assert court_b.footprint_area_m2 == pytest.approx(1600, rel=0.01)
    assert any("interior ring" in n for n in court_b.notes)


def test_duplicate_ids_are_made_unique():
    stock = stock_from_geojson(fc(
        feature(rect(0, 0, 10, 10), id="A"), feature(rect(20, 0, 10, 10), id="A"),
    ))
    assert [b.building_id for b in stock.buildings] == ["A", "A_2"]


def test_projected_input_crs():
    # Same 20x10 m rectangle given in UTM zone 12N (EPSG:32612).
    from pyproj import Transformer
    t = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)
    x0, y0 = t.transform(LON0, LAT0)
    ring = [[x0, y0], [x0 + 20, y0], [x0 + 20, y0 + 10], [x0, y0 + 10], [x0, y0]]
    data = fc(feature({"type": "Polygon", "coordinates": [ring]}, building="house"))
    data["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32612"}}
    stock = stock_from_geojson(data)
    assert stock.buildings[0].footprint_area_m2 == pytest.approx(200, rel=0.01)
    assert stock.frame.lon0 == pytest.approx(LON0, abs=1e-3)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError, match="FeatureCollection"):
        stock_from_geojson({"type": "Feature"})
    with pytest.raises(ValueError, match="default_archetype"):
        stock_from_geojson(fc(feature(rect(0, 0, 10, 10))), default_archetype="castle")
    with pytest.raises(ValueError, match="No usable"):
        stock_from_geojson(fc(feature(None)))


def test_geojson_round_trip_preserves_buildings(tmp_path):
    stock = stock_from_geojson(fc(
        feature(rect(0, 0, 25, 15), building="apartments", **{"building:levels": 5}, start_date=1972),
        feature(rect(60, 30, 12, 9), building="house"),
    ))
    path = tmp_path / "stock.geojson"
    path.write_text(json.dumps(stock.to_geojson()))
    again = load_geojson(str(path))

    assert stock.frame.lon0 == again.frame.lon0 and stock.frame.lat0 == again.frame.lat0
    for a, b in zip(stock.buildings, again.buildings, strict=True):
        assert a.to_properties() == b.to_properties()
        assert a.footprint.equals_exact(b.footprint, tolerance=1e-3)


# --------------------------------------------------------------------------- #
# OSM
# --------------------------------------------------------------------------- #

def _node(x, y):
    return {"lon": LON0 + x / M_PER_DEG_LON, "lat": LAT0 + y / M_PER_DEG_LAT}


OVERPASS_RESPONSE = {
    "osm3s": {"timestamp_osm_base": "2026-10-01T00:00:00Z"},
    "elements": [
        {"type": "way", "id": 101, "tags": {"building": "house", "start_date": "1958"},
         "geometry": [_node(0, 0), _node(15, 0), _node(15, 10), _node(0, 10), _node(0, 0)]},
        {"type": "way", "id": 102, "tags": {"building": "yes", "amenity": "school", "building:levels": "2"},
         "geometry": [_node(40, 0), _node(100, 0), _node(100, 30), _node(40, 30), _node(40, 0)]},
        {"type": "way", "id": 103, "tags": {"highway": "residential"},
         "geometry": [_node(0, 0), _node(50, 50)]},
        {"type": "way", "id": 104, "tags": {"building": "house"},  # unclosed ring
         "geometry": [_node(0, 50), _node(10, 50), _node(10, 60)]},
        {"type": "relation", "id": 201, "tags": {"building": "office", "type": "multipolygon"},
         "members": [
             {"type": "way", "role": "outer", "geometry": [_node(200, 0), _node(240, 0), _node(240, 40)]},
             {"type": "way", "role": "outer", "geometry": [_node(240, 40), _node(200, 40), _node(200, 0)]},
             {"type": "way", "role": "inner", "geometry": [_node(210, 10), _node(230, 10), _node(230, 30), _node(210, 30), _node(210, 10)]},
         ]},
    ],
}


def test_overpass_to_geojson_and_stock():
    gj = osm.overpass_to_geojson(OVERPASS_RESPONSE)
    assert [f["id"] for f in gj["features"]] == ["way/101", "way/102", "relation/201"]
    stock = stock_from_geojson(gj)
    by_id = {b.building_id: b for b in stock.buildings}
    assert by_id["way_101"].archetype == "single_family"
    assert by_id["way_101"].year_built == 1958
    assert by_id["way_102"].archetype == "primary_school"  # amenity outranks building=yes
    assert by_id["way_102"].num_floors == 2
    # relation outer ways are stitched; the courtyard is dropped for extrusion
    assert by_id["relation_201"].footprint_area_m2 == pytest.approx(1600, rel=0.02)


def test_bbox_helpers():
    s, w, n, e = osm.bbox_from_point(LAT0, LON0, 500)
    assert osm.bbox_area_km2((s, w, n, e)) == pytest.approx(1.0, rel=0.01)
    assert "way[\"building\"]" in osm.build_query((s, w, n, e))
    with pytest.raises(ValueError, match="limit"):
        osm.validate_bbox(osm.bbox_from_point(LAT0, LON0, 5000))
    with pytest.raises(ValueError, match="south < north"):
        osm.validate_bbox((n, w, s, e))


# --------------------------------------------------------------------------- #
# StockManager persistence
# --------------------------------------------------------------------------- #

def _manager(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = str(tmp_path / "outputs")
    cfg.paths.workspace_root = str(tmp_path)
    return StockManager(cfg)


def test_stock_manager_load_persist_and_reload(tmp_path):
    src = tmp_path / "blocks.geojson"
    src.write_text(json.dumps(fc(
        feature(rect(0, 0, 20, 20), building="office", height=10),
        feature(rect(40, 0, 3, 3), building="house"),
    )))
    mgr = _manager(tmp_path)
    res = mgr.load_from_file(str(src), stock_id="tucson_demo", default_year=1975)
    assert res["stock_id"] == "tucson_demo"
    assert res["num_buildings"] == 1 and res["num_excluded"] == 1

    fresh = _manager(tmp_path)  # simulates a server restart
    stock = fresh.get("tucson_demo")
    assert len(stock) == 1 and stock.buildings[0].archetype == "small_office"
    assert fresh.get_meta("tucson_demo")["loader_options"] == {"default_year": 1975}
    assert [s["stock_id"] for s in fresh.list_stocks()] == ["tucson_demo"]

    with pytest.raises(ValueError, match="already exists"):
        mgr.load_from_file(str(src), stock_id="tucson_demo")
    with pytest.raises(ValueError, match="Invalid stock_id"):
        mgr.get("../etc")
    with pytest.raises(KeyError):
        mgr.get("nope")


def test_stock_manager_fetch_osm_uses_cache_files(tmp_path, monkeypatch):
    monkeypatch.setattr(osm, "fetch_overpass", lambda query, url=None, timeout_s=None: OVERPASS_RESPONSE)
    mgr = _manager(tmp_path)
    res = mgr.fetch_osm(center_lat=LAT0, center_lon=LON0, radius_m=300, stock_id="osm_demo")
    assert res["num_buildings"] == 3
    d = mgr.stock_dir("osm_demo")
    assert (d / "osm_raw.json").exists() and (d / "source.geojson").exists()
    meta = mgr.get_meta("osm_demo")
    assert meta["osm"]["osm_base_timestamp"] == "2026-10-01T00:00:00Z"
    with pytest.raises(ValueError, match="bbox"):
        mgr.fetch_osm()


# --------------------------------------------------------------------------- #
# Approximate uses and enrichment
# --------------------------------------------------------------------------- #

def test_approximate_use_is_noted():
    stock = stock_from_geojson(fc(feature(rect(0, 0, 30, 20), building="church")))
    b = stock.buildings[0]
    assert b.archetype == "secondary_school"
    assert "archetype" not in b.inferred_fields
    assert any("approximated" in n for n in b.notes)
    assert any("approximated" in k for k in stock.summary()["notes_by_type"])


def _parcel(x, y, w, d, **props):
    return feature(rect(x, y, w, d), **props)


def test_enrich_fills_only_inferred_fields_and_rederives_dependents():
    from ubem_mcp_server.urban.enrich import enrich_stock

    stock = stock_from_geojson(fc(
        feature(rect(0, 0, 15, 10), building="yes", osm_id="a"),                       # all inferred
        feature(rect(40, 0, 20, 20), building="office", start_date=2010, osm_id="b"),  # year from source
        feature(rect(80, 0, 10, 10), building="yes", osm_id="c"),                      # straddles two parcels
        feature(rect(200, 0, 10, 10), building="yes", osm_id="d"),                     # no parcel
    ), default_year=1975)
    parcels = fc(
        _parcel(-5, -5, 25, 20, YR_BUILT=1952, LUC="0131"),
        _parcel(35, -5, 30, 30, YR_BUILT=1990, LUC="0310", HGT_FT=40),
        _parcel(78, -5, 4, 20, YR_BUILT=1930, LUC="0131"),   # covers 20% of c
        _parcel(82, -5, 20, 20, YR_BUILT=1961, LUC="9999"),  # covers 80% of c; unknown code
    )
    enriched, report = enrich_stock(
        stock, parcels,
        field_map={"year_built": "YR_BUILT", "use": "LUC", "height_m": "HGT_FT"},
        use_value_map={"0131": "single_family", "0310": "office"},
        height_unit="ft",
    )
    a, b, c, d = (enriched.get(i) for i in "abcd")

    assert (a.year_built, a.archetype) == (1952, "single_family")
    assert "year_built" not in a.inferred_fields and "archetype" not in a.inferred_fields
    assert {"num_floors", "height_m"} <= set(a.inferred_fields)

    assert b.year_built == 2010  # source value kept without overwrite
    assert b.height_m == pytest.approx(12.19, abs=0.01)
    assert b.num_floors == 4  # re-derived from the joined height
    assert "num_floors" in b.inferred_fields and "height_m" not in b.inferred_fields

    assert c.year_built == 1961  # largest-overlap parcel
    assert c.archetype == "single_family" and "archetype" in c.inferred_fields  # 9999 unrecognized

    assert d.year_built == 1975 and "year_built" in d.inferred_fields

    assert report["matched"] == 3 and report["unmatched"] == 1
    assert report["unrecognized_use_values"] == {"9999": 1}
    assert report["inferred_share_after"]["year_built"] < report["inferred_share_before"]["year_built"]

    over, _ = enrich_stock(stock, parcels, field_map={"year_built": "YR_BUILT"}, overwrite=True)
    assert over.get("b").year_built == 1990


def test_enrich_with_points_and_projected_crs(tmp_path):
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)
    x, y = t.transform(LON0 + 10 / M_PER_DEG_LON, LAT0 + 10 / M_PER_DEG_LAT)
    layer = fc(feature({"type": "Point", "coordinates": [x, y]}, built=1999, levels=2))
    layer["crs"] = {"type": "name", "properties": {"name": "EPSG:32612"}}

    src = tmp_path / "stock.geojson"
    src.write_text(json.dumps(fc(feature(rect(0, 0, 20, 20), building="yes", osm_id="p"))))
    pts = tmp_path / "points.geojson"
    pts.write_text(json.dumps(layer))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(src), stock_id="base")
    res = mgr.enrich("base", str(pts), {"year_built": "built", "num_floors": "levels"}, new_stock_id="enriched")

    assert res["enrichment"]["matched"] == 1
    b = mgr.get("enriched").get("p")
    assert (b.year_built, b.num_floors) == (1999, 2)
    assert b.height_m == pytest.approx(6.0)
    assert mgr.get_meta("enriched")["derived_from"] == "base"
    assert mgr.get("base").get("p").year_built == 1980  # original untouched

    with pytest.raises(ValueError, match="Unknown target"):
        mgr.enrich("base", str(pts), {"color": "x"})
