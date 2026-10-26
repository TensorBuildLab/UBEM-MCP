---
title: Compare a retrofit or decarbonisation scenario with the baseline
use_case: U2
description: Apply measures such as heat pumps, insulation or lighting to a simulated stock and compare energy, peaks and emissions with the baseline.
---

# Compare a retrofit or decarbonisation scenario with the baseline

Use this for "what if" questions: electrifying heating, insulating old houses, replacing lighting, or a policy package.

## 1. Start from a simulated baseline

The scenario reuses a baseline model set's buildings, weather and climate, so the only differences are the measures. If there is no baseline yet, follow the guide `stock-to-district` up to and including `aggregate_stock_results(batch_id="...")`, and note the baseline `model_set_id` (for example `simulate_building_stock(stock_id="district", model_set_id="baseline")`).

## 2. Choose the measures

`list_retrofit_measures()` lists the measure types and their parameters; the resource `ubem://retrofit-measures` holds the same list. Write the scenario as data, for example:

```json
{"name": "electrify",
 "measures": [{"type": "heat_pump", "heating_cop": 3.3},
              {"type": "heat_pump_water_heater", "cop": 3.0, "archetypes": ["single_family"]},
              {"type": "lighting", "reduction": 0.4}],
 "vintages": ["pre1980"], "adoption_rate": 0.5, "seed": 1}
```

`archetypes` and `vintages` target buildings; `adoption_rate` with `seed` retrofits a reproducible random share of them.

## 3. Simulate and compare

1. `simulate_retrofit_scenario(stock_id="district", baseline_model_set_id="baseline", scenario={...})` starts a background batch and returns `next_steps`.
2. Poll `get_batch_status(batch_id="...")` until `is_complete`, then `aggregate_stock_results(batch_id="...")`.
3. `compare_retrofit_scenario(stock_id="district", scenario_model_set_id="baseline__electrify")`. Pass `emission_factors={"electricity": 0.4, "natural_gas": 0.18}` (kg CO2e per kWh) for emissions.

## 4. Report

- savings by fuel and in total, and by building type;
- how many buildings adopted the measures;
- the electric and gas peaks before and after, and when they occur. Electrifying heat can move the peak from a summer afternoon to a winter morning and raise it, even when total energy falls. Always say so when it happens.
- any retrofitted building whose energy use rose.
