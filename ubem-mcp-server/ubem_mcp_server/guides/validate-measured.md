---
title: Check a modeled district against measured energy use
use_case: U4
description: Load utility bills, benchmarking disclosures or meter data, match them to buildings and compare with simulated results using ASHRAE Guideline 14 statistics.
---

# Check a modeled district against measured energy use

Use this when the user has measured energy use for some buildings (utility bills, a city's benchmarking disclosure such as NYC Local Law 84 or Seattle's building benchmarking, campus meters) and wants to know how well the model matches.

## 1. A simulated model set

Follow the guide `stock-to-district` until `aggregate_stock_results` has run. Measured data only help for buildings in the model set, so include them when you select buildings.

## 2. Load the measured data

Look at the table's columns first, then map them:

`load_measured_energy(stock_id="campus", csv_path="benchmarking_2023.csv", year=2023, id_column="Property Id", match_field="PROPERTY_ID", columns={"electricity": "Electricity Use - Grid Purchase (kWh)", "natural_gas": "Natural Gas Use (therms)", "floor_area": "Property GFA - Self-Reported (ft²)"}, license="public domain")`

- Units are read from the column names ("(kWh)", "(therms)", "(kBtu)", "(ft²)"); otherwise pass `units={"electricity": "MWh"}`.
- Match by an identifier the stock also has (`match_field`, such as a parcel or property id joined with `enrich_building_stock`), or by location with `lat_column` and `lon_column`. Both can be given; location is tried when the identifier does not match.
- Monthly data: add `month_column="Month"`; each row is then one month of one meter.
- Read `match` in the answer and tell the user how many rows matched, and which did not (`unmatched_examples`). A meter that covers several buildings (a campus meter, one parcel with several footprints) is compared with the sum of those buildings.
- Only the mapped columns are kept; names and addresses in the table are not stored.

## 3. Compare

`compare_to_measured(stock_id="campus", model_set_id="baseline", measured_id="measured_2023")`

Report, in this order:

1. District totals by fuel (`district`): measured, modeled and percent difference.
2. How far individual buildings are off (`percent_error_spread`: median absolute percent error, share within 10, 25 and 50%), and `across_meters_guideline14` (NMBE and CV(RMSE) across meters; a positive NMBE means the model is below the measurements).
3. Which archetypes are furthest off (`by_archetype`) and the `largest_differences`, as candidates for better data or the user's own models (`set_building_models`, guide `own-models`).
4. Every item in `caveats`: weather year, floor area mismatch, meters not compared.

For monthly data, also give `monthly_guideline14`. Its thresholds (NMBE within 5%, CV(RMSE) within 15%) are written for calibrating single buildings; archetype models are not calibrated, so present them as a reference, not as pass or fail.

The per-meter table is in `files.meters_csv` for plots or further analysis.
