"""Workflow guides, server instructions and reference resources.

The guides must stay true to the tools: every tool call they show has to name
a real tool and real argument names (OpenStudio-MCP shipped guides that called
a tool with a parameter it does not accept). Every guide must reach every
client: as a tool, an MCP prompt and an MCP resource, with the same text.
"""
import asyncio
import json
import os
import re

import pytest

from ubem_mcp_server import guides
from ubem_mcp_server.config import Config

_idd = Config().energyplus.idd_path
pytestmark = [
    pytest.mark.energyplus,
    pytest.mark.skipif(not os.path.exists(_idd), reason=f"EnergyPlus not installed (IDD not found at {_idd!r})"),
]

CALL = re.compile(r"`([a-z_]+)\(([^`]*)\)`")
ARG = re.compile(r"(?:^|,\s*)([a-z_]+)=")
TOOL_WORD = re.compile(r"\b((?:get|run|simulate|aggregate|query|export|load|fetch|enrich|list|compare|assign|find|"
                       r"discover|cancel|validate|generate)_[a-z_]+)\b")


@pytest.fixture(scope="module")
def srv():
    from ubem_mcp_server import server
    return server


@pytest.fixture(scope="module")
def tool_args(srv):
    return {t.name: set(t.inputSchema.get("properties", {})) for t in asyncio.run(srv.mcp.list_tools())}


def test_guides_exist_with_front_matter():
    names = {g["name"] for g in guides.list_guides()}
    assert {"stock-to-district", "retrofit-scenario", "data-only", "own-models"} <= names
    for g in guides.list_guides():
        assert g["title"] and g["description"] and g["use_case"].startswith("U")
    with pytest.raises(KeyError, match="available"):
        guides.get_guide("nope")


@pytest.mark.parametrize("name", [g["name"] for g in guides.list_guides()])
def test_guide_tool_calls_name_real_tools_and_arguments(name, tool_args):
    text = guides.get_guide(name)["text"]
    calls = CALL.findall(text)
    assert calls, f"guide {name} shows no tool calls"
    for tool, args in calls:
        assert tool in tool_args, f"guide {name}: unknown tool {tool}"
        for arg in ARG.findall(args):
            assert arg in tool_args[tool], f"guide {name}: {tool} has no argument {arg!r}"
    for other in re.findall(r"guide `([a-z-]+)`", text):
        guides.get_guide(other)  # cross-references point at real guides


def test_server_instructions_name_real_tools(srv, tool_args):
    words = set(TOOL_WORD.findall(srv.SERVER_INSTRUCTIONS))
    assert words and words <= set(tool_args), words - set(tool_args)
    assert "claude" not in srv.SERVER_INSTRUCTIONS.lower()  # client- and model-neutral


def test_each_guide_reaches_clients_three_ways(srv):
    async def run():
        _, listed = await srv.mcp.call_tool("get_workflow_guide", {})
        assert {g["name"] for g in listed["guides"]} == {g["name"] for g in guides.list_guides()}
        prompts = {p.name for p in await srv.mcp.list_prompts()}
        resources = {str(r.uri) for r in await srv.mcp.list_resources()}
        for g in guides.list_guides():
            text = guides.get_guide(g["name"])["text"]
            _, tool = await srv.mcp.call_tool("get_workflow_guide", {"name": g["name"]})
            assert tool["ok"] and tool["text"] == text
            assert g["name"] in prompts
            prompt = await srv.mcp.get_prompt(g["name"], {})
            assert prompt.messages[0].content.text == text
            assert f"ubem://guides/{g['name']}" in resources
            res = list(await srv.mcp.read_resource(f"ubem://guides/{g['name']}"))
            assert res[0].content == text
        _, missing = await srv.mcp.call_tool("get_workflow_guide", {"name": "nope"})
        assert missing["ok"] is False and missing["error_type"] == "not_found"
    asyncio.run(run())


def test_reference_resources_come_from_code(srv):
    from ubem_mcp_server.urban.archetypes import ARCHETYPES
    from ubem_mcp_server.urban.retrofit import list_measures

    async def read(uri):
        return json.loads(list(await srv.mcp.read_resource(uri))[0].content)

    arch = asyncio.run(read("ubem://archetypes"))
    assert {a["key"] for a in arch["archetypes"]} == set(ARCHETYPES)
    assert asyncio.run(read("ubem://retrofit-measures")) == json.loads(json.dumps(list_measures()))
    schema = asyncio.run(read("ubem://scenario-schema"))
    assert "measures" in schema["scenario_keys"] and schema["example"]["measures"]
