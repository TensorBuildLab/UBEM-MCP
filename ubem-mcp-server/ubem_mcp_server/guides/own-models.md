---
title: Run your own EnergyPlus models as a batch
use_case: U1
description: Simulate a set of existing EnergyPlus models, from any tool or consultant, as one background batch and collect the results.
---

# Run your own EnergyPlus models as a batch

Use this when the user already has EnergyPlus models (IDF files), for example from OpenStudio, a consultant or another UBEM tool, and wants to run many of them.

1. Find the files: `discover_idf_files(directory="models/", pattern="*.idf")`.
2. Start the batch: `run_batch_simulation(models=["models/a.idf", "models/b.idf"], weather_file="site.epw", max_workers=4)`. Each item can also be an object with its own `idf_path`, `weather_file` and `label`. The call returns at once with a `batch_id`.
3. Wait with `get_batch_status(batch_id="...", wait_seconds=50)`, repeating until `is_complete` is true.
4. Collect results page by page: `get_batch_results(batch_id="...", limit=50)`, and the failures with `get_batch_results(batch_id="...", status="failed")`.

Models must be complete IDF files that EnergyPlus can run; HVACTemplate objects are expanded automatically. Use the single-model tools (`load_idf_model(idf_path="models/a.idf")`, `validate_idf(idf_path="models/a.idf")`) to check one model before running hundreds.

Files are exchanged by path, so when UBEM-MCP runs in a container, the models must be in a folder the container can see.

## Mix your models into a modeled district

When the user has detailed models for some buildings and wants archetype models for the rest (for example a campus with a few audited buildings):

1. Build and generate the district as in the guide `stock-to-district`, without simulating: `generate_building_models(stock_id="campus", model_set_id="baseline")`.
2. Swap in the user's models: `set_building_models(stock_id="campus", model_set_id="baseline", building_models={"way_123": "models/library.idf"}, license="proprietary")`. This makes a new model set (`baseline__own`) and leaves `baseline` as it was. The user's files are copied unchanged except for the outputs that district results need; tell the user which were added (`supplied[].outputs_added`) and pass on any EnergyPlus version warning.
3. Simulate it: `simulate_model_set(stock_id="campus", model_set_id="baseline__own")`, poll `get_batch_status`, then `aggregate_stock_results(batch_id="...")`. Results report each supplied model's own floor area and energy use; its `hvac_system` reads "supplied model".
4. Retrofit scenarios built on `baseline__own` apply measures to archetype models only and leave the user's models unchanged; say so when reporting savings.
