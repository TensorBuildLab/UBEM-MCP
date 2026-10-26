# Paper materials

Everything behind the numbers, tables and figures of the SoftwareX paper on UBEM-MCP v1.0.0, "UBEM-MCP: Urban building energy modeling data and simulation for AI assistants". All examples run through the UBEM-MCP Python library, which calls the same code as the MCP tools. Scripts live in `ubem-mcp-server/scripts/`; run them inside the dev container from `ubem-mcp-server/` (see the repository README).

## Figures

```bash
python paper/figures/make_figures.py      # needs matplotlib; writes paper/figures/fig*.png and fig*.pdf
```

The script reads only the files in `results/`. Fig. 3 draws footprints from `ubem-mcp-server/agent_tests/fixtures/campus.geojson` (© OpenStreetMap contributors, ODbL).

## Where each result comes from

| Paper | What | Script and command | Result files in `results/` |
|---|---|---|---|
| Table 1 | Use cases in the UBEM literature | `literature/analyze_sample.py` (see below) | `literature/manual_codes.csv` |
| Table 2, Fig. 1 | Tool families | the server's tool list | `tool_counts.json` |
| Sec. 2.5, Fig. 2 | Archetypes against DOE reference results: Tucson (2B), San Francisco (3C), Baltimore (4A) | `validate_archetypes.py --epw <file> --climate-zone 2B --out <dir> --workers 4` | `archetypes_2B_tucson.json`, `archetypes_3C_san_francisco.json`, `archetypes_4A_baltimore.json` |
| Sec. 2.5 | Batch throughput: 1, 2 and 4 workers on a random 24-building subset of a 1,961-building neighbourhood | `benchmark_scaling.py --geojson <neighbourhood> --weather-file <Tucson TMYx> --climate-zone 2B --sizes 24 --workers 1 2 4` | `batch_scaling_workers.json` |
| Sec. 3.1, Table 4, Fig. 3 | Campus electrification (U1, U2) | `run_case_study.py --geojson agent_tests/fixtures/campus.geojson --default-year 1960 --out <dir> --workers 4` | `campus_electrification.json`, `campus_buildings_comparison.csv`, `campus_district_hourly_baseline.csv`, `campus_district_hourly_electrified.csv` |
| Sec. 3.1 | Reproduction by an AI assistant (task `S41_campus`) and short agent tasks | `agent_tests/harness/claude_code.py S41_campus --timeout 10800` (see `agent_tests/README.md`) | `agent_runs.json` |
| Sec. 3.1, 3.3 | Models regenerated from project records and compared with the simulated ones | `verify_examples.py` | `verify_examples.json` |
| Sec. 3.2 | Data-only inventory (U3): OpenStreetMap, Pima County parcels, ComStock/ResStock | `run_data_example.py` | `data_only_inventory.json` |
| Sec. 3.3, Table 5, Fig. 4 | Seattle benchmarking comparison (U4), with step 6 (heating fuel from the meters) | `run_measured_example.py --out <dir> --workers 4`, then `--compare-only --fuel-step` | `seattle_measured.json`, `seattle_meters_archetype_fuel.csv`, `seattle_meters_fuel_from_data.csv` |

Data sources and licences: OpenStreetMap (ODbL 1.0), Climate.OneBuilding.Org weather (downloaded at run time, not redistributed), Pima County parcels (requested fields: parcel number and land-use code only), City of Seattle building energy benchmarking (public domain), NREL ComStock/ResStock (CC BY 4.0), DOE Commercial Reference Buildings and residential prototypes (CC BY 4.0).

OpenStreetMap and the county and city data change over time; the data-only and Seattle examples record the fetch date (17 October 2026) and keep the raw responses with their outputs, so a new fetch can give slightly different counts.

## Literature evidence for the use cases (Table 1)

`literature/` reproduces the corpus analysis behind Table 1.

| File | What it is |
|---|---|
| `fetch_openalex.py` | Searches OpenAlex titles and abstracts with 13 UBEM queries and writes `works.jsonl` (1,005 records on 2026-10-17). Set `OPENALEX_MAILTO` to use OpenAlex's polite pool. |
| `classify.py` | Keyword rules that tag each on-topic paper with an abstract (600 papers) with one or more categories; writes `coded.json` and `coded.csv`. |
| `records.csv` | Identifier, DOI, year, type, citations, venue and title of every record. Abstracts are left out because they may be under publishers' copyright; fetch them again with `fetch_openalex.py`. |
| `keyword_codes.csv` | The keyword categories of the 600 papers. |
| `sample_ids.json`, `manual_codes.csv` | The random sample of 120 papers and the main contribution one author coded for each (118 on topic). |
| `codebook.md` | The codes and how to apply them. |
| `analyze_sample.py` | Shares with Wilson 95% intervals; with a second coder's sheet, agreement and Cohen's kappa. |
| `make_tables.py` | Builds the committed tables from a fetch, and a blind sheet for a second coder. |

```bash
cd paper/literature
python analyze_sample.py                      # Table 1, column 2
python fetch_openalex.py works.jsonl          # a new fetch (results change as OpenAlex grows)
python make_tables.py --second-coder works.jsonl 30 sheet.csv
```

OpenAlex metadata are CC0.
