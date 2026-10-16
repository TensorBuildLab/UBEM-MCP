"""Smoke test: the MCP server module imports and registers its tools.

Guards against dependency breaks (e.g. an incompatible `mcp` major version)
that unit tests of the individual managers would not notice.
"""
import asyncio


def test_server_imports_and_registers_tools():
    from ubem_mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert {"load_idf_model", "run_energyplus_simulation", "get_server_status"} <= names
    assert {
        "discover_idf_files", "run_batch_simulation", "get_batch_status",
        "get_batch_results", "list_batches", "cancel_batch",
    } <= names
