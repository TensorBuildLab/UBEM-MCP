"""
Measured-data example (paper Section 3.3, use case U4): compare archetype
models with disclosed energy use in Seattle.

Steps, all through the UBEM-MCP tools (called from the Python library):
  1. fetch OpenStreetMap footprints for Lower Queen Anne in data mode,
  2. join Seattle's 2019 building energy benchmarking records (public domain)
     as points: year built, floors and main use fill what OSM lacks, and the
     property id is kept on each building,
  3. assign archetypes and generate models for the benchmarked buildings,
  4. simulate them with typical-year weather,
  5. load the measured energy use and compare, with ASHRAE Guideline 14
     statistics, once as reported and once weather-normalized,
  6. (--fuel-step) set each building's heating fuel from its meters
     (no gas use: electricity), model and simulate again, and compare.

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/run_measured_example.py --out outputs/examples/measured [--workers 4]
    # redo only step 5 on finished simulations:
    uv run python scripts/run_measured_example.py --out outputs/examples/measured --compare-only
    # then step 6:
    uv run python scripts/run_measured_example.py --out outputs/examples/measured --compare-only --fuel-step
"""

import argparse
import csv
from collections import Counter
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

from ubem_mcp_server.api import UBEM

CENTER = (47.626, -122.360)  # Lower Queen Anne, Seattle
RADIUS_M = 700
YEAR = 2019                  # last full year before COVID-19 changed building use

BENCHMARK_URL = "https://data.seattle.gov/resource/teqw-tu6e.json"
BENCHMARK_LICENSE = "Public Domain (City of Seattle open data)"
BENCHMARK_ATTRIBUTION = "City of Seattle, Building Energy Benchmarking Data 2015-Present"
# Only what the comparison needs: no building names or addresses.
FIELDS = ["osebuildingid", "latitude", "longitude", "yearbuilt", "numberoffloors", "numberofbuildings",
          "largestpropertyusetype", "propertygfabuildings", "electricity_kwh", "naturalgas_therms",
          "steamuse_kbtu", "siteenergyuse_kbtu", "siteenergyusewn_kbtu"]

# ENERGY STAR Portfolio Manager property types -> use words UBEM-MCP knows.
# Types with no DOE prototype stay unknown and are not modeled.
ESPM_USE = {
    "Multifamily Housing": "apartments", "Residence Hall/Dormitory": "dormitory",
    "Senior Living Community": "nursing_home", "Office": "office", "Medical Office": "clinic",
    "Hotel": "hotel", "K-12 School": "school", "Retail Store": "retail",
    "Supermarket/Grocery Store": "supermarket", "Restaurant": "restaurant", "Strip Mall": "mall",
    "Worship Facility": "place_of_worship", "Performing Arts": "theatre", "Museum": "museum",
    "Social/Meeting Hall": "community_centre", "Distribution Center": "warehouse",
    "Non-Refrigerated Warehouse": "warehouse", "Self-Storage Facility": "warehouse",
}


def fetch_benchmarking(path_csv: Path, path_points: Path):
    """Download one year of records near the site; write the table and points."""
    q = {"$select": ",".join(FIELDS), "$where": f"datayear='{YEAR}'", "$limit": 10000}
    with urllib.request.urlopen(BENCHMARK_URL + "?" + urllib.parse.urlencode(q), timeout=120) as r:  # noqa: S310
        rows = json.load(r)

    def near(r):
        dy = (float(r["latitude"]) - CENTER[0]) * 111_320
        dx = (float(r["longitude"]) - CENTER[1]) * 111_320 * math.cos(math.radians(CENTER[0]))
        return math.hypot(dx, dy) <= RADIUS_M

    rows = [r for r in rows if r.get("latitude") and near(r)]
    with open(path_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [float(r["longitude"]), float(r["latitude"])]},
              "properties": {**{k: r.get(k) for k in FIELDS},
                             "USE_CLASS": ESPM_USE.get(r.get("largestpropertyusetype"))}} for r in rows]
    path_points.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--compare-only", action="store_true", help="redo step 5 on finished simulations")
    ap.add_argument("--fuel-step", action="store_true", help="also run step 6 (heating fuel from the meters)")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    results = a.out / "results.json"
    timings, r = {}, {}
    if a.compare_only and results.exists():
        r = json.loads(results.read_text(encoding="utf-8"))
        timings = r.get("timings_s", {})

    with UBEM(workspace=str(a.out), max_workers=a.workers) as u:
        if not a.compare_only:
            build_and_simulate(u, a, r, timings)
        elif "selection" not in r:
            with open(a.out / "benchmarking_2019.csv", encoding="utf-8") as f:
                r["records"] = count_records(list(csv.DictReader(f)))
            select(u, r)
        compare(u, a, r, timings)
        if a.fuel_step:
            fuel_step(u, a, r, timings)

    results.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: r.get(k) for k in ("records", "selection", "timings_s")}, indent=1, default=str))


def count_records(rows):
    return {"total": len(rows), "single_building": sum(1 for x in rows if x.get("numberofbuildings") == "1"),
            "with_steam": sum(1 for x in rows if x.get("steamuse_kbtu"))}


def select(u, r, stock_id="lqa_model"):
    """Model only buildings that one single-building benchmarking record describes."""
    exp = u.export_stock(stock_id=stock_id, format="csv",
                         columns=["osebuildingid", "numberofbuildings", "largestpropertyusetype"],
                         name="benchmarked_buildings")
    with open(exp["path"], encoding="utf-8") as f:
        table = list(csv.DictReader(f))
    ids = [t["building_id"] for t in table if t.get("osebuildingid") and t.get("numberofbuildings") == "1"]
    r["selection"] = {"model_stock_buildings": len(table), "with_record": sum(1 for t in table if t.get("osebuildingid")),
                      "single_building_records_modeled": len(ids)}
    return ids


def build_and_simulate(u, a, r, timings):
    """Steps 1-4."""
    t0 = time.perf_counter()
    r["fetch"] = u.fetch_osm_buildings(center_lat=CENTER[0], center_lon=CENTER[1], radius_m=RADIUS_M,
                                       stock_id="lqa", mode="data")
    rows = fetch_benchmarking(a.out / "benchmarking_2019.csv", a.out / "benchmarking_2019.geojson")
    r["records"] = count_records(rows)
    r["enrich"] = u.enrich_building_stock(
        stock_id="lqa", layer_path=str(a.out / "benchmarking_2019.geojson"),
        field_map={"year_built": "yearbuilt", "num_floors": "numberoffloors", "use": "USE_CLASS"},
        overwrite=True, keep_columns=["osebuildingid", "numberofbuildings", "largestpropertyusetype"],
        new_stock_id="lqa_benchmarked", layer_license=BENCHMARK_LICENSE, layer_attribution=BENCHMARK_ATTRIBUTION)
    r["assign"] = u.assign_archetypes(stock_id="lqa_benchmarked", new_stock_id="lqa_model", default_year=1960)
    timings["intake_s"] = time.perf_counter() - t0

    ids = select(u, r)

    t0 = time.perf_counter()
    r["weather"] = u.get_site_weather(stock_id="lqa_model")
    r["models"] = u.generate_building_models(stock_id="lqa_model", building_ids=ids, model_set_id="benchmarked")
    timings["weather_and_models_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    sim = u.simulate_model_set(stock_id="lqa_model", model_set_id="benchmarked")
    r["batch"] = u.wait(sim["batch"]["batch_id"], poll_seconds=10, timeout=4 * 3600)
    timings["simulate_s"] = time.perf_counter() - t0


def load_once(u, a, **kw):
    """load_measured_energy, or None when a rerun already loaded the same data."""
    if (a.out / "outputs" / "stocks" / kw["stock_id"] / "measured" / kw["measured_id"]).exists():
        return None
    return u.load_measured_energy(**kw)


def compare(u, a, r, timings):
    """Step 5."""
    # The measured table keeps only single-building properties, as modeled.
    single = a.out / "benchmarking_2019_single.csv"
    with open(a.out / "benchmarking_2019.csv", encoding="utf-8") as f_in, \
            open(single, "w", newline="", encoding="utf-8") as f_out:
        rd = csv.DictReader(f_in)
        w = csv.DictWriter(f_out, fieldnames=rd.fieldnames)
        w.writeheader()
        w.writerows(x for x in rd if x["numberofbuildings"] == "1")
    common = dict(stock_id="lqa_model", csv_path=str(single), id_column="osebuildingid",
                  match_field="osebuildingid", year=YEAR, license=BENCHMARK_LICENSE,
                  attribution=BENCHMARK_ATTRIBUTION)
    r["load_reported"] = load_once(u, a,
        columns={"electricity": "electricity_kwh", "natural_gas": "naturalgas_therms",
                 "total": "siteenergyuse_kbtu", "floor_area": "propertygfabuildings"},
        units={"floor_area": "ft2"}, measured_id="seattle_2019", **common) or r.get("load_reported")
    r["load_normalized"] = load_once(u, a,
        columns={"total": "siteenergyusewn_kbtu", "floor_area": "propertygfabuildings"},
        units={"floor_area": "ft2"}, measured_id="seattle_2019_weather_normalized", **common) or r.get("load_normalized")
    r["compare_reported"] = u.compare_to_measured(stock_id="lqa_model", model_set_id="benchmarked",
                                                  measured_id="seattle_2019")
    r["compare_normalized"] = u.compare_to_measured(stock_id="lqa_model", model_set_id="benchmarked",
                                                    measured_id="seattle_2019_weather_normalized")
    r["timings_s"] = timings



def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fuel_step(u, a, r, timings):
    """Step 6: a building that used no gas or district steam in the year is
    heated with electricity; one that used gas is heated with gas."""
    pts = json.loads((a.out / "benchmarking_2019.geojson").read_text(encoding="utf-8"))
    counts = Counter()
    for f in pts["features"]:
        p = f["properties"]
        gas, steam = _num(p.get("naturalgas_therms")), _num(p.get("steamuse_kbtu"))
        p["HEATING_FUEL"] = "natural_gas" if gas > 0 else ("electricity" if steam == 0 else None)
        counts[p["HEATING_FUEL"] or "district_steam"] += 1
    layer = a.out / "benchmarking_2019_fuel.geojson"
    layer.write_text(json.dumps(pts), encoding="utf-8")
    r["fuel_records"] = dict(counts)

    t0 = time.perf_counter()
    r["fuel_enrich"] = u.enrich_building_stock(
        stock_id="lqa_model", layer_path=str(layer), field_map={"heating_fuel": "HEATING_FUEL"}, overwrite=True,
        new_stock_id="lqa_model_fuel", layer_license=BENCHMARK_LICENSE, layer_attribution=BENCHMARK_ATTRIBUTION)
    sel = {}
    ids = select(u, sel, stock_id="lqa_model_fuel")
    r["fuel_selection"] = sel["selection"]
    u.get_site_weather(stock_id="lqa_model_fuel")
    r["fuel_models"] = u.generate_building_models(stock_id="lqa_model_fuel", building_ids=ids,
                                                  model_set_id="benchmarked_fuel")
    sim = u.simulate_model_set(stock_id="lqa_model_fuel", model_set_id="benchmarked_fuel")
    r["fuel_batch"] = u.wait(sim["batch"]["batch_id"], poll_seconds=10, timeout=4 * 3600)
    timings["fuel_step_s"] = time.perf_counter() - t0

    common = dict(stock_id="lqa_model_fuel", csv_path=str(a.out / "benchmarking_2019_single.csv"),
                  id_column="osebuildingid", match_field="osebuildingid", year=YEAR, license=BENCHMARK_LICENSE,
                  attribution=BENCHMARK_ATTRIBUTION)
    r["fuel_load"] = load_once(u, a,
        columns={"electricity": "electricity_kwh", "natural_gas": "naturalgas_therms",
                 "total": "siteenergyuse_kbtu", "floor_area": "propertygfabuildings"},
        units={"floor_area": "ft2"}, measured_id="seattle_2019", **common) or r.get("fuel_load")
    r["compare_fuel"] = u.compare_to_measured(stock_id="lqa_model_fuel", model_set_id="benchmarked_fuel",
                                              measured_id="seattle_2019")
    r["timings_s"] = timings


if __name__ == "__main__":
    main()
