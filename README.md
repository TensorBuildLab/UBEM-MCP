# UBEM-MCP

[![CI](https://github.com/TensorBuildLab/UBEM-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/TensorBuildLab/UBEM-MCP/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/TensorBuildLab/UBEM-MCP)](https://github.com/TensorBuildLab/UBEM-MCP/releases) [![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE) ![Python 3.10-3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue.svg) ![EnergyPlus 26.1](https://img.shields.io/badge/EnergyPlus-26.1-orange.svg)

An open, model-neutral server for **urban building energy modeling (UBEM)** data and simulation, built on EnergyPlus. Through **68 tools** it takes an AI assistant, a script or a person from building footprints and parcel data to a building stock with recorded provenance, archetype or user-supplied EnergyPlus models, background batch runs, district results, retrofit scenarios, comparisons with measured energy use and NREL ComStock/ResStock reference data. It uses only the open Model Context Protocol, so it works with any MCP client and any model, and the same tools are a Python library and a command line.

> **Version**: 1.0.0
> **EnergyPlus Compatibility**: 26.1.0 (default; see [Building against a different EnergyPlus version](#building-against-a-different-energyplus-version))
> **Python**: 3.10+

This project is derived from [EnergyPlus-MCP](https://github.com/LBNL-ETA/EnergyPlus-MCP) (LBNL) — see [NOTICE.md](NOTICE.md) for attribution and the citation for the original work.

<details open>
<summary><h2>📑 Table of Contents</h2></summary>

- [Overview](#overview)
- [What it is for](#what-it-is-for)
- [Three ways to use it](#three-ways-to-use-it)
- [Installation](#installation)
  - [Quickest: the ready-to-run image](#quickest-the-ready-to-run-image)
  - [Using the MCP Server](#using-the-mcp-server)
    - [Claude Desktop](#claude-desktop)
    - [VS Code](#vs-code)
    - [Cursor](#cursor)
  - [Development Setup](#development-setup)
    - [VS Code Dev Container](#vs-code-dev-container)
    - [Docker Setup](#docker-setup)
    - [Local Development](#local-development)
    - [Streamable HTTP Transport (Local Testing)](#streamable-http-transport-local-testing)
    - [Building against a different EnergyPlus version](#building-against-a-different-energyplus-version)
- [Available Tools](#available-tools)
- [Usage Examples](#usage-examples)
- [Urban Building Stock](#urban-building-stock)
- [Site Weather](#site-weather)
- [Archetype Models](#archetype-models)
- [District Results](#district-results)
- [Retrofit Scenarios](#retrofit-scenarios)
- [Batch Simulation](#batch-simulation)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Reproducing the paper](#reproducing-the-paper)
- [Cite this work](#cite-this-work)
- [License](#license)

</details>

## Overview

UBEM studies chain the same steps: gather building data, fill what is missing, assign archetypes, generate and simulate models, and aggregate the results. Most groups rebuild that chain as one-off scripts, and an AI agent asked to do it writes the scripts again each time. UBEM-MCP fixes those decisions in tested, documented tools instead:

- **Named objects, not files**: every step produces a stock, model set, batch, results or scenario that the next tool takes by ID, so a baseline and its variants live side by side.
- **Every value says where it came from**: inferred attributes are marked, assumptions are listed per building, sources and licences travel with the data, and personal columns are dropped on load.
- **Data first, models optional**: inventories can be loaded, joined, queried and exported without running a model.
- **Long jobs in the background**: batches return at once; status reports progress, time left and grouped failure causes.
- **Answers sized for an agent**: one structured envelope on the 33 new tools, summaries instead of raw output, paged lists.
- **Expertise the agent can look up**: server instructions, workflow guides and catalogues ship inside the server for every client.
- **Open at every layer**: your own GIS layers, your own EnergyPlus models, the single-model tools of EnergyPlus-MCP.
- **Reproducible by record**: input hashes, software versions and recipes, exported and rerun with one tool each.
- **Agent behaviour is tested**: one agent task per use case, scored only from the server's outputs and the final answer, with harnesses for Claude Code and for any OpenAI-compatible model API (`src/agent_tests/`).

## What it is for

The four use cases come from the UBEM research literature (eleven published reviews and a coded sample of 118 papers; see [paper/](paper/README.md)), not from the authors' preferences. Each has a workflow guide inside the server and a worked example script.

| Use case | What UBEM-MCP does | Guide | Worked example |
|---|---|---|---|
| **U1** Build and run models from GIS data to estimate and map district demand | footprints or OSM → stock → weather and climate zone → archetype models → batch → district totals, peaks, EUI maps | `stock-to-district` | `scripts/run_case_study.py` (106 campus buildings, Tucson) |
| **U2** Retrofit and decarbonisation scenarios | declarative measure lists applied to a baseline, simulated and compared, including peak shifts | `retrofit-scenario` | `scripts/run_case_study.py` (campus electrification) |
| **U3** Work with building stock data | data mode, parcel joins, queries of what is known, exports with licences, ComStock/ResStock county data | `data-only` | `scripts/run_data_example.py` (2,978 structures near campus, Pima County parcels) |
| **U4** Compare models with measured energy use | bills, meters or benchmarking matched to buildings; ASHRAE Guideline 14 statistics | `validate-measured` | `scripts/run_measured_example.py` (Seattle benchmarking, Lower Queen Anne) |

Your own detailed models can replace archetypes for chosen buildings in any of them (guide `own-models`).

## Three ways to use it

UBEM-MCP is built for agents first, not for agents only. The same tools, with the same arguments and the same results, are available three ways; all three run the same server code.

**1. As an MCP server**, for any MCP client and any model (see [Installation](#installation)).

**2. As a Python library**, for scripts, notebooks and pipelines:

```python
from ubem_mcp_server.api import UBEM

with UBEM(workspace="my_project", max_workers=4) as ubem:
    ubem.load_building_stock(geojson_path="campus.geojson", stock_id="campus")
    run = ubem.simulate_building_stock(stock_id="campus")
    ubem.wait(run["batch"]["batch_id"], progress=print)
    summary = ubem.aggregate_stock_results(batch_id=run["batch"]["batch_id"])
    print(summary["district"]["total_kwh"])
```

Every tool is a method (`ubem.tools()` lists them, `help(ubem.query_stock)` shows the description). A tool that answers `ok: false` raises `UBEMError` with its `error_type`; pass `raise_on_error=False` to get the result instead.

**3. On the command line**, with options built from each tool's arguments:

```bash
ubem-mcp tools                                   # list tools
ubem-mcp help simulate_building_stock            # options of one tool
ubem-mcp guide stock-to-district                 # a workflow guide
ubem-mcp load_building_stock --geojson-path campus.geojson --stock-id campus
ubem-mcp simulate_building_stock --stock-id campus --max-workers 4   # waits, showing progress
ubem-mcp aggregate_stock_results --batch-id <id> --output summary.json
ubem-mcp serve                                   # run the MCP server
```

Results print as JSON; the exit code is 1 when a tool answers `ok: false`. Lists and objects are given as JSON (`--field-map '{"year_built": "YR"}'`) and lists of names also as `a,b,c`. `python -m ubem_mcp_server` works the same way.

## Installation

### Quickest: the ready-to-run image

Each release publishes an image with EnergyPlus 26.1 and UBEM-MCP installed; only Docker is needed. The folder you mount at `/data` is the workspace: put your GeoJSON, CSV or EPW files there, and results appear in `outputs/` inside it.

```bash
docker pull ghcr.io/tensorbuildlab/ubem-mcp:1.0.0
```

Add it to any MCP client, for example Claude Desktop (`claude_desktop_config.json`), VS Code (`.vscode/mcp.json`, key `servers`) or Cursor (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "ubem": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-v", "/path/to/your/project:/data", "ghcr.io/tensorbuildlab/ubem-mcp:1.0.0"]
    }
  }
}
```

The same image runs the command line: `docker run --rm -v "$PWD":/data ghcr.io/tensorbuildlab/ubem-mcp:1.0.0 tools`. The sections below build a development image from source instead.

### Using the MCP Server

**Prerequisites (all clients):**
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS / Windows) or Docker Engine (Linux), running
- `git` on your PATH
- The `ubem-mcp-dev` image built locally (step 1 below — do this once)

Choose the appropriate setup for your AI assistant or IDE:

#### Claude Desktop

1. **Build the Docker image** (one-time setup):
   ```bash
   git clone https://github.com/TensorBuildLab/UBEM-MCP.git
   cd UBEM-MCP
   docker build -t ubem-mcp-dev -f .devcontainer/Dockerfile .devcontainer
   ```

2. **Locate the Claude Desktop config file** for your OS:
   - **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
   - **Linux**: Claude Desktop is not officially supported on Linux. If you use a community build, check its docs for the config path (commonly `~/.config/Claude/claude_desktop_config.json`).

   Create the file if it does not exist, then add:
   ```json
   {
     "mcpServers": {
       "ubem": {
         "command": "docker",
         "args": [
           "run",
           "--rm",
           "-i",
           "-v", "/path/to/UBEM-MCP:/workspace",
           "-w", "/workspace/src",
           "ubem-mcp-dev",
           "uv", "run", "python", "-m", "ubem_mcp_server.server"
         ]
       }
     }
   }
   ```

   **Important**:
   - Replace `/path/to/UBEM-MCP` with the absolute path to your cloned repo.
     - macOS/Linux example: `/Users/yourname/code/UBEM-MCP`
     - Windows example: `C:\\Users\\yourname\\code\\UBEM-MCP` (use double backslashes in JSON)
   - Remove all comments (text after `//`) when adding to the actual config file, as JSON doesn't support comments.

3. **Restart Claude Desktop**. The `ubem` MCP server should appear in the MCP servers panel.

4. **Verify**: in a new chat, ask *"List the UBEM-MCP tools you have access to."* You should see tools like `load_idf_model`, `run_batch_simulation`, `get_server_status`. If not, check [Troubleshooting](#troubleshooting).

#### VS Code

VS Code 1.102+ ships native MCP support. Config goes in `.vscode/mcp.json` at the workspace root (or in user settings under `"mcp"`).

1. **Build the Docker image** (same as Claude Desktop step 1 above).

2. **Create `.vscode/mcp.json`** in your project:
   ```json
   {
     "servers": {
       "ubem": {
         "command": "docker",
         "args": [
           "run",
           "--rm",
           "-i",
           "-v", "${workspaceFolder}:/workspace",
           "-w", "/workspace/src",
           "ubem-mcp-dev",
           "uv", "run", "python", "-m", "ubem_mcp_server.server"
         ]
       }
     }
   }
   ```

   **Important**: Remove all comments (text after `//`) when saving — JSON does not support comments.

3. **Reload VS Code** (`Ctrl/Cmd+Shift+P` → *Developer: Reload Window*). Open the Chat view and confirm the `ubem` MCP server shows as *Running*.

4. **Verify**: ask the chat *"What UBEM-MCP tools are available?"* — you should see the tool list.

#### Cursor

1. **Build the Docker image** (same as Claude Desktop step 1 above).

2. **Locate the Cursor MCP config file** for your OS:
   - **macOS/Linux**: `~/.cursor/mcp.json`
   - **Windows**: `%USERPROFILE%\.cursor\mcp.json`

   Create the file if it does not exist, then add:
   ```json
   {
     "mcpServers": {
       "ubem": {
         "command": "docker",
         "args": [
           "run",
           "--rm",
           "-i",
           "-v", "/path/to/UBEM-MCP:/workspace",
           "-w", "/workspace/src",
           "ubem-mcp-dev",
           "uv", "run", "python", "-m", "ubem_mcp_server.server"
         ]
       }
     }
   }
   ```

   **Important**:
   - Replace `/path/to/UBEM-MCP` with the absolute path to your cloned repo (Windows users: use double backslashes in JSON, e.g. `C:\\Users\\yourname\\code\\UBEM-MCP`).
   - Remove all comments (text after `//`) when saving — JSON does not support comments.

3. **Restart Cursor**. Open *Settings → MCP* and confirm the `ubem` server is listed as connected.

4. **Verify**: ask Cursor chat *"What UBEM-MCP tools are available?"* — you should see the tool list.

### Development Setup

For contributors who want to modify or extend the MCP server:

#### VS Code Dev Container

The easiest development setup with all dependencies pre-configured.

**Prerequisites:**
- [Visual Studio Code](https://code.visualstudio.com/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

**Steps:**
1. Clone and open in VS Code:
   ```bash
   git clone https://github.com/TensorBuildLab/UBEM-MCP.git
   cd UBEM-MCP
   code .
   ```

2. Click "Reopen in Container" when prompted (or press `Ctrl+Shift+P` → "Dev Containers: Reopen in Container")

3. The container automatically installs EnergyPlus 26.1.0 and all dependencies (to pin a different version, see [Building against a different EnergyPlus version](#building-against-a-different-energyplus-version))

#### Docker Setup

For direct Docker development without VS Code:

```bash
# Clone repository
git clone https://github.com/TensorBuildLab/UBEM-MCP.git
cd UBEM-MCP

# Build container
docker build -t ubem-mcp-dev -f .devcontainer/Dockerfile .devcontainer

# Run container
docker run -it --rm -v "$(pwd)":/workspace -w /workspace/src ubem-mcp-dev bash

# Inside container, install dependencies
uv sync --extra dev
```

#### Local Development

For local development (requires EnergyPlus installation):

**Prerequisites:**
- Python 3.10+
- [uv package manager](https://github.com/astral-sh/uv)
- [EnergyPlus 26.1.0](https://github.com/NREL/EnergyPlus/releases/tag/v26.1.0) (or pin a different version — see [Building against a different EnergyPlus version](#building-against-a-different-energyplus-version))

```bash
# Clone and install
git clone https://github.com/TensorBuildLab/UBEM-MCP.git
cd UBEM-MCP/src
uv sync --extra dev

# Run server for testing
uv run python -m ubem_mcp_server.server
```

#### Streamable HTTP Transport (Local Testing)

By default the server runs over **stdio**, which is what every MCP client config in this README uses. The server can also run as a token-authenticated **streamable HTTP** service — useful for testing remote-style deployments, smoke-testing with `curl`, or connecting clients that expect an HTTP MCP endpoint.

**Prerequisites — pick one path:**

- **Docker (recommended; no local EnergyPlus install needed)**: build the `ubem-mcp-dev` image once (per [Docker Setup](#docker-setup) above). The image ships with EnergyPlus 26.1.0 and all Python deps pre-installed.
- **Local Development**: follow [Local Development](#local-development) above — Python 3.10+, `uv`, and a local EnergyPlus install. HTTP mode pulls in `uvicorn` and `python-dotenv`, which are declared in `pyproject.toml` — `uv sync --extra dev` will install them.

**1. Generate a bearer token.** Tokens must be at least 32 characters:

```bash
openssl rand -hex 32
```

**2. Create `.env` in `src/`** (gitignored). Copy from [.env.example](src/.env.example) and fill in your values:

```bash
# EPLUS_IDD_PATH — ONLY set this for the Local variant.
# Leave it commented out / unset when using the Docker variant; the image has
# EnergyPlus 26.1.0 baked in and auto-detects it.
# EPLUS_IDD_PATH=/Applications/EnergyPlus-26-1-0/Energy+.idd

MCP_TRANSPORT=streamable-http
MCP_HTTP_HOST=0.0.0.0
MCP_HTTP_PORT=8000
MCP_HTTP_PATH=/mcp

# JSON array. Required when MCP_TRANSPORT=streamable-http.
MCP_TOKENS=[{"label":"local-dev","token":"<paste-32+-char-hex-here>"}]
```

**3. Start the server.** Pick the variant that matches your setup:

*Docker (recommended):* publish the port, mount the repo, and let the container pick up `.env` via `--env-file`. Run from the **repo root**:

```bash
docker run --rm \
  -p 8000:8000 \
  -v "$(pwd)":/workspace \
  -w /workspace/src \
  --env-file src/.env \
  ubem-mcp-dev \
  uv run python -m ubem_mcp_server.server
```

*Local:* (requires a host EnergyPlus install matching whatever `EPLUS_IDD_PATH` is set to in `.env`)

```bash
cd src
uv run python -m ubem_mcp_server.server
```

Either way you should see a log line like `Listening on http://0.0.0.0:8000 (path=/mcp, 1 tokens)`.

**4. Smoke-test with curl.** The server exposes two endpoints:

```bash
# Health check — no auth required
curl -s http://localhost:8000/health

# Unauthenticated MCP request — should return 401
curl -i -X POST http://localhost:8000/mcp

# Authenticated initialize handshake
TOKEN=<paste-your-token>
curl -i -X POST http://localhost:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

**5. Connect MCP Inspector.** Run the Inspector and point it at `http://localhost:8000/mcp` with transport `Streamable HTTP` and an `Authorization: Bearer <your-token>` header.

**Tests** — transport and auth behavior is covered by `tests/test_config_transport.py` and `tests/test_auth.py`; batch orchestration is covered by `tests/test_batch_manager.py` (mocked worker) and `tests/test_integration_energyplus.py` (real EnergyPlus runs); `tests/test_library_scripts.py` runs the Python library from plain scripts. Run with:
```bash
uv run pytest tests/                    # everything; EnergyPlus tests skip if EnergyPlus isn't installed
uv run pytest tests/ -m "not energyplus" # fast tests only
```
CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the fast tests on Python 3.10–3.12 and the integration tests against EnergyPlus 26.1.0.

#### Building against a different EnergyPlus version

The Docker image bakes EnergyPlus **26.1.0** in by default. To pin a different release, override the build args below when building the image. You'll need:

- `EPLUS_VER`: release version, e.g. `25.1.0`
- `EPLUS_HASH`: the short commit string NREL embeds in the release tarball filename
- `EPLUS_PREFIX`: install path inside the container, e.g. `/app/software/EnergyPlusV25-1-0`
- `EPLUS_DIST_SUFFIX`: `Ubuntu22.04` for EnergyPlus ≤ 25.1.0, `Ubuntu24.04` for ≥ 26.1.0

```bash
docker build \
  --build-arg EPLUS_VER=25.1.0 \
  --build-arg EPLUS_HASH=68a4a7c774 \
  --build-arg EPLUS_PREFIX=/app/software/EnergyPlusV25-1-0 \
  --build-arg EPLUS_DIST_SUFFIX=Ubuntu22.04 \
  -t ubem-mcp-dev \
  -f .devcontainer/Dockerfile .devcontainer
```

**Heads up — three other places reference the install path** (`/app/software/EnergyPlusV26-1-0`) and don't read the Dockerfile ARG, so if you override `EPLUS_PREFIX` you'll also need to update:

- `src/ubem_mcp_server/config.py` — `version` and `default_installation`
- `src/.vscode/mcp.json` — the two `EnergyPlusV26-1-0` strings
- The client config JSON you created for Claude Desktop / VS Code / Cursor — only if it sets `EPLUS_IDD_PATH` explicitly

## Available Tools

The server provides **68 tools** organized into **15 categories**. It uses only the open MCP standard, so it works with any MCP client and any model.

**Responses.** The 33 tools new in UBEM-MCP return one structured object (MCP structured content, with the same JSON as text):

```json
{"ok": true, "warnings": [], "...": "tool data", "next_steps": ["poll get_batch_status(...) until is_complete"]}
{"ok": false, "error_type": "not_found | invalid_input | unavailable | internal", "error": "Unknown stock_id: nope"}
```

Responses are summaries kept under 20,000 characters (lists are paged with `limit`/`offset`, groups are capped), with full detail written to files. Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so a client can tell which tools only read, which discard work, and which reach the internet. The single-model tools carried over from EnergyPlus-MCP keep their original text responses.

**Your models, not only ours.** Archetype models are a starting point. `set_building_models` puts the user's own EnergyPlus models (from OpenStudio, a consultant, a calibration study or another UBEM tool) in place of archetype models for chosen buildings, in a new model set. The files are copied unchanged except for the outputs district results need, and each addition is reported, together with any EnergyPlus version mismatch. Results, scenarios (which leave supplied models unchanged), provenance and project records all treat supplied models like the others.

**National reference data, weighted honestly.** `fetch_comstock_resstock` downloads NREL's ComStock or ResStock annual results for a county from the Open Energy Data Initiative (no extra packages; a compact copy is kept). `query_comstock_resstock` answers weighted questions (building counts, floor area, energy and EUI by any characteristic, and an upgrade's savings over the baseline) and reports the number of samples behind every figure, flagging small groups. `benchmark_with_comstock_resstock` places the modeled EUI of each archetype within the distribution for the same building type and county. The data are CC BY 4.0 and the attribution comes with every answer.

**Checked against measurements.** `load_measured_energy` reads utility bills, benchmarking disclosures (such as NYC Local Law 84 or Seattle's benchmarking data) or meter data, annual or monthly, in any common unit. Rows are matched to buildings by a shared identifier or by location, and a meter that covers several buildings is compared with their sum. Only the mapped columns are kept. `compare_to_measured` reports district totals, the spread of building-level errors, median error by archetype, the largest differences and ASHRAE Guideline 14 NMBE and CV(RMSE), together with caveats such as a weather-year mismatch.

**Failures explained.** Every run's EnergyPlus `.err` file is read for warning, severe and fatal counts and the first messages. `get_batch_status` groups failed buildings by cause, so one broken input across hundreds of buildings shows as one problem, with example buildings and a suggested fix, and reports progress with an estimate of the time left. Each building's duration is its EnergyPlus run time, without time spent waiting in the queue.

**Reproducible by record.** Every step that creates a stock or a model set stores its arguments and the SHA-256 of each input file; every generated model is hashed. `export_project_record` collects these for a stock's whole lineage, together with the weather file, results and software versions (UBEM-MCP, EnergyPlus version and build, the bundled archetype library and Python packages), in `project_record.json`. `rerun_project_record` repeats the steps from the same files under new ids and reports, model by model, whether the regenerated models match; it warns when an input file, the EnergyPlus version or the archetype library has changed.

**Guides for any client.** Step-by-step workflow guides ship inside the server, not in any one client's skill system. Each guide is offered three ways: through the `get_workflow_guide` tool (works in every MCP client), as an MCP prompt, and as an MCP resource under `ubem://guides/`. Current guides: `stock-to-district` (model a district from footprints), `retrofit-scenario` (compare a retrofit or decarbonisation scenario), `data-only` (work with building data without a model), `validate-measured` (check a model against measured energy use) and `own-models` (run your own EnergyPlus models as a batch, or mix them into a modeled district). Reference data are resources too: `ubem://archetypes`, `ubem://retrofit-measures` and `ubem://scenario-schema`, generated from the code. On connection, every client receives short server instructions on how the tools fit together.

### 📘 Guides (1 tool) — new in UBEM-MCP
- `get_workflow_guide` - List the workflow guides, or return one guide's step-by-step text

### 🗺️ Urban Building Stock (9 tools) — new in UBEM-MCP
- `list_archetypes` - List the building archetypes (DOE prototype use types) and vintage bins
- `load_building_stock` - Load building footprints from a GeoJSON file, for modeling or as a data inventory (`mode`)
- `fetch_osm_buildings` - Download building footprints for an area from OpenStreetMap
- `enrich_building_stock` - Fill guessed or unknown attributes (year built, use, floors, height) from a parcel/assessor layer by spatial join
- `get_building_stock` - Summarize a stock and list/filter its buildings
- `query_stock` - Filter, group and summarize buildings by any attribute or source column, with data completeness, without running a model
- `export_stock` - Export a stock to CSV or GeoJSON with a record of its sources, licences and attribution
- `assign_archetypes` - Derive a modeling stock from a data inventory (archetypes, defaults, exclusions)
- `list_building_stocks` - List loaded stocks (persisted across restarts)

### 🌦️ Site Weather (2 tools) — new in UBEM-MCP
- `find_weather_stations` - List the weather stations nearest to a stock or a point
- `get_site_weather` - Download a stock's typical-year weather and set its ASHRAE climate zone

### 🏗️ Building Models (4 tools) — new in UBEM-MCP
- `generate_building_models` - Generate an archetype EnergyPlus model for each building of a stock
- `simulate_building_stock` - Generate models for a stock and simulate them all as one background batch
- `set_building_models` - Use your own EnergyPlus models for some buildings; the rest keep archetype models
- `simulate_model_set` - Simulate an existing model set (with your models, or regenerated from a record)

### 📊 District Results (1 tool) — new in UBEM-MCP
- `aggregate_stock_results` - Aggregate a simulated stock into per-building results, hourly district demand, a GeoJSON results layer and an EUI map

### 🔧 Retrofit Scenarios (3 tools) — new in UBEM-MCP
- `list_retrofit_measures` - List the available retrofit measures and their parameters
- `simulate_retrofit_scenario` - Apply a scenario of measures to a simulated stock and simulate it
- `compare_retrofit_scenario` - Compare a scenario with its baseline: savings by fuel, peaks, emissions, per building

### 🇺🇸 ComStock / ResStock (3 tools) — new in UBEM-MCP
- `fetch_comstock_resstock` - Download NREL ComStock or ResStock annual results for a county or state from OEDI
- `query_comstock_resstock` - Weighted building counts, floor area, energy, EUI and upgrade savings, with sample counts
- `benchmark_with_comstock_resstock` - Place modeled EUI by archetype within ComStock/ResStock for the same area

### 📏 Validation (2 tools) — new in UBEM-MCP
- `load_measured_energy` - Load bills, benchmarking disclosures or meter data and match them to buildings by id or location
- `compare_to_measured` - Compare simulated with measured energy use: district totals, error spread, ASHRAE Guideline 14 statistics

### 🧾 Project Records (2 tools) — new in UBEM-MCP
- `export_project_record` - Write a record of every step, input file hash, model hash, result and software version behind a stock
- `rerun_project_record` - Repeat a recorded analysis from its input files and check that every model is identical

### 🏙️ Batch Simulation (6 tools) — new in UBEM-MCP
- `discover_idf_files` - Glob a directory for IDF files to build a batch's model list
- `run_batch_simulation` - Run many EnergyPlus models concurrently as a background job; returns a `batch_id` immediately
- `get_batch_status` - Poll overall/per-status progress of a batch job
- `get_batch_results` - Get per-model results (success/failure, duration, output paths)
- `list_batches` - List all known batch jobs (in-memory + persisted on disk)
- `cancel_batch` - Cancel a batch job: models not yet started never run, and running EnergyPlus processes are stopped

### 🗂️ Model Config & Loading (9 tools)
- `load_idf_model` - Load and validate IDF files
- `validate_idf` - Comprehensive model validation
- `list_available_files` - Browse sample files and weather data
- `copy_file` - Intelligent file copying with path resolution
- `get_model_summary` - Extract basic model information
- `check_simulation_settings` - Review simulation control settings
- `modify_simulation_control` - Modify simulation parameters
- `modify_run_period` - Adjust simulation time periods
- `get_server_configuration` - Get server configuration info

### 🔍 Model Inspection (9 tools)
- `list_zones` - List all thermal zones with properties
- `get_surfaces` - Get building surface information
- `get_materials` - Extract material definitions
- `inspect_schedules` - Analyze all schedule objects
- `inspect_people` - Analyze occupancy settings
- `inspect_lights` - Analyze lighting loads
- `inspect_electric_equipment` - Analyze equipment loads
- `get_output_variables` - Get/discover output variables
- `get_output_meters` - Get/discover energy meters

### ⚙️ Model Modification (8 tools)
- `modify_people` - Update occupancy settings
- `modify_lights` - Update lighting loads
- `modify_electric_equipment` - Update equipment loads
- `change_infiltration_by_mult` - Modify infiltration rates
- `add_window_film_outside` - Add window films
- `add_coating_outside` - Apply surface coatings
- `add_output_variables` - Add output variables
- `add_output_meters` - Add energy meters

### 🚀 Simulation & Results (4 tools)
- `run_energyplus_simulation` - Execute a single simulation
- `create_interactive_plot` - Generate HTML visualizations
- `discover_hvac_loops` - Find all HVAC loops
- `get_loop_topology` - Get HVAC loop details

### 🖥️ Server Management (5 tools)
- `visualize_loop_diagram` - Generate HVAC diagrams
- `get_server_status` - Check server health
- `get_server_logs` - View recent logs
- `get_error_logs` - Get error logs
- `clear_logs` - Clear/rotate log files

## Usage Examples

### Quick start: a small district

With the server running in an MCP client, ask in plain language, for example: *"Simulate the buildings in agent_tests/fixtures/campus_small.geojson and give me the district's annual energy use and peak demand."* The assistant calls the tools below; from a script, the Python library calls the same tools:

```python
from ubem_mcp_server.api import UBEM

u = UBEM(workspace="my_study")   # a folder holding a copy of agent_tests/fixtures/campus_small.geojson
u.load_building_stock(geojson_path="campus_small.geojson", stock_id="small", default_year=1960)
u.get_site_weather(stock_id="small")                 # nearest weather station and climate zone
run = u.simulate_building_stock(stock_id="small", model_set_id="baseline")
u.wait(run["batch"]["batch_id"], progress=lambda s: print(s["counts"]))
summary = u.aggregate_stock_results(batch_id=run["batch"]["batch_id"])
print(summary["district"]["total_kwh"], summary["district"]["peak_kw"])
```

The command line does the same: `ubem-mcp load_building_stock --geojson-path campus_small.geojson --stock-id small`, then `ubem-mcp simulate_building_stock --stock-id small` (it waits for the batch) and `ubem-mcp aggregate_stock_results --stock-id small --model-set-id <id>`.

### Single-model workflow (inherited from EnergyPlus-MCP)

1. **Load a model**:
   ```json
   {
     "tool": "load_idf_model",
     "arguments": {
       "idf_path": "sample_files/1ZoneUncontrolled.idf"
     }
   }
   ```

2. **Inspect zones**:
   ```json
   {
     "tool": "list_zones",
     "arguments": {
       "idf_path": "sample_files/1ZoneUncontrolled.idf"
     }
   }
   ```

3. **Run a single simulation**:
   ```json
   {
     "tool": "run_energyplus_simulation",
     "arguments": {
       "idf_path": "sample_files/1ZoneUncontrolled.idf",
       "weather_file": "sample_files/USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
       "annual": true
     }
   }
   ```

### Using with MCP Inspector

Test tools interactively (requires Node.js 18+):

```bash
# From the repo root, run the server inside the dev image under the Inspector
npx @modelcontextprotocol/inspector \
  docker run --rm -i \
    -v "$(pwd):/workspace" \
    -w /workspace/src \
    ubem-mcp-dev \
    uv run python -m ubem_mcp_server.server
```

## Urban Building Stock

UBEM starts from the city, not from finished EnergyPlus models. The urban tools turn building footprints into a **building stock**: one record per building with a local metric footprint, an archetype (use type), year built and vintage, floor count and height.

1. **Get footprints** — from OpenStreetMap:
   ```json
   {"tool": "fetch_osm_buildings", "arguments": {"center_lat": 32.2335, "center_lon": -110.9465, "radius_m": 300, "stock_id": "uofa_campus", "default_year": 1960}}
   ```
   or from any GeoJSON (e.g. a city or Microsoft footprint dataset), naming columns if auto-detection misses them:
   ```json
   {"tool": "load_building_stock", "arguments": {"geojson_path": "footprints.geojson", "height_field": "HGT", "height_unit": "ft", "source_crs": "EPSG:2868"}}
   ```

2. **Check data quality** — the summary reports, per attribute, the share of buildings whose value was *inferred* rather than read from the data (e.g. a floor count guessed from the archetype), plus exclusions (sheds, garages, tiny footprints) and approximations (e.g. a church modeled as the closest DOE prototype). Every building lists its own `inferred_fields` and `notes`.

3. **Fill gaps from parcel data** — footprint sources rarely include year built or use. Join an assessor/parcel layer onto the footprints:
   ```json
   {
     "tool": "enrich_building_stock",
     "arguments": {
       "stock_id": "uofa_campus",
       "layer_path": "parcels.geojson",
       "field_map": {"year_built": "YEAR_BUILT", "use": "LAND_USE"},
       "use_value_map": {"0131": "single_family", "0310": "office"},
       "new_stock_id": "uofa_campus_enriched"
     }
   }
   ```
   Each building takes the attributes of the parcel containing its centroid (or covering most of its footprint). Only inferred attributes are filled unless `overwrite` is set, and the result is saved as a new stock so the original stays as a record.

4. **Inspect** with `get_building_stock` (filter by archetype, vintage, or floor area).

5. **Know where the data came from** — every stock records its data sources with their licences and the attribution they require: OpenStreetMap (ODbL), your own files (pass `license` and `attribution` to `load_building_stock`, or `layer_license` and `layer_attribution` to `enrich_building_stock`), the weather file (Climate.OneBuilding.Org) and the archetype library (DOE, CC BY 4.0). Tool results list the sources and warn when a licence is unknown or when ODbL footprints are combined with data whose terms may not allow it. Model sets and results carry the same record, and each results folder gets an `ATTRIBUTION.txt` to copy into reports and maps.

**Working with data only.** Not every study needs a model. Load footprints or an inventory with `"mode": "data"` to keep it as the data describe it: every structure (sheds and garages included), the full footprint geometry (all parts, courtyards), the raw use value, and unknown values left empty rather than guessed. Then:
```json
{"tool": "query_stock", "arguments": {"stock_id": "inventory", "group_by": ["use"], "filters": {"year_built": {"is_null": true}}}}
```
returns counts, floor areas and the share of each attribute that is known, per group, and
```json
{"tool": "export_stock", "arguments": {"stock_id": "inventory", "format": "csv", "columns": ["PARCEL"]}}
```
writes the table with a metadata file of its sources and licences. `query_stock` and `export_stock` work on modeling stocks too. When you do want to simulate, `assign_archetypes` derives a modeling stock from the inventory, and `generate_building_models` refuses a data-mode stock until then.

**Personal data.** Columns about people, such as an owner's name, mailing address, phone or e-mail in assessor data, are dropped when a stock is loaded, and the tool reports which columns were removed. Modeling never needs them. Pass `keep_personal_fields: true` only if you need them and are allowed to hold them.

Stocks are saved under `outputs/stocks/<stock_id>/` as `buildings.geojson` (normalized, WGS84 — open it in QGIS or edit and reload it), `meta.json` (source, loader options, OSM query and data timestamp, enrichment provenance) and `excluded.json`. OpenStreetMap fetches also keep the raw Overpass response, so a study can be reproduced exactly.

Archetypes follow the U.S. DOE commercial prototype buildings and the DOE single-family prototype; vintages follow the DOE reference buildings (pre-1980, 1980–2004, 2004+). Run `list_archetypes` for the full list.

## Site Weather

A stock's location determines its weather file and climate zone, so they do not have to be looked up by hand:

```json
{"tool": "get_site_weather", "arguments": {"stock_id": "uofa_campus"}}
```

- **Station**: the station nearest to the stock's center, from the [Climate.OneBuilding.Org](https://climate.onebuilding.org) TMYx index of about 17,000 stations worldwide. The index is downloaded on first use and cached under `outputs/weather/`. Use `find_weather_stations` to see the alternatives and pass `station` to pick another one.
- **Weather data**: the station's most recent 15-year typical meteorological year (e.g. `TMYx.2009-2023`), with its STAT and DDY files. Pass `dataset` for another period, or `weather_file` to use a local EPW instead.
- **Climate zone**: the ASHRAE 169 zone recorded in the station's .stat file. Without one, the zone is estimated from the EPW with the ASHRAE 169 degree-day and moisture criteria, and the result says so. Zones without a DOE reference location use the closest one, with a note: 0A uses 1A, the hot-dry 0B and 1B use 2B (Phoenix, which is itself 1B under ASHRAE 169-2021), and 5C uses 4C.

The result is saved in the stock's `meta.json`, recorded in every model set's manifest, and comes with a warning when the station is more than 50 km from the site. `generate_building_models` and `simulate_building_stock` resolve it automatically if it has not been set, and an explicit `climate_zone` or `weather_file` always wins.

## Archetype Models

Each building in a stock becomes an EnergyPlus model built from its footprint and its archetype, vintage and climate zone:

- **Geometry**:
  - The footprint is extruded floor by floor. Buildings above three floors are modeled as ground, middle and top floors, with the middle floor multiplied.
  - Commercial floors get core/perimeter zoning (4.57 m perimeter zones, as in the DOE prototypes) when the footprint allows it.
  - Detailed outlines are simplified to at most 30 vertices, never changing the area by more than 5%. An outline that cannot be simplified that far is modeled as an oriented rectangle of the same area, and the manifest says so.
  - Walls shared with an adjacent building are adiabatic party walls. The nearest walls of surrounding buildings are added as shading.
- **Constructions, loads and schedules**:
  - Commercial buildings use the DOE Commercial Reference Buildings: 16 types × 3 vintages × 16 climate zones.
  - Single-family houses use the DOE/PNNL IECC 2006 single-family prototype. See [the data README](src/ubem_mcp_server/urban/data/README.md) for sources and licenses.
- **HVAC**:
  - The reference system type is expressed with EnergyPlus HVACTemplate objects and autosized: packaged single-zone, PTAC, packaged VAV, or central VAV with chiller and boiler.
  - Or use `hvac="ideal"` for ideal loads.

```json
{"tool": "simulate_building_stock", "arguments": {"stock_id": "uofa_campus_enriched"}}
```

Without `climate_zone` or `weather_file`, the stock's site weather is used (see [Site Weather](#site-weather)). This returns a `batch_id`; poll it with `get_batch_status`, then aggregate the results (see [District Results](#district-results)). Each model set's `manifest.json` lists, per building, the model file, zoning, party walls, shading and every modeling assumption applied.

**Validation.** `scripts/validate_archetypes.py` generates models with the reference buildings' own dimensions and compares their simulated site energy (without refrigeration) with the DOE reference results for the same climate zone. With the Tucson TMYx 2011–2025 weather file (climate zone 2B), 35 of 49 cases are within ±20% of the reference EUI and the median absolute difference is 10.1%; San Francisco (3C) and Baltimore (4A) give 11.8% and 13.5% (`paper/results/archetypes_*.json`, Fig. 2 of the paper).

Known differences:
- Hospitals and outpatient clinics run about 50% low: one space type per building leaves out the reheat and humidification of their many space types.
- Warehouses run higher (+21% to +47% in 2B), because the whole floor is cooled while the reference cools only office and fine-storage areas.
- The two older large-hotel vintages and the two newer secondary-school vintages run 28–36% low.
- The single-family infiltration model overstates heating in windy, heating-dominated climates.
- Elevators, commercial refrigeration and kitchen exhaust are not modeled.

## District Results

Once a stock's batch completes, aggregate it:

```json
{"tool": "aggregate_stock_results",
 "arguments": {"batch_id": "<batch_id>", "emission_factors": {"electricity": 0.4, "natural_gas": 0.18}}}
```

This reads each building's EnergyPlus SQLite output and writes, under `outputs/stocks/<stock_id>/models/<model_set_id>/results/<batch_id>/`:

| File | Contents |
|---|---|
| `buildings.csv` | One row per building: floor area, annual energy by fuel and end use, site EUI, peak demand by fuel, inferred attributes, and emissions when factors are given |
| `results.geojson` | The stock's footprints with their results and result status, ready for QGIS or a web map |
| `district_hourly.csv` | Hourly district demand by fuel (8,760 rows): the sum over all buildings with results |
| `summary.json` | District totals and EUI, EUI distribution, peak demand and when it occurs, load factors, breakdowns by archetype and vintage, the highest-EUI buildings, and coverage |
| `map.html` | Interactive map of site EUI. The base map loads when it is viewed online |

The coverage block states how many buildings of the stock and of the model set have results. Every building without results is listed with the reason: the simulation failed, or the model could not be generated. District totals cover only buildings with results, and the summary says so. Emission factors are inputs in kg CO2e per kWh by fuel, since grid factors depend on the region and year studied.

## Retrofit Scenarios

A scenario applies measures to the archetype parameters of the buildings it targets and regenerates their models. Everything else stays identical to the baseline model set: geometry, neighbors, weather, schedules and HVAC option. So the difference in results is the effect of the measures.

```json
{"tool": "simulate_retrofit_scenario",
 "arguments": {
   "stock_id": "uofa_campus", "baseline_model_set_id": "baseline",
   "scenario": {
     "name": "electrify",
     "measures": [
       {"type": "heat_pump", "heating_cop": 3.3},
       {"type": "heat_pump_water_heater", "archetypes": ["single_family"]},
       {"type": "lighting", "target_w_m2": 6.5}],
     "vintages": ["pre1980", "1980_2004"],
     "adoption_rate": 0.5, "seed": 1}}}
```

| Measure | Parameters | Effect |
|---|---|---|
| `lighting` | `target_w_m2` or `reduction` | Lower interior lighting power density |
| `plug_loads` | `reduction` | Lower plug and process loads |
| `wall_insulation`, `roof_insulation` | `target_r` or `added_r` (m²K/W) | Raise the assembly R-value |
| `windows` | `u_factor`, optional `shgc`, `vt` | Replace windows where the new U-factor is lower |
| `air_sealing` | `reduction` | Lower design infiltration, or the effective leakage area for houses |
| `cool_roof` | optional `solar_absorptance` (default 0.3) | Reflective roof membrane |
| `hvac_efficiency` | `cooling_cop`, `heating_efficiency` | Higher-efficiency like-for-like equipment |
| `heat_pump` | optional `heating_cop` (default 3.3), `cooling_cop` | Replace packaged single-zone or PTAC heating with an air-source heat pump with electric backup. Central VAV systems are left unchanged |
| `heat_pump_water_heater` | optional `cop` (default 3.0) | Electric hot water at a constant COP. Ambient-temperature effects are not modeled |

How measures are applied:
- A measure applies only where it improves on the building's existing value, and each measure can carry its own `archetypes` / `vintages` filters.
- `adoption_rate` retrofits a reproducible random share of the targeted buildings, set by `seed`.
- The scenario's manifest lists, per building, every change made, such as `lighting 10.76 -> 6.46 W/m2`.

After the scenario's batch completes, aggregate it with `aggregate_stock_results`, then compare:

```json
{"tool": "compare_retrofit_scenario",
 "arguments": {"stock_id": "uofa_campus", "scenario_model_set_id": "baseline__electrify",
               "emission_factors": {"electricity": 0.4, "natural_gas": 0.18}}}
```

The comparison reports, over the buildings with results in both runs:
- District savings in total and by fuel, which shows fuel switching
- The change in peak demand
- Avoided emissions
- Savings by archetype and the spread of savings across retrofitted buildings
- Any retrofitted building whose energy use went up

It also writes a per-building `buildings_comparison.csv`.

## Batch Simulation

This is the key difference from single-model EnergyPlus MCP servers: **run a whole portfolio of buildings and poll for results**, instead of one blocking call per building.

**Why polling, not a single blocking call:** a portfolio of many buildings can take minutes to hours — far longer than an MCP client should have a single tool call block for. Each model runs as its own EnergyPlus process, started from the EnergyPlus command line directly; going through eppy, as the single-model `run_simulation` tool does, made each batch run about three times slower. `run_batch_simulation` returns a `batch_id` immediately; you poll `get_batch_status`/`get_batch_results` until it's done.

1. **Discover models in a directory**:
   ```json
   {"tool": "discover_idf_files", "arguments": {"directory": "sample_files", "pattern": "*.idf"}}
   ```

2. **Submit the batch**:
   ```json
   {
     "tool": "run_batch_simulation",
     "arguments": {
       "models": ["sample_files/1ZoneUncontrolled.idf", "sample_files/1ZoneEvapCooler.idf"],
       "weather_file": "sample_files/USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
       "continue_on_error": true
     }
   }
   ```
   Returns `{"batch_id": "...", "status": "queued", ...}`.

3. **Poll for status**:
   ```json
   {"tool": "get_batch_status", "arguments": {"batch_id": "<batch_id>"}}
   ```
   Repeat until `"is_complete": true`.

4. **Get per-model results**:
   ```json
   {"tool": "get_batch_results", "arguments": {"batch_id": "<batch_id>"}}
   ```

Per-model overrides are supported by passing dicts instead of plain path strings:
```json
{
  "tool": "run_batch_simulation",
  "arguments": {
    "models": [
      {"idf_path": "bldg_001.idf", "label": "office_a"},
      {"idf_path": "bldg_002.idf", "weather_file": "chicago.epw", "label": "office_b"}
    ],
    "continue_on_error": false
  }
}
```
With `continue_on_error: false`, the first model failure cancels the models not yet started. `cancel_batch` also stops EnergyPlus runs in progress, and when the server stops (for example when the MCP client disconnects) it stops unfinished batches and records them as cancelled.

## Architecture

The server follows a layered architecture:

```
┌─────────────────────────┐
│   MCP Protocol Layer    │  FastMCP server handling client communications
├─────────────────────────┤
│     Tools Layer         │  68 tools in 15 categories
├─────────────────────────┤
│  Orchestration Layer    │  EnergyPlus, Batch & Stock Managers, Config     
├─────────────────────────┤
│  EnergyPlus Integration │  Direct interface to simulation engine (per-model,
│                         │  one EnergyPlus process per concurrent batch model)
└─────────────────────────┘
```

**Project Structure:**
```
src/
├── ubem_mcp_server/
│   ├── server.py              # FastMCP server with tools
│   ├── energyplus_tools.py    # Core EnergyPlus integration (single model)
│   ├── batch_manager.py       # Batch/portfolio orchestration (new in UBEM-MCP)
│   ├── urban/                 # Building stock, weather, archetype models, results, retrofits (new in UBEM-MCP)
│   ├── config.py              # Configuration management
│   └── utils/                 # Specialized utilities
├── scripts/                    # Archetype-library builders, validation and scaling benchmarks
├── sample_files/               # Sample IDF and weather files
├── tests/                      # Unit and integration tests
├── agent_tests/                # Agent tasks, harnesses and results
└── pyproject.toml              # Dependencies
```

## Configuration

The server auto-detects EnergyPlus installation and uses sensible defaults. Configuration can be customized via environment variables:

- `EPLUS_IDD_PATH`: Path to EnergyPlus IDD file
- `UBEM_WORKSPACE_ROOT`: Folder that relative input paths are resolved against (default in the container: `/workspace/src`); outputs go to `<workspace>/outputs`
- `UBEM_OUTPUT_DIR`: Where stocks, models, batches and results are written, if not under the workspace
- `UBEM_LOG_LEVEL`: Logging level (default `INFO`; the command line uses `WARNING` unless `--verbose`)
- `UBEM_BATCH_MAX_WORKERS`: Max concurrent simulation processes for batch jobs (default: CPU count)
- `MCP_TRANSPORT`, `MCP_HTTP_HOST`, `MCP_HTTP_PORT`, `MCP_HTTP_PATH`, `MCP_TOKENS`: streamable-HTTP transport settings (see [Streamable HTTP Transport](#streamable-http-transport-local-testing))

## Troubleshooting

**Common Issues:**

1. **"IDD file not found"**: EnergyPlus 26.1 is missing. Use the Docker image, set `EPLUS_IDD_PATH`, or install EnergyPlus in its default folder (found automatically). Tools that do not simulate (data mode, queries, exports) work without it
2. **"Module not found"**: Run `uv sync` to install dependencies
3. **"Permission denied"**: Check file permissions
4. **"Simulation failed"**: Check EnergyPlus error messages in the model's output directory
5. **Batch stuck at `"is_complete": false`**: Check `get_batch_status` per-status counts — a very large batch with a low `max_workers` value simply takes longer; check `get_server_logs` for individual model errors

**Debugging:**
- Check server status: `get_server_status`
- View logs: `get_server_logs`
- Check errors: `get_error_logs`
- Check a batch job: `get_batch_status` / `get_batch_results`

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with tests
4. Run checks:
   ```bash
   uv run ruff check
   uv run black .
   uv run pytest
   ```
5. Submit a pull request

## Reproducing the paper

[paper/README.md](paper/README.md) lists the script behind every table and figure of the SoftwareX paper, the result files they produced, and how to regenerate the figures (`python paper/figures/make_figures.py`). `src/scripts/verify_examples.py` regenerates the worked examples' models and checks them against the models that were simulated.

## Cite this work

If you use UBEM-MCP, please cite the software (see [CITATION.cff](CITATION.cff); GitHub's "Cite this repository" button gives APA and BibTeX) and the EnergyPlus-MCP paper it builds on — see [NOTICE.md](NOTICE.md) for that citation.

## Funding

This work was supported by the U.S. National Science Foundation under grant [CMMI-2623038](https://www.nsf.gov/awardsearch/show-award/?AWD_ID=2623038). Any opinions, findings, conclusions or recommendations expressed here are those of the authors and do not necessarily reflect the views of the National Science Foundation.

## License

This repository uses two BSD-3-Clause licenses side by side:

- [LICENSE.txt](LICENSE.txt) (TensorBuildLab) covers UBEM-MCP's own original code, including the batch simulation feature.
- [LICENSE-EnergyPlus-MCP.txt](LICENSE-EnergyPlus-MCP.txt) / [COPYRIGHT-EnergyPlus-MCP.txt](COPYRIGHT-EnergyPlus-MCP.txt) (LBNL) covers the code carried over from EnergyPlus-MCP.

See [NOTICE.md](NOTICE.md) for exactly which files each license covers and the relationship between this project and EnergyPlus-MCP.

**Government Rights Notice**: The EnergyPlus-MCP portions of this software were developed under funding from the U.S. Department of Energy, and the U.S. Government consequently retains certain rights as described in [LICENSE-EnergyPlus-MCP.txt](LICENSE-EnergyPlus-MCP.txt).
