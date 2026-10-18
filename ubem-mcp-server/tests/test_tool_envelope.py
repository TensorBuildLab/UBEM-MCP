"""MCP-level tests: tool annotations, the response envelope and response sizes.

These call tools through the MCP server exactly as a client would, so they
check what any MCP client and model receives, not only the Python functions.
Importing the server builds an EnergyPlusManager, which needs an EnergyPlus
install, so these run with the integration tests.
"""
import asyncio
import json
import os

import pytest

from ubem_mcp_server.config import Config

from .test_urban_stock import fc, feature, rect

_idd = Config().energyplus.idd_path
pytestmark = [
    pytest.mark.energyplus,
    pytest.mark.skipif(not os.path.exists(_idd), reason=f"EnergyPlus not installed (IDD not found at {_idd!r})"),
]

# Responses must fit comfortably in a model's context; OpenStudio-MCP caps at 10k characters.
MAX_RESPONSE_CHARS = 20_000

UBEM_TOOLS = {
    "discover_idf_files", "run_batch_simulation", "get_batch_status", "get_batch_results", "list_batches",
    "cancel_batch", "list_archetypes", "load_building_stock", "fetch_osm_buildings", "enrich_building_stock",
    "get_building_stock", "query_stock", "export_stock", "assign_archetypes", "list_building_stocks",
    "find_weather_stations", "get_site_weather", "generate_building_models", "simulate_building_stock",
    "aggregate_stock_results", "list_retrofit_measures", "simulate_retrofit_scenario", "compare_retrofit_scenario",
}


@pytest.fixture()
def server(tmp_path, monkeypatch):
    from ubem_mcp_server import server as srv
    from ubem_mcp_server.urban.stock_manager import StockManager

    cfg = Config()
    cfg.paths.output_dir = str(tmp_path / "outputs")
    cfg.paths.workspace_root = str(tmp_path)
    monkeypatch.setattr(srv, "stock_manager", StockManager(cfg))
    return srv


def call(srv, name, **args):
    content, structured = asyncio.run(srv.mcp.call_tool(name, args))
    text = content[0].text
    assert json.loads(text) == structured  # text and structured content agree
    return structured, text


def test_every_tool_is_annotated_and_ubem_tools_have_output_schema(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert UBEM_TOOLS <= set(tools)
    assert all(t.annotations is not None for t in tools.values())
    for name in UBEM_TOOLS:
        schema = tools[name].outputSchema
        assert schema and "ok" in schema["properties"], name
    assert tools["query_stock"].annotations.readOnlyHint
    assert tools["cancel_batch"].annotations.destructiveHint
    assert tools["clear_logs"].annotations.destructiveHint
    assert tools["fetch_osm_buildings"].annotations.openWorldHint
    assert not tools["load_building_stock"].annotations.readOnlyHint


def test_envelope_success_and_error_types(server, tmp_path):
    src = tmp_path / "blocks.geojson"
    src.write_text(json.dumps(fc(feature(rect(0, 0, 20, 20), building="house", OWNER="X", id="a"))))

    res, _ = call(server, "load_building_stock", geojson_path=str(src), stock_id="s")
    assert res["ok"] is True and res["stock_id"] == "s" and res["num_buildings"] == 1
    assert any("OWNER" in w for w in res["warnings"])

    res, _ = call(server, "get_building_stock", stock_id="missing")
    assert res == {"ok": False, "warnings": [], "error_type": "not_found", "error": "Unknown stock_id: missing"}

    res, _ = call(server, "query_stock", stock_id="s", metrics=["avg:height_m"])
    assert res["ok"] is False and res["error_type"] == "invalid_input" and "Bad metric" in res["error"]

    res, _ = call(server, "load_building_stock", geojson_path=str(tmp_path / "nope.geojson"))
    assert res["ok"] is False and res["error_type"] == "not_found"

    res, _ = call(server, "list_retrofit_measures")
    assert res["ok"] is True and res["count"] == len(res["items"]) > 0


def test_aggregate_reports_running_batch_without_error(server, monkeypatch):
    monkeypatch.setattr(server.batch_manager, "get_status",
                        lambda batch_id: {"is_complete": False, "counts": {"running": 3}, "metadata": {}})
    res, _ = call(server, "aggregate_stock_results", batch_id="b1")
    assert res["ok"] is True and res["is_complete"] is False and res["next_steps"]


def test_responses_stay_small_on_a_large_stock(server, tmp_path):
    feats = [feature(rect(30 * (i % 20), 30 * (i // 20), 12, 10), building="house", id=f"b{i}",
                     PARCEL=f"{i:06d}", LAND_USE="0131", start_date=str(1920 + i % 90))
             for i in range(400)]
    src = tmp_path / "big.geojson"
    src.write_text(json.dumps(fc(*feats)))

    checks = [
        ("load_building_stock", {"geojson_path": str(src), "stock_id": "big", "mode": "data"}),
        ("get_building_stock", {"stock_id": "big"}),
        ("query_stock", {"stock_id": "big", "group_by": ["PARCEL"]}),  # 400 groups, capped
        ("query_stock", {"stock_id": "big", "group_by": ["decade"], "sample": 50, "columns": ["PARCEL"]}),
        ("list_building_stocks", {}),
        ("list_archetypes", {}),
        ("list_retrofit_measures", {}),
        ("export_stock", {"stock_id": "big", "format": "csv"}),
        ("assign_archetypes", {"stock_id": "big", "new_stock_id": "big_model"}),
    ]
    for name, args in checks:
        res, text = call(server, name, **args)
        assert res["ok"], (name, res)
        assert len(text) < MAX_RESPONSE_CHARS, (name, len(text))

    res, _ = call(server, "query_stock", stock_id="big", group_by=["PARCEL"])
    assert res["num_groups"] == 400 and len(res["groups"]) == 20 and res["other_groups"]["groups"] == 380



def test_full_workflow_through_mcp_tools(server, tmp_path):
    """Stock -> simulate -> poll -> aggregate -> retrofit -> compare, as a client would run it.
    Runs on one event loop, as a real server does, so background batches keep running."""
    from .test_integration_energyplus import EPW

    src = tmp_path / "pair.geojson"
    src.write_text(json.dumps(fc(feature(rect(0, 0, 12, 10), building="house", id="h1", start_date="1965"),
                                 feature(rect(40, 0, 12, 10), building="house", id="h2", start_date="2010"))))

    async def acall(name, **args):
        content, structured = await server.mcp.call_tool(name, args)
        assert json.loads(content[0].text) == structured
        return structured, content[0].text

    async def wait(batch_id):
        for _ in range(300):
            st, _ = await acall("get_batch_status", batch_id=batch_id)
            if st["is_complete"]:
                return st
            await asyncio.sleep(1)
        raise AssertionError("batch did not finish")

    async def workflow():
        res, _ = await acall("load_building_stock", geojson_path=str(src), stock_id="pair", license="CC0-1.0")
        assert res["ok"]
        res, _ = await acall("simulate_building_stock", stock_id="pair", climate_zone="3C", weather_file=EPW,
                             model_set_id="base", max_workers=2)
        assert res["ok"] and res["next_steps"], res
        batch_id = res["batch"]["batch_id"]
        assert (await wait(batch_id))["counts"].get("success") == 2

        page, _ = await acall("get_batch_results", batch_id=batch_id, limit=1)
        assert page["num_models"] == 2 and len(page["models"]) == 1 and page["next_offset"] == 1

        agg, text = await acall("aggregate_stock_results", batch_id=batch_id, make_map=False)
        assert agg["ok"] and agg["coverage"]["buildings_with_results"] == 2
        assert len(text) < MAX_RESPONSE_CHARS

        scen = {"name": "leds", "measures": [{"type": "lighting", "reduction": 0.5}]}
        res, _ = await acall("simulate_retrofit_scenario", stock_id="pair", baseline_model_set_id="base",
                             scenario=scen, max_workers=2)
        assert res["ok"] and any("compare_retrofit_scenario" in s for s in res["next_steps"])
        await wait(res["batch"]["batch_id"])
        res, _ = await acall("aggregate_stock_results", batch_id=res["batch"]["batch_id"], make_map=False)
        assert res["ok"]
        cmp, _ = await acall("compare_retrofit_scenario", stock_id="pair", scenario_model_set_id="base__leds")
        assert cmp["ok"] and cmp["district"]["savings_kwh"] > 0

    asyncio.run(workflow())
