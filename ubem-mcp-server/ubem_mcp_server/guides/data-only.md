---
title: Work with building stock data without running a model
use_case: U3
description: Load an inventory as the data describe it, join parcel data, check what is known, summarize by any attribute and export with licences.
---

# Work with building stock data without running a model

Use this when the user wants to characterise a building stock, check data quality, combine footprints with parcels, or prepare a dataset, with no simulation.

## 1. Load as data

- `load_building_stock(geojson_path="inventory.geojson", stock_id="inventory", mode="data", license="ODbL-1.0")`
- or `fetch_osm_buildings(center_lat=32.2335, center_lon=-110.9465, radius_m=500, stock_id="inventory", mode="data")`

Data mode keeps every structure (sheds and garages included), the full footprint geometry and the raw use value, and leaves unknown values empty instead of guessing them. Columns about people (owner names, mailing addresses) are dropped; tell the user which ones. Use `keep_personal_fields=true` only if the user needs them and may hold them.

## 2. Join other layers

`enrich_building_stock(stock_id="inventory", layer_path="parcels.geojson", field_map={"year_built": "YEAR_BUILT", "use": "LAND_USE"}, new_stock_id="inventory_parcels", layer_license="public domain")` fills unknown values. Check the returned `warnings`: combining ODbL footprints with data under other terms can restrict how the result may be shared.

## 3. Ask questions of the data

`query_stock` filters, groups and summarizes; it never returns the whole table.

- What is known: `query_stock(stock_id="inventory")` and read `overall.known_share`.
- By use: `query_stock(stock_id="inventory", group_by=["use"])`
- By age: `query_stock(stock_id="inventory", group_by=["decade"], filters={"use": "house"})`
- Large buildings: `query_stock(stock_id="inventory", filters={"footprint_area_m2": {"min": 1000}}, sample=10)`
- Any source column works, such as a land-use code: `query_stock(stock_id="inventory", group_by=["LAND_USE"], metrics=["count", "sum:footprint_area_m2"])`

Report counts together with how complete the data are; a sum of floor area over buildings whose floor count is unknown is a lower bound.

## 4. Export

`export_stock(stock_id="inventory", format="csv", columns=["LAND_USE"])` writes the table and a metadata file with its sources, licences and attribution. Use `format="geojson"` for GIS. Give the user the attribution line to keep with the data.

## 5. Modeling later

When the user wants simulations after all, `assign_archetypes(stock_id="inventory", new_stock_id="inventory_model", default_year=1960)` derives a modeling stock, then follow the guide `stock-to-district` from step 3.

## 6. National reference data (ComStock and ResStock)

When the user has no inventory, or wants context for one, NREL's ComStock (commercial) and ResStock (residential) samples describe every US county:

1. `fetch_comstock_resstock(dataset="resstock", state="AZ", county="04019")` and `fetch_comstock_resstock(dataset="comstock", state="AZ", county="04019")`. The county is a 5-digit FIPS code (ComStock requires it).
2. `query_comstock_resstock(dataset_id="resstock_az_g0400190_upgrade0", group_by=["building_type", "vintage"])` for weighted building counts, floor area, energy and EUI. Any characteristic works as a group, e.g. `group_by=["in.heating_fuel"]`.
3. What an upgrade would save: fetch it (`upgrade=4`; the list is in `upgrades_available`), then `query_comstock_resstock(dataset_id="resstock_az_g0400190_upgrade4", baseline_dataset_id="resstock_az_g0400190_upgrade0", group_by=["building_type"])`.
4. To check modeled results against them: `benchmark_with_comstock_resstock(stock_id="campus", model_set_id="baseline", dataset_ids=["comstock_az_g0400190_upgrade0", "resstock_az_g0400190_upgrade0"])`.

Each sample stands for many real buildings (`weight`). Always give the number of samples with a weighted figure, and say when a group is flagged `few_samples`. Both datasets are models, not measurements. Credit them with the returned attribution (CC BY 4.0).
