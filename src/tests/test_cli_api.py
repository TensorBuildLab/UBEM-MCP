"""The Python library and command line: same tools, same results as MCP."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from ubem_mcp_server.config import Config

from .test_data_mode import INVENTORY

EPW = str(Path(__file__).parent.parent / "sample_files" / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw")
needs_ep = pytest.mark.skipif(not os.path.exists(Config().energyplus.idd_path), reason="EnergyPlus not installed")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A project folder with an inventory, and the server's shared state
    restored afterwards."""
    from ubem_mcp_server import server

    saved = {k: getattr(server, k) for k in ("config", "ep_manager", "batch_manager", "stock_manager")}
    monkeypatch.setenv("UBEM_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("UBEM_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("UBEM_BATCH_MAX_WORKERS", "2")
    (tmp_path / "inventory.geojson").write_text(json.dumps(INVENTORY))
    yield tmp_path
    for k, v in saved.items():
        setattr(server, k, v)


@pytest.fixture
def ubem(workspace):
    from ubem_mcp_server.api import UBEM

    u = UBEM(workspace=str(workspace))
    yield u
    u.close()


def test_library_exposes_every_tool_as_a_method(ubem, workspace):
    from ubem_mcp_server import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {t["name"] for t in ubem.tools()} == names
    assert "load_building_stock" in dir(ubem)
    assert "GeoJSON" in ubem.load_building_stock.__doc__
    assert ubem.config.paths.output_dir == str(workspace / "outputs")
    d = ubem.describe("query_stock")
    assert "stock_id" in d["input_schema"]["properties"] and d["annotations"]["readOnlyHint"] is True


def test_library_results_and_errors(ubem, workspace):
    from ubem_mcp_server.api import UBEMError

    res = ubem.load_building_stock(geojson_path="inventory.geojson", stock_id="inv", mode="data")
    assert res["ok"] and res["num_buildings"] == 6
    assert (workspace / "outputs" / "stocks" / "inv" / "meta.json").exists()
    with pytest.raises(UBEMError) as e:
        ubem.query_stock(stock_id="nope")
    assert e.value.error_type == "not_found" and e.value.result["ok"] is False
    ubem.raise_on_error = False
    assert ubem.query_stock(stock_id="nope")["error_type"] == "not_found"
    with pytest.raises(AttributeError):
        ubem.no_such_tool()
    text = ubem.get_server_status()  # inherited tools answer in text
    assert "text" in text or "server" in text


def test_three_entry_points_give_the_same_result(ubem, workspace, capsys):
    from ubem_mcp_server import cli, server

    ubem.load_building_stock(geojson_path="inventory.geojson", stock_id="inv", mode="data")
    args = {"stock_id": "inv", "group_by": ["use"], "filters": {"footprint_area_m2": {"min": 50}}}
    lib = ubem.query_stock(**args)
    _, mcp_result = asyncio.run(server.mcp.call_tool("query_stock", args))
    capsys.readouterr()
    code = cli.main(["query_stock", "--stock-id", "inv", "--group-by", "use",
                     "--filters", '{"footprint_area_m2": {"min": 50}}'])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert lib == mcp_result == out


def test_cli_lists_helps_and_validates(workspace, capsys):
    from ubem_mcp_server import cli

    assert cli.main(["tools"]) == 0
    assert "export_project_record" in capsys.readouterr().out
    assert cli.main(["help", "load_building_stock"]) == 0
    assert "--geojson-path" in capsys.readouterr().out
    assert cli.main(["guide"]) == 0 and "stock-to-district" in capsys.readouterr().out
    assert cli.main(["guide", "data-only"]) == 0 and "query_stock" in capsys.readouterr().out
    assert cli.main(["nope"]) == 2
    with pytest.raises(SystemExit, match="geojson-path"):
        cli.main(["load_building_stock"])
    with pytest.raises(SystemExit, match="integer"):
        cli.main(["query_stock", "--stock-id", "x", "--sample", "many"])
    assert cli.main(["query_stock", "--stock-id", "nope"]) == 1  # ok=false -> exit code 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "not_found"


def test_cli_options_exist_for_every_tool(ubem):
    from ubem_mcp_server import cli

    for name, tool in ubem._tools.items():
        parser = cli._tool_parser(tool)  # no clashing option names
        for prop in (tool.inputSchema or {}).get("properties") or {}:
            assert f"--{prop.replace('_', '-')}" in parser.format_help(), (name, prop)


def test_cli_writes_output_file_and_loads_with_args_json(workspace, capsys):
    from ubem_mcp_server import cli

    out = workspace / "load.json"
    code = cli.main(["load_building_stock", "--args", json.dumps({"geojson_path": "inventory.geojson",
                                                                  "stock_id": "inv", "mode": "data"}),
                     "--output", str(out)])
    assert code == 0 and json.loads(out.read_text())["num_buildings"] == 6


@pytest.mark.energyplus
@needs_ep
def test_cli_waits_for_simulations(workspace, capsys):
    from ubem_mcp_server import cli

    assert cli.main(["load_building_stock", "--geojson-path", "inventory.geojson", "--stock-id", "inv"]) == 0
    capsys.readouterr()
    code = cli.main(["simulate_building_stock", "--stock-id", "inv", "--weather-file", EPW,
                     "--climate-zone", "3C", "--building-ids", "h1", "--poll", "2"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    result = json.loads(captured.out)
    status = result["batch_status"][result["batch"]["batch_id"]]
    assert status["is_complete"] and status["counts"] == {"success": 1}
    assert "waiting for 1 batch" in captured.err
    # A new process (here: a new CLI call) finds the finished batch on disk.
    assert cli.main(["aggregate_stock_results", "--batch-id", result["batch"]["batch_id"],
                     "--make-map", "false"]) == 0
    agg = json.loads(capsys.readouterr().out)
    assert agg["coverage"]["buildings_with_results"] == 1


def test_documented_environment_variables_are_read_by_the_code():
    import re

    readme = Path(__file__).resolve().parents[2] / "README.md"
    if not readme.exists():
        pytest.skip("README.md not found next to the package")
    section = readme.read_text(encoding="utf-8").split("## Configuration", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"`((?:UBEM|EPLUS|MCP)_[A-Z_]+)`", section))
    code = "".join(p.read_text(encoding="utf-8") for p in (Path(__file__).parents[1] / "ubem_mcp_server").rglob("*.py"))
    assert documented and not {v for v in documented if v not in code}
