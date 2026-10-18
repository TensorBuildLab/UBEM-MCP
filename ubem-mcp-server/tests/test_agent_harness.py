"""The agent-test harness: answer reading, checks and the generic MCP client
(driven here by a scripted stand-in for a model)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_tests.harness import common, generic_client  # noqa: E402


def test_numbers_and_mentions():
    assert common.numbers("about 1,234.5 kWh, 2.3 million homes, 280k, −4.1%") == [1234.5, 2.3e6, 280000.0, -4.1]
    assert common.mentions("total 523 MWh", 523_400)       # kWh expected, MWh stated
    assert common.mentions("savings of 12.6%", -12.2, rel=0, abs_tol=1.0, scales=(1,))
    assert not common.mentions("savings of 15%", 12.2, rel=0, abs_tol=1.0, scales=(1,))


def test_every_task_has_known_checks_and_fixtures():
    for t in common.TASKS:
        assert set(t["checks"]) <= set(common.CHECKS), t["id"]
        for f in t["files"]:
            assert (common.ROOT / "fixtures" / f).exists()
        assert "tool" not in t["prompt"].lower()  # prompts are written as a user would ask


def scripted(steps):
    """A stand-in model: returns the scripted tool calls, then the final answer."""
    it = iter(steps)

    def chat(messages, tools):
        assert any(t["function"]["name"] == "query_stock" for t in tools)
        assert "UBEM-MCP" in messages[0]["content"]  # server instructions reach the model
        step = next(it)
        if isinstance(step, str):
            return {"choices": [{"message": {"role": "assistant", "content": step}}], "usage": {"prompt_tokens": 10}}
        calls = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                 for i, (n, a) in enumerate(step)]
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}}]}
    return chat


def test_generic_client_runs_a_task_and_is_scored(tmp_path):
    run_dir = common.prepare(tmp_path / "run", "U3_data")
    chat = scripted([
        [("get_workflow_guide", {"name": "data-only"})],
        [("load_building_stock", {"geojson_path": "parcels_inventory.geojson", "stock_id": "inv", "mode": "data"})],
        [("query_stock", {"stock_id": "inv", "group_by": ["LAND_USE"]}), ("query_stock", {"stock_id": "nope"})],
        [("export_stock", {"stock_id": "inv", "format": "csv", "columns": ["PARCEL"]})],
        "There are 10 structures. 70% have a known year built. Owner names and mailing addresses were dropped.",
    ])
    transcript = asyncio.run(generic_client.run("U3_data", run_dir, chat, "scripted"))
    assert transcript["error"] is None and transcript["turns"] == 5
    assert [c["name"] for c in transcript["tool_calls"]][:2] == ["get_workflow_guide", "load_building_stock"]
    assert any(c["ok"] is False and c["error_type"] == "not_found" for c in transcript["tool_calls"])
    s = common.score(run_dir, "U3_data", transcript)
    assert s["checks"]["data_stock_complete"]["passed"], s
    assert s["checks"]["export_with_parcel"]["passed"] and s["checks"]["no_simulation"]["passed"]
    assert s["checks"]["mentions_personal_columns"]["passed"]
    assert s["used_guide"] and s["failed_tool_calls"] == 1 and s["tool_calls"] == 5
    share = s["checks"]["answer_known_year_share"]
    assert share["passed"] == ("70%" in share["detail"]), share
