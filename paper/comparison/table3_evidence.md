# Table 3 evidence: EnergyPlus-MCP and OpenStudio-MCP

This file lists the code-level evidence behind each cell of Table 3 (nine design
principles) for the two other building-energy MCP servers in the comparison. It
also flags cells the code does not fully support. Checked on 2026-10-25 by
reading the source only. Neither server's code was run.

- **EnergyPlus-MCP** v0.1.0 (`energyplus-mcp-server/pyproject.toml:7`). Commit
  `5a7d3bb1d2e537ba329d3412c8b79d22cedd7c70` (2026-05-07). The repository has no
  release tags. Paths below are relative to `energyplus-mcp-server/energyplus_mcp_server/`
  unless stated otherwise.
- **OpenStudio-MCP** v1.2.1, tag `v1.2.1`, commit
  `8e2673ddf0886b5ffcc27671f7e6c09e8ef894c3`
  (https://github.com/NatLabRockies/openstudio-mcp). For the P6 history check,
  tag `v0.9.0` was also read (commit `8ae7c7aa64c86213fd4ba80be049528c75a48020`).
  Paths are relative to the repository root.

Line numbers refer to those commits.

## EnergyPlus-MCP (v0.1.0)

| Principle | Rating in Table 3 | Evidence | Verdict |
|---|---|---|---|
| P1 Named objects, not files | No: file paths | Every model tool takes a file path, e.g. `server.py:124` `load_idf_model(idf_path: str)`. Modify tools take `output_path` (`server.py:250`). If that is omitted, a `<stem>_modified<suffix>` file is written next to the input (`energyplus_tools.py:864`), so repeated edits produce chains of files. | Supported |
| P2 Provenance, licences, privacy | No | Tool results carry no source, licence or privacy fields. The simulation result records the input IDF path, weather path, options and a timestamp (`energyplus_tools.py:2707-2716`), but individual values carry nothing about where they came from. | Supported |
| P3 Data first, models optional | No | All 35 tools work on IDF files or server state (README line 3: "provides **35 comprehensive tools** for working with EnergyPlus"). No tool works on building data without a model. | Supported |
| P4 Long jobs in the background | No: blocking run | The async tool `run_energyplus_simulation` (`server.py:1199`) makes a synchronous call to `ep_manager.run_simulation` (`server.py:1228`), which calls `idf.run(...)` (`energyplus_tools.py:2700`) and returns only once the run finishes. `simulation_timeout` / `tool_timeout` are defined in `config.py:61-62`. Their only other use is to be echoed in the configuration report (`energyplus_tools.py:448-449`), so they are never enforced. | Supported |
| P5 Answers sized for the assistant | No limits | No size cap, pagination or truncation was found (no `max_results`/truncation logic in `server.py` or `energyplus_tools.py`). Responses mix JSON strings with plain-text prefixes and errors, e.g. `"EnergyPlus simulation completed:\n{result}"` and `"File not found: ..."` (`server.py:1237-1240`). There are no response-size tests in `tests/`. | Supported |
| P6 Expertise the assistant can look up | Docstrings | 35 `@mcp.tool()` registrations in `server.py`, each with a docstring. There are no `@mcp.resource` or `@mcp.prompt` registrations. | Supported |
| P7 Open at every layer | One model | Users can point tools at their own IDF and EPW files: `utils/path_utils.py:76` `resolve_path` accepts absolute or relative paths. Only the EnergyPlus IDF model format is handled; no epJSON, OSM or gbXML code was found. | Supported if "One model" means one model format. Clearer wording: "Own IDF files only" |
| P8 Reproducible | No | Simulations write to a timestamped output directory and return their paths, options and timestamp (`energyplus_tools.py:2707-2716`). Inputs are not copied, software versions are not stored with the run (they appear only in `get_server_status`, `server.py:1069`), lineage across `_modified` files is not recorded, and there is no rerun tool. | Partly. "No" is defensible; "Minimal: run metadata only" would be more precise |
| P9 Tested agent behaviour | No | `tests/` contains `test_auth.py`, `test_config_transport.py`, `test_energyplus_manager.py` and `test_servery.py`, all unit or integration tests. No LLM-in-the-loop tests. | Supported |

## OpenStudio-MCP (v1.2.1)

Tool count: 197 `@mcp.tool(...)` registrations with distinct names under
`mcp_server/`, all registered unconditionally except `list_skills`, `get_skill`
and `recommend_tools`, which are dropped when `OSMCP_DISABLE_KNOWLEDGE_SKILLS=1`
(`mcp_server/skills/__init__.py`). The README says "**150+ tools**" (`README.md:7`).
The server instructions say "150+ tools" (`mcp_server/server.py:61`). "More than 150"
is supported.

| Principle | Rating in Table 3 | Evidence | Verdict |
|---|---|---|---|
| P1 Named objects, not files | Partial: run IDs, one model per session | `mcp_server/model_manager.py:3` "Each MCP session (one connection) gets its own loaded OpenStudio model". `load_model` replaces it (`model_manager.py:102-105`). `save_osm_model(osm_path=None)` saves to the loaded path (`mcp_server/skills/model_management/operations.py:197-203`; `model_manager.py:118`). Writes are only allowed in the user's run area, and `/inputs` and other shared roots are read-only (`mcp_server/config.py:363-364`). As a result, a nameless save overwrites a model loaded from `/runs` but is refused for one loaded from `/inputs`. Simulations are referred to by `run_id` (`mcp_server/skills/simulation/operations.py:47-62`). Model edits still go through file paths (`load_osm_model(osm_path)`, `model_management/tools.py:16`). | Supported, but the "writes back over the source file" claim needs a qualifier (see summary) |
| P2 Provenance, licences, privacy | Partial: error summaries | `extract_simulation_errors_op(run_id)` (`mcp_server/skills/results/operations.py:399`). Per-step attribution of runner messages (`mcp_server/skills/measures/runner_messages.py:99-107`). Results return `run_id` and the paths of the SQL and HTML files they came from (`results/operations.py:239-246`). HVAC wiring recipes name their source `.rb` file (`mcp_server/skills/api_reference/wiring_recipes.py:33`). No licence or privacy fields were found. | Supported |
| P3 Data first, models optional | No | Analysis tools call `get_model()`, which fails without a model: "No model loaded. Call load_osm_model first." (`model_manager.py:135`). The tools that need no model create a model (`create_bar_building`, `create_typical_building`, `mcp_server/skills/comstock/tools.py:24,85`) or list files and weather. No tools for building data such as footprints or attributes. | Supported |
| P4 Long jobs in the background | Yes | Simulations are queued (`status: "queued"`, `simulation/operations.py:52,67-71`), started with `subprocess.Popen` (`:391`) and run by a background dispatcher thread (`:499`). `get_run_status` and `cancel_run` let the assistant check on or stop a run (`simulation/tools.py:73,97`). | Supported |
| P5 Answers sized for the assistant | Yes, tested | Runtime: `ResponseSizeGuard` middleware (`mcp_server/server.py:102`; `mcp_server/response_guard.py:97-110`) replaces any response over 1 MB (default `OSMCP_MAX_TOOL_RESPONSE_MB=1`, `response_guard.py:33-35`) with a small `ok`/preview pointer and saves the full response to a file. List tools default to 10 results (`server.py:89`). Tests: `tests/test_response_sizes.py:24` `MAX_RESPONSE_CHARS = 10_000`. That limit is checked for the default responses of 8 paginated list tools (`:28-37,251-268`) and a few detail tools (`:538,573,652`), not every tool. `tests/test_response_guard.py` covers the 1 MB guard. Envelope: `mcp_server/schemas/tool_responses.schema.json` is a coarse schema (only `ok` required, `additionalProperties: true`). `tests/test_contract.py:12-20` validates 3 static examples against it, not live tool responses. | Partly. "Yes" is fair for the size limits; the wording "tests cap every response at 10,000 characters" and "checked against a schema" overstate the tests (see summary) |
| P6 Expertise the assistant can look up | Resources, guides | 4 `@mcp.resource` (e.g. `openstudio://tool-catalog`) and 6 `@mcp.prompt` in `mcp_server/skills/prompts_resources/tools.py:24-277`. `list_skills`/`get_skill` (`mcp_server/skills/skill_discovery/tools.py:11,23`) serve `.claude/skills/*/SKILL.md`, which the Docker image copies to `/skills` (`docker/Dockerfile:89`). `recommend_tools` router (`mcp_server/skills/tool_router/tools.py:8`), `search_api` and `search_wiring_patterns` (`mcp_server/skills/api_reference/tools.py:8,48`). **Guide/tool mismatch:** the `save_osm_model` tool's only parameter is `osm_path` (`mcp_server/skills/model_management/tools.py:29`). At v0.9.0 the served guides call `save_osm_model(save_path=...)`, e.g. `.claude/skills/simulate/SKILL.md:15`, `retrofit/SKILL.md:16,60`, `new-building/SKILL.md:83` and `tool-workflows/SKILL.md:102,114`; v0.9.0 also copied these guides into the image (`docker/Dockerfile:52`). At v1.2.1 the same `save_path=` calls are still there (e.g. `simulate/SKILL.md:15`, `tool-workflows/SKILL.md:134,146`), while `tool-workflows/SKILL.md:85,88` uses `osm_path=`. The CHANGELOG does not mention it. | Supported. The mismatch is confirmed for v0.9.0 and is still present in v1.2.1. Whether the server rejects the extra argument at runtime was not tested |
| P7 Open at every layer | Partial: user measures | Users can create, edit and apply their own measures (`create_measure`, `edit_measure`, `apply_measure`, `list_custom_measures`) and download measures from BCL (`download_measure_from_bcl`). Users can bring their own models: `README.md:105` "`/inputs` — test models + your own models". They can also import their own geometry (`import_gbxml`, `import_floorspacejs`). Everything stays within OpenStudio formats, and there is no user data layer. | Partly. "Partial" holds, but the reason is broader: "Partial: own OSM/gbXML models and measures" |
| P8 Reproducible | Partial: staged inputs | The OSW and seed model are copied into the run directory (`simulation/operations.py:565-571`). `RunRecord` stores IDs, timestamps, paths and the exit code, but no software versions (`:47-62`). No rerun tool. | Supported |
| P9 Tested agent behaviour | Yes | `tests/llm/` holds 11 test modules. `tests/llm/README.md:3` "verify Claude picks the right MCP tools and chains them correctly", via `claude -p` (`tests/llm/runner.py:3`). Tool selection: `test_02_tool_selection.py`. Routing: `test_09_tool_routing.py`. Confusion pairs: `test_10_confusion_pairs.py`. Cross-model sweeps are in `docs/sweeps/`. These tests are opt-in (`LLM_TESTS_ENABLED=1`) and are not part of the CI workflow. | Supported (it is worth noting that they are opt-in and not run in CI) |

## Summary: changes made to Table 3 after this check

- EnergyPlus-MCP P7 reads "Own IDF files only" and P8 reads "Minimal: run metadata only".
- OpenStudio-MCP P5 reads "Yes: envelope, large outputs to files, size tests". The response-size tests cover the default answers of list and detail tools, and the schema test checks static examples, so the paper does not claim that every response is capped or validated.
- OpenStudio-MCP P7 reads "Partial: own OSM/gbXML models and measures".
- OpenStudio-MCP P9 reads "Yes (opt-in, not in CI)".
- The paper gives OpenStudio-MCP's size as "nearly 200 tools" (197 registrations) and does not repeat the claim that a save without a new name overwrites the source file, which holds only for models in the writable run area.
