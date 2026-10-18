# Paper materials

Material behind the SoftwareX manuscript on UBEM-MCP. The manuscript itself is drafted separately and will be added here on submission.

## Literature evidence for the use cases (Section 1.4)

`literature/` reproduces the corpus analysis behind Table 2.

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
python analyze_sample.py                      # Table 2, column 2
python fetch_openalex.py works.jsonl          # a new fetch (results change as OpenAlex grows)
python make_tables.py --second-coder works.jsonl 30 sheet.csv
```

OpenAlex metadata are CC0.

## Worked examples (Section 4)

Each example is a script in `ubem-mcp-server/scripts/` that calls the UBEM-MCP library, which runs the same code as the MCP tools. Their summaries are copied to `results/`.

| Section | Script | Data |
|---|---|---|
| 4.1 Campus electrification (U1, U2) | `run_case_study.py` | OpenStreetMap (ODbL), Climate.OneBuilding.Org weather |
| 4.2 Data-only workflow (U3) | `run_data_example.py` | OpenStreetMap, Pima County parcels, NREL ComStock/ResStock (CC BY 4.0) |
| 4.3 Comparison with measured energy use (U4) | `run_measured_example.py` | OpenStreetMap, City of Seattle building energy benchmarking (public domain) |
| 4.5 Reproduction by an agent | `agent_tests/` task `S41_campus` | as 4.1 |
