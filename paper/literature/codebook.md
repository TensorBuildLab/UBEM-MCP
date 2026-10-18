# Codebook: main contribution of a UBEM paper

Read the title and abstract and give the paper **one** code: the contribution the paper itself presents as its main one, not everything it touches. A paper that builds a model in order to evaluate retrofits is coded by what it reports as the result (usually S); a paper whose result is the modeling method is G.

| Code | Main contribution | Use case |
|---|---|---|
| G | Build or generate UBEM models from GIS and building data, including archetypes, level of detail, speed-ups and workflows | U1 |
| M | Baseline demand estimation and mapping for a city or district | U1 |
| S | Scenario, retrofit, policy or decarbonisation decisions | U2 |
| D | UBEM data work: acquire, enrich or characterise building stock data, data-driven prediction | U3 |
| V | Calibration, validation, uncertainty | U4 |
| R | Supply side: renewables, district energy | other |
| L | Grid flexibility and load profiles | other |
| C | Climate change, microclimate, urban heat island | other |
| P | Urban planning and morphology | other |
| O | Occupant behaviour | other |
| X | Reviews, education, other | other |
| off | Not about urban building energy modeling | excluded |

Second coder: fill the `code` column of the sheet without looking at `manual_codes.csv`, then run `python analyze_sample.py manual_codes.csv <your sheet>` for agreement and Cohen's kappa.
