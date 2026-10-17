"""Smoke test: the MCP server module imports and registers its tools.

Guards against dependency breaks (e.g. an incompatible `mcp` major version)
that unit tests of the individual managers would not notice. Importing the
server builds an EnergyPlusManager, which needs an EnergyPlus install, so this
runs with the integration tests.
"""
import asyncio
import os

import pytest

from ubem_mcp_server.config import Config

_idd = Config().energyplus.idd_path
pytestmark = [
    pytest.mark.energyplus,
    pytest.mark.skipif(not os.path.exists(_idd), reason=f"EnergyPlus not installed (IDD not found at {_idd!r})"),
]


def test_server_imports_and_registers_tools():
    from ubem_mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert {"load_idf_model", "run_energyplus_simulation", "get_server_status"} <= names
    assert {
        "discover_idf_files", "run_batch_simulation", "get_batch_status",
        "get_batch_results", "list_batches", "cancel_batch",
    } <= names
    assert {
        "list_archetypes", "load_building_stock", "fetch_osm_buildings",
        "get_building_stock", "list_building_stocks", "enrich_building_stock",
        "generate_building_models", "simulate_building_stock", "find_weather_stations", "get_site_weather", "aggregate_stock_results",
        "list_retrofit_measures", "simulate_retrofit_scenario", "compare_retrofit_scenario",
    } <= names
