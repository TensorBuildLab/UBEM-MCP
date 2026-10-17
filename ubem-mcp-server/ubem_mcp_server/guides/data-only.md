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
