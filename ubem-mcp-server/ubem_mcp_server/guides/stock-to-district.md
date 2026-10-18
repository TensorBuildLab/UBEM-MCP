---
title: Model a district from building footprints
use_case: U1
description: Footprints and building data to archetype EnergyPlus models, a background batch run and district energy results and maps.
---

# Model a district from building footprints

Use this when the user wants energy use, end uses, peaks or an EUI map for a neighbourhood, campus or city district.

## 1. Get the building stock

- From OpenStreetMap: `fetch_osm_buildings(center_lat=32.2335, center_lon=-110.9465, radius_m=300, stock_id="district", default_year=1960)`. Areas are limited to 25 km² per request.
- From the user's own GeoJSON: `load_building_stock(geojson_path="footprints.geojson", stock_id="district", license="CC-BY-4.0", attribution="City open data")`. Name columns (`height_field=`, `use_field=`, `year_field=`) when auto-detection misses them, and `source_crs=` when the file is not WGS84.

Read the result before going on, and tell the user:
- `inferred_share`: the share of buildings whose year, floors or height were guessed. Open data rarely carry year built, so this is often 100% for `year_built`.
- `excluded_by_reason`: sheds, garages and tiny footprints left out.
- `warnings`: unknown licences, personal columns that were dropped.

## 2. Fill gaps (optional, recommended)

If the user has an assessor or parcel layer: `enrich_building_stock(stock_id="district", layer_path="parcels.geojson", field_map={"year_built": "YEAR_BUILT", "use": "LAND_USE"}, new_stock_id="district_enriched", layer_license="public domain")`. Only guessed values are filled unless `overwrite=true`. Compare `inferred_share` before and after.

## 3. Weather and climate zone

`get_site_weather(stock_id="district")` picks the nearest station's typical-year weather and the ASHRAE climate zone. Report the station and its distance; a warning appears beyond 50 km. Skip this step if the user gives a weather file.

## 4. Simulate

`simulate_building_stock(stock_id="district", max_workers=4)` generates one model per building and starts a background batch; it returns at once with a `batch_id`. For a quick check first, add `max_buildings=10`. Use `hvac="ideal"` only when the user wants loads rather than equipment energy.

Then call `get_batch_status(batch_id="...", wait_seconds=50)`, which waits up to 50 seconds for the batch, and repeat until `is_complete` is true. Do not poll in a tight loop, and do not write your own simulation scripts: the batch handles parallel runs, failures and restarts.

## 5. Aggregate and report

`aggregate_stock_results(batch_id="...")` writes `buildings.csv`, `results.geojson`, `district_hourly.csv`, `summary.json`, `map.html` and `ATTRIBUTION.txt`, and returns a summary. Report:
- coverage: buildings with and without results, and why any failed;
- district site energy, EUI and the split by fuel and end use;
- the peak demand and when it occurs;
- the share of inputs that were guessed, which bounds how far the numbers can be trusted;
- the attribution line, for any map or report the user makes.

For a single building's failure, `get_batch_results(batch_id="...", status="failed")` lists the error details.
