# Changelog

## 1.0.0 (2026-10-26)

First release, described in the SoftwareX paper (see [paper/](paper/README.md)). Built on EnergyPlus-MCP v0.1.0, whose 35 single-model tools are kept.

New in UBEM-MCP:

- **Building stocks**: load GeoJSON or fetch OpenStreetMap footprints; model mode (archetype per building) or data mode (raw inventory, unknown values left empty); join parcels, points or other GIS layers; query, group and export (CSV, GeoJSON, ComStock-style columns) with sources and licences; personal columns dropped on load and refused in joins.
- **Site weather**: nearest Climate.OneBuilding.Org station, typical-year weather and ASHRAE 169 climate zone.
- **Archetype models**: EnergyPlus models generated per building from the DOE Commercial Reference Buildings and the residential single-family prototype, with every assumption listed; building data (such as heating fuel) override archetype defaults; users' own EnergyPlus models can replace archetypes, and models for older EnergyPlus versions are upgraded.
- **Background batches**: any set of EnergyPlus models as a job with progress, time left, failures grouped by cause, cancellation and restart-safe records; one batch at a time with a worker cap; long-poll waiting.
- **District results and retrofit scenarios**: totals, end uses by fuel, hourly peaks, per-building EUI and maps; declarative measure lists compared with their baseline.
- **Measured data**: bills, meters or benchmarking tables matched to buildings and compared with models using ASHRAE Guideline 14 statistics, by fuel and archetype.
- **ComStock and ResStock**: county-level reference data, weighted queries and benchmarks.
- **Project records**: input hashes, software versions and recipes; one tool reruns a study and checks that its models match.
- **One envelope for every tool** (`ok`, `warnings`, `error_type`, `error`) as MCP structured content; workflow guides as MCP prompts, resources and a plain tool.
- **Three entry points**: MCP server (stdio or authenticated streamable HTTP), Python library (`UBEM` class) and `ubem-mcp` command line.
- **Ready-to-run image** `ghcr.io/tensorbuildlab/ubem-mcp` with EnergyPlus 26.1, built and tested by `.github/workflows/image.yml` and published with each release.
- **Tests**: unit tests on Python 3.10–3.12, integration tests with real EnergyPlus 26.1 in CI, and agent tasks with two harnesses (Claude Code and a generic MCP client).

Fixed before release:

- Batch workers are threads that each start an EnergyPlus process, instead of forkserver worker processes, which re-imported the caller's main script: an unguarded script ran again in every worker and code read from stdin failed in every worker.
- `aggregate_stock_results` answers `ok: false` when no building has results, and warns when only some do.
- `cancel_batch` stops EnergyPlus runs in progress; a server that stops (for example when its MCP client disconnects) stops its unfinished batches and records them as cancelled, and a batch record left 'running' by a stopped server reads as cancelled.
- The server and library start without EnergyPlus (single-model tools report the missing installation when called); outside the container the workspace defaults to the current folder and EnergyPlus 26.1 is found in its default install folder.
