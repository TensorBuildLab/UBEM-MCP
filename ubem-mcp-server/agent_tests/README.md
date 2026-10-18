# Agent tests

These tests check that agents can do the community's common UBEM tasks (U1 to U4) with UBEM-MCP, across different MCP clients and model families. Nothing in UBEM-MCP is specific to one client or one model provider, and these tests are how that claim is checked.

## Tasks

`tasks.json` holds one task per use case. Each prompt is written the way a user would ask, without naming tools:

| Task | Use case | What the agent must do |
|---|---|---|
| `U1_district` | U1 | Model a district from footprints; report total energy, peak demand, EUI by type and the assumptions |
| `U2_retrofit` | U2 | Simulate a baseline and a retrofit scenario; report the savings |
| `U3_data` | U3 | Characterise an inventory without simulating, export it for GIS, and flag personal data |
| `U3_resstock` | U3 | Answer questions about a county's homes from NREL ResStock, including an upgrade's savings |
| `S41_campus` | U1, U2 | Rerun the paper's Section 4.1 campus electrification study from a plain request; passes only if the numbers match the published run |
| `U4_validate` | U4 | Simulate buildings and compare them with metered energy use |

Checks (`harness/common.py`) look only at what the server wrote and at the agent's final answer. For example, does the answer state the district total that the aggregated results contain? They never look at how the client works, so every client and model is scored the same way.

## Clients

- **Claude Code** (`harness/claude_code.py`): headless, with UBEM-MCP as its only tool source. It runs with no built-in tools, no other MCP servers, no settings files, and in an empty working folder.
- **Generic MCP client** (`harness/generic_client.py`): about 150 lines on the MCP Python SDK. It does what any MCP host does: it passes the server instructions and tool list to a model, runs the tool calls and stops at the answer. It reaches any model through the OpenAI-compatible chat API (OpenAI, Google Gemini, OpenRouter, Ollama, vLLM, and others).

Both clients add the same one-line note to the system prompt (`common.CLIENT_NOTE`): input files are in the tools' working folder.

## Running

The server runs in the development container (`ubem-p6`), which has EnergyPlus.

```bash
# Claude Code, from the host
python agent_tests/harness/claude_code.py U1_district U3_data --model sonnet --repeat 3

# Generic client with any OpenAI-compatible model, inside the container
docker exec -e UBEM_AGENT_API_KEY -e UBEM_AGENT_MODEL=gpt-5-mini -e UBEM_AGENT_BASE_URL=https://api.openai.com/v1 \
  -w /workspace/ubem-mcp-server ubem-p6 /tmp/venv/bin/python -m agent_tests.harness.generic_client U1_district \
  agent_tests/results/runs/generic_gpt-5-mini_U1_district

python agent_tests/summarize.py      # results/summary.md and summary.json
```

Each run keeps its workspace, transcript and score under `results/runs/` (not committed). `results/summary.*` is committed.

## Fixtures

`fixtures/` holds eight building footprints from part of the University of Arizona campus and, in `campus.geojson`, the 116 footprints of the paper's Section 4.1 case study with its published results in `campus_section_4_1_expected.json` (OpenStreetMap, © OpenStreetMap contributors, ODbL 1.0). Parcel numbers, years, land-use codes, owner names, mailing addresses and meter readings in `parcels_inventory.geojson` and `bills_2023.csv` are synthetic, made up for these tests (`build_fixtures.py`).
