# UBEM-MCP

A Model Context Protocol (MCP) server for **urban building energy modeling (UBEM)**, built on EnergyPlus. It provides **41 tools** for loading, validating, modifying, analyzing, and simulating EnergyPlus building energy models — including **batch simulation of a whole portfolio of buildings at once**, which is the core addition over single-model EnergyPlus servers.

> **Version**: 0.1.0
> **EnergyPlus Compatibility**: 26.1.0 (default; see [Building against a different EnergyPlus version](#building-against-a-different-energyplus-version))
> **Python**: 3.10+

This project is derived from [EnergyPlus-MCP](https://github.com/LBNL-ETA/EnergyPlus-MCP) (LBNL) — see [NOTICE.md](NOTICE.md) for attribution and the citation for the original work.

<details open>
<summary><h2>📑 Table of Contents</h2></summary>

- [Overview](#overview)
- [Installation](#installation)
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
- [Batch Simulation](#batch-simulation)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Cite this work](#cite-this-work)
- [License](#license)

</details>

## Overview

UBEM-MCP makes EnergyPlus building energy simulation — for a single building or an entire portfolio — accessible to AI assistants and automation tools through the Model Context Protocol.

**Key Features:**
- 🏙️ **Batch / Portfolio Simulation**: Run many EnergyPlus models concurrently as a background job; poll for status and results instead of blocking on a single long-running call
- 🏗️ **Complete Model Lifecycle**: Load, validate, analyze, modify, and simulate IDF files
- 🔍 **Deep Building Analysis**: Extract detailed information about zones, surfaces, materials, and schedules
- 🔧 **HVAC Intelligence**: Discover, analyze, and visualize HVAC system topology
- 📈 **Smart Output Management**: Auto-discover and configure output variables/meters
- 📊 **Advanced Visualization**: Create interactive plots and HVAC system diagrams

## Installation

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
           "-w", "/workspace/ubem-mcp-server",
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
           "-w", "/workspace/ubem-mcp-server",
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
           "-w", "/workspace/ubem-mcp-server",
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
docker run -it --rm -v "$(pwd)":/workspace -w /workspace/ubem-mcp-server ubem-mcp-dev bash

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
cd UBEM-MCP/ubem-mcp-server
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

**2. Create `.env` in `ubem-mcp-server/`** (gitignored). Copy from [.env.example](ubem-mcp-server/.env.example) and fill in your values:

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
  -w /workspace/ubem-mcp-server \
  --env-file ubem-mcp-server/.env \
  ubem-mcp-dev \
  uv run python -m ubem_mcp_server.server
```

*Local:* (requires a host EnergyPlus install matching whatever `EPLUS_IDD_PATH` is set to in `.env`)

```bash
cd ubem-mcp-server
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

**Tests** — transport and auth behavior is covered by `tests/test_config_transport.py` and `tests/test_auth.py`; batch orchestration is covered by `tests/test_batch_manager.py`. Run with:
```bash
uv run pytest tests/
```

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

- `ubem-mcp-server/ubem_mcp_server/config.py` — `version` and `default_installation`
- `ubem-mcp-server/.vscode/mcp.json` — the two `EnergyPlusV26-1-0` strings
- The client config JSON you created for Claude Desktop / VS Code / Cursor — only if it sets `EPLUS_IDD_PATH` explicitly

## Available Tools

The server provides **41 tools** organized into **6 categories**:

### 🏙️ Batch Simulation (6 tools) — new in UBEM-MCP
- `discover_idf_files` - Glob a directory for IDF files to build a batch's model list
- `run_batch_simulation` - Run many EnergyPlus models concurrently as a background job; returns a `batch_id` immediately
- `get_batch_status` - Poll overall/per-status progress of a batch job
- `get_batch_results` - Get per-model results (success/failure, duration, output paths)
- `list_batches` - List all known batch jobs (in-memory + persisted on disk)
- `cancel_batch` - Cancel a batch job (best-effort for models already running)

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

### Basic Workflow

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
    -w /workspace/ubem-mcp-server \
    ubem-mcp-dev \
    uv run python -m ubem_mcp_server.server
```

## Batch Simulation

This is the key difference from single-model EnergyPlus MCP servers: **run a whole portfolio of buildings and poll for results**, instead of one blocking call per building.

**Why polling, not a single blocking call:** eppy's `IDF` class holds module-level global state, so concurrent simulations run as separate worker processes rather than in-process threads. And a portfolio of many buildings can take minutes to hours — far longer than an MCP client should have a single tool call block for. `run_batch_simulation` returns a `batch_id` immediately; you poll `get_batch_status`/`get_batch_results` until it's done.

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
With `continue_on_error: false`, the first model failure cancels remaining not-yet-started models in the batch (best-effort — an EnergyPlus subprocess already running in a worker can't be forcibly killed).

## Architecture

The server follows a layered architecture:

```
┌─────────────────────────┐
│   MCP Protocol Layer    │  FastMCP server handling client communications
├─────────────────────────┤
│     Tools Layer         │  41 tools organized into 6 categories
├─────────────────────────┤
│  Orchestration Layer    │  EnergyPlus Manager, Batch Manager & Config Module
├─────────────────────────┤
│  EnergyPlus Integration │  Direct interface to simulation engine (per-model,
│                         │  one worker process per concurrent batch model)
└─────────────────────────┘
```

**Project Structure:**
```
ubem-mcp-server/
├── ubem_mcp_server/
│   ├── server.py              # FastMCP server with tools
│   ├── energyplus_tools.py    # Core EnergyPlus integration (single model)
│   ├── batch_manager.py       # Batch/portfolio orchestration (new in UBEM-MCP)
│   ├── config.py              # Configuration management
│   └── utils/                 # Specialized utilities
├── sample_files/               # Sample IDF and weather files
├── tests/                      # Unit tests
└── pyproject.toml              # Dependencies
```

## Configuration

The server auto-detects EnergyPlus installation and uses sensible defaults. Configuration can be customized via environment variables:

- `EPLUS_IDD_PATH`: Path to EnergyPlus IDD file
- `EPLUS_SAMPLE_PATH`: Custom sample files directory
- `EPLUS_OUTPUT_PATH`: Output directory for results
- `UBEM_BATCH_MAX_WORKERS`: Max concurrent simulation processes for batch jobs (default: CPU count)
- `MCP_TRANSPORT`, `MCP_HTTP_HOST`, `MCP_HTTP_PORT`, `MCP_HTTP_PATH`, `MCP_TOKENS`: streamable-HTTP transport settings (see [Streamable HTTP Transport](#streamable-http-transport-local-testing))

## Troubleshooting

**Common Issues:**

1. **"IDD file not found"**: Ensure EnergyPlus is installed
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

## Cite this work

UBEM-MCP is built on EnergyPlus-MCP. If you use UBEM-MCP in your research, please cite the original EnergyPlus-MCP paper — see [NOTICE.md](NOTICE.md) for the full citation and BibTeX entry.

## License

UBEM-MCP is distributed under the same modified BSD license as EnergyPlus-MCP, on which it's built — see [License.txt](License.txt) for the full license text and [Copyright.txt](Copyright.txt) for the copyright notice. See [NOTICE.md](NOTICE.md) for the relationship between this project and EnergyPlus-MCP.

**Government Rights Notice**: The EnergyPlus-MCP portions of this software were developed under funding from the U.S. Department of Energy, and the U.S. Government consequently retains certain rights as described in [License.txt](License.txt).
