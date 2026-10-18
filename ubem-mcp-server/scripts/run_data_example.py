"""
Data-only example (paper Section 4.2, use case U3): characterise a district's
building stock without running a model.

Steps, all through the UBEM-MCP tools (here called from the Python library):
  1. fetch OpenStreetMap footprints in data mode,
  2. join Pima County assessor parcels (land-use codes only; owner and mailing
     fields are never requested),
  3. report what is known, by use, before and after the join,
  4. set the district against NREL ComStock/ResStock for Pima County,
  5. export the inventory with its sources, licences and attribution.

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/run_data_example.py --out outputs/examples/data_only
"""

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ubem_mcp_server.api import UBEM
from ubem_mcp_server.urban import osm

CENTER = (32.2335, -110.9465)  # University of Arizona main campus, Tucson
RADIUS_M = 1000

PARCELS_URL = "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/12/query"
PARCEL_FIELDS = "PARCEL,PARCEL_USE"  # never the MAIL*/owner columns
PARCEL_LICENSE = "Pima County GIS data, provided subject to the Pima County GIS disclaimer"
PARCEL_ATTRIBUTION = "Parcels: Pima County Information Technology Department - GIS"

# Arizona Department of Revenue property use codes (Property Use Code Manual):
# the first two digits give the type of property, the third a subcategory.
# Each class maps to a use word UBEM-MCP recognizes; None leaves use unknown
# (vacant land, or improvements such as garages that are not a building type).
USE_CLASS_BY_PREFIX3 = {
    "019": None,                 # miscellaneous residential improvements
    "032": "residential",        # duplex
    "033": "residential",        # triplex
    "211": "hospital",
    "272": "community_centre",   # club, lodge or fraternal organization
    "291": "school",             # private day care and preschool
    "941": "residential", "942": "commercial", "949": "school",              # federal
    "951": "residential", "952": "commercial", "958": "government",          # state
    "959": "university",         # state-owned school; here the University of Arizona
    "961": "residential", "962": "commercial", "967": "fire_station", "969": "school",  # county
    "971": "residential", "972": "commercial", "977": "fire_station", "978": "government",
    "979": "school",             # municipal
    "927": "place_of_worship", "925": "office", "928": "house", "929": "school",  # religious
}
USE_CLASS_BY_PREFIX2 = {
    "00": None,                  # vacant land
    "01": "house",               # single-family residence
    "03": "apartments",          # multiple residential
    "07": "residential",         # condominiums and townhouses
    "10": "commercial", "11": "retail", "12": "retail", "15": "office", "16": "bank",
    "20": "restaurant", "21": "clinic", "26": "parking", "27": "community_centre", "29": "school",
    "88": None,                  # limited-use sites
    "90": "clinic", "91": "community_centre", "92": "place_of_worship",
}


def use_class(code):
    code = (code or "").strip()
    if len(code) < 3:
        return None
    if code[:3] in USE_CLASS_BY_PREFIX3:
        return USE_CLASS_BY_PREFIX3[code[:3]]
    if code[:2] in ("94", "95", "96", "97") and code[2] == "0":
        return None              # government-owned vacant land
    return USE_CLASS_BY_PREFIX2.get(code[:2])


def fetch_parcels(bbox, path: Path):
    """Download parcels intersecting bbox as GeoJSON (2,000 per page)."""
    s, w, n, e = bbox
    feats, offset = [], 0
    while True:
        q = {"geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
             "spatialRel": "esriSpatialRelIntersects", "outFields": PARCEL_FIELDS, "outSR": 4326,
             "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": 2000, "f": "geojson"}
        with urllib.request.urlopen(PARCELS_URL + "?" + urllib.parse.urlencode(q), timeout=120) as r:  # noqa: S310
            page = json.load(r)
        feats += page.get("features") or []
        if not page.get("properties", {}).get("exceededTransferLimit") and len(page.get("features") or []) < 2000:
            break
        offset += 2000
    for f in feats:
        p = f["properties"]
        p["USE_CLASS"] = use_class(p.get("PARCEL_USE"))
    data = {"type": "FeatureCollection", "features": feats}
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    timings, results = {}, {}

    with UBEM(workspace=str(a.out)) as u:
        t0 = time.perf_counter()
        results["fetch"] = u.fetch_osm_buildings(center_lat=CENTER[0], center_lon=CENTER[1], radius_m=RADIUS_M,
                                                 stock_id="district", mode="data")
        timings["fetch_osm_s"] = time.perf_counter() - t0
        results["before"] = u.query_stock(stock_id="district", group_by=["use"], max_groups=15, sample=0)

        t0 = time.perf_counter()
        bbox = osm.bbox_from_point(CENTER[0], CENTER[1], RADIUS_M)
        parcels = fetch_parcels(bbox, a.out / "parcels.geojson")
        timings["fetch_parcels_s"] = time.perf_counter() - t0
        results["parcels"] = {"features": len(parcels["features"]), "fields": PARCEL_FIELDS.split(",")}
        results["enrich"] = u.enrich_building_stock(
            stock_id="district", layer_path=str(a.out / "parcels.geojson"), field_map={"use": "USE_CLASS"},
            keep_columns=["PARCEL", "PARCEL_USE"], new_stock_id="district_parcels", layer_license=PARCEL_LICENSE, layer_attribution=PARCEL_ATTRIBUTION)
        timings["enrich_s"] = time.perf_counter() - t0 - timings["fetch_parcels_s"]

        metrics = ["count", "sum:footprint_area_m2", "mean:footprint_area_m2", "median:footprint_area_m2"]
        results["after"] = u.query_stock(stock_id="district_parcels", group_by=["use"], max_groups=15, sample=0,
                                         metrics=metrics)
        results["by_parcel_code"] = u.query_stock(stock_id="district_parcels", group_by=["PARCEL_USE"],
                                                  max_groups=15, sample=0)

        t0 = time.perf_counter()
        for ds in ("resstock", "comstock"):
            results[f"fetch_{ds}"] = u.fetch_comstock_resstock(dataset=ds, state="AZ", county="04019")
        timings["fetch_reference_s"] = time.perf_counter() - t0
        res_id, com_id = results["fetch_resstock"]["dataset_id"], results["fetch_comstock"]["dataset_id"]
        results["resstock"] = u.query_comstock_resstock(dataset_id=res_id, group_by=["building_type"])
        results["resstock_sfd_vintage"] = u.query_comstock_resstock(
            dataset_id=res_id, group_by=["vintage"], filters={"building_type": "Single-Family Detached"})
        results["comstock"] = u.query_comstock_resstock(dataset_id=com_id, group_by=["building_type"])

        results["export"] = u.export_stock(stock_id="district_parcels", format="csv",
                                           columns=["PARCEL", "PARCEL_USE"], name="district_inventory")
        results["timings_s"] = timings

    (a.out / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    summary = summarize(results)
    (a.out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


def summarize(r):
    """The numbers the paper reports."""
    before, after = r["before"]["overall"], r["after"]["overall"]
    groups = {g["use"]: g for g in r["after"]["groups"]}
    house = groups.get("house", {})
    sfd = next(g for g in r["resstock"]["groups"] if g["group"]["building_type"] == "Single-Family Detached")
    vint = sorted(r["resstock_sfd_vintage"]["groups"], key=lambda g: -g["buildings_represented"])
    total_sfd = sum(g["buildings_represented"] for g in vint)
    e = r["enrich"]["enrichment"]
    return {
        "structures": r["fetch"]["num_buildings"],
        "osm_personal_columns_dropped": r["fetch"]["personal_fields_removed"],
        "parcels": r["parcels"],
        "parcel_match": {"matched": e["matched"], "unmatched": e["unmatched"]},
        "use_filled_from_parcels": e["filled"].get("use", 0),
        "use_not_given_to_outbuildings": e["use_not_given_to_unconditioned"],
        "known_share_before": before["known_share"],
        "known_share_after": after["known_share"],
        "floor_area_known_for": before.get("sum:gross_floor_area_m2:known_for", before["count"]),
        "by_use_after": {k: {"count": g["count"], "footprint_m2": g["sum:footprint_area_m2"]}
                         for k, g in groups.items()},
        "house_footprint_m2": {"mean": house.get("mean:footprint_area_m2"),
                               "median": house.get("median:footprint_area_m2")},
        "resstock_pima_sfd": {"samples": sfd["samples"], "dwellings": round(sfd["buildings_represented"]),
                              "mean_floor_area_m2": round(sfd["floor_area_m2"] / sfd["buildings_represented"], 1),
                              "eui_kwh_m2_p25_p50_p75": sfd["eui_kwh_m2_p25_p50_p75"]},
        "resstock_pima_sfd_vintage_share": {g["group"]["vintage"]: round(g["buildings_represented"] / total_sfd, 3)
                                            for g in vint},
        "comstock_pima": {"samples": r["comstock"]["overall"]["samples"],
                          "buildings": round(r["comstock"]["overall"]["buildings_represented"])},
        "warnings": sorted(set(r["enrich"]["warnings"] + r["export"]["warnings"])),
        "attribution": r["export"]["attribution"],
        "export": {"rows": r["export"]["rows"], "columns": r["export"]["columns"]},
        "timings_s": {k: round(v, 1) for k, v in r["timings_s"].items()},
    }


if __name__ == "__main__":
    main()
