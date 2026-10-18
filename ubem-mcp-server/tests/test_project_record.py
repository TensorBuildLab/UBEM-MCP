"""Project records: recipes, hashes, software versions, export and rerun."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import record

from .test_data_mode import INVENTORY
from .test_urban_stock import _manager

EPW = str(Path(__file__).parent.parent / "sample_files" / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw")
needs_idd = pytest.mark.skipif(not os.path.exists(Config().energyplus.idd_path),
                               reason="EnergyPlus IDD not installed")

PARCELS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": f["geometry"], "properties": {"PID": f["properties"]["id"], "YR": "1962"}}
    for f in INVENTORY["features"] if f["properties"]["id"] == "h1"]}


def _project(tmp_path):
    """data load -> enrich -> assign_archetypes -> weather -> baseline -> scenario."""
    (tmp_path / "inventory.geojson").write_text(json.dumps(INVENTORY))
    (tmp_path / "parcels.geojson").write_text(json.dumps(PARCELS))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(tmp_path / "inventory.geojson"), stock_id="inv", mode="data", license="CC0-1.0")
    mgr.enrich("inv", str(tmp_path / "parcels.geojson"), {"year_built": "YR"}, new_stock_id="inv_parcels",
               layer_license="public domain")
    mgr.assign_archetypes("inv_parcels", new_stock_id="inv_model", default_year=1970)
    mgr.resolve_weather("inv_model", weather_file=EPW)
    mgr.generate_models("inv_model", model_set_id="baseline")
    mgr.generate_scenario("inv_model", "baseline", {"name": "led", "measures": [{"type": "lighting",
                                                                                "reduction": 0.4}], "seed": 1})
    return mgr


def test_sha256_and_combined_hash(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("abc")
    assert record.sha256_file(str(f)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert record.sha256_file(str(tmp_path / "missing")) is None
    assert record.combined_hash({"a": "1", "b": "2"}) == record.combined_hash({"b": "2", "a": "1"})
    assert record.combined_hash({"a": "1"}) != record.combined_hash({"a": "2"})


def test_software_versions_name_energyplus_and_library():
    cfg = Config()
    sv = record.software_versions(cfg.energyplus.idd_path)
    assert sv["ubem_mcp"] and sv["python"]
    assert len(sv["archetype_library"]["sha256"]) == 64
    assert all(sv["archetype_library"]["files"].values())
    if os.path.exists(cfg.energyplus.idd_path):
        assert sv["energyplus"]["idd_version"] and sv["energyplus"]["idd_build"]
    other = json.loads(json.dumps(sv))
    other["energyplus"]["idd_version"] = "9.9.9"
    assert any("EnergyPlus differs" in d for d in record.compare_software(other, sv))
    assert record.compare_software(sv, sv) == []


def test_every_step_stores_a_recipe_with_input_hashes(tmp_path):
    (tmp_path / "inventory.geojson").write_text(json.dumps(INVENTORY))
    (tmp_path / "parcels.geojson").write_text(json.dumps(PARCELS))
    mgr = _manager(tmp_path)
    mgr.load_from_file(str(tmp_path / "inventory.geojson"), stock_id="inv", mode="data")
    mgr.enrich("inv", str(tmp_path / "parcels.geojson"), {"year_built": "YR"}, new_stock_id="inv2")
    mgr.assign_archetypes("inv2", new_stock_id="inv3", default_year=1970)

    load = mgr.get_meta("inv")["recipe"]
    assert load["op"] == "load" and load["args"]["loader_options"] == {"mode": "data"}
    assert load["inputs"][0]["sha256"] == record.sha256_file(str(tmp_path / "inventory.geojson"))
    enrich = mgr.get_meta("inv2")["recipe"]
    assert enrich["op"] == "enrich" and enrich["parent"] == "inv" and enrich["args"]["field_map"] == {"year_built": "YR"}
    assert enrich["inputs"][0]["sha256"] == record.sha256_file(str(tmp_path / "parcels.geojson"))
    assert mgr.get_meta("inv3")["recipe"] == {"op": "assign_archetypes", "args": {"default_year": 1970},
                                              "inputs": [], "parent": "inv2"}
    assert mgr.lineage("inv3") == ["inv", "inv2", "inv3"]


@needs_idd
def test_project_record_covers_lineage_models_and_software(tmp_path):
    mgr = _project(tmp_path)
    base = mgr.model_manifest("inv_model", "baseline")
    assert base["recipe"]["op"] == "generate_models"
    assert base["recipe"]["inputs"][0]["sha256"] == record.sha256_file(EPW)
    assert all(m["idf_sha256"] == record.sha256_file(m["idf_path"]) for m in base["models"])
    assert base["software"]["archetype_library"]["sha256"]
    scen = mgr.model_manifest("inv_model", "baseline__led")
    assert scen["recipe"]["op"] == "generate_scenario" and scen["recipe"]["args"]["scenario"]["seed"] == 1

    out = mgr.project_record("inv_model")
    assert out["steps"] == ["inv", "inv_parcels", "inv_model"]
    assert {m["model_set_id"] for m in out["model_sets"]} == {"baseline", "baseline__led"}
    assert out["warnings"] == [] and out["software"]["archetype_library_sha256"]
    rec = json.loads(Path(out["record_path"]).read_text())
    assert rec["steps"][-1]["weather"]["epw_sha256"] == record.sha256_file(EPW)
    assert len(rec["model_sets"][0]["idf_sha256"]) == base["num_models"]
    assert "CC0-1.0" in json.dumps(rec["sources"])


@needs_idd
def test_rerun_reproduces_identical_models(tmp_path):
    mgr = _project(tmp_path)
    path = mgr.project_record("inv_model")["record_path"]
    out = mgr.rerun_record(path)
    assert out["stock_ids"] == {"inv": "inv_rerun", "inv_parcels": "inv_parcels_rerun",
                                "inv_model": "inv_model_rerun"}
    assert out["reproduced"] is True, out["warnings"]
    for ms in out["model_sets"]:
        assert ms["different"] == 0 and ms["identical"] == ms["num_models"] > 0
    assert mgr.get("inv_parcels_rerun").get("h1").year_built == 1962


@needs_idd
def test_rerun_warns_when_an_input_changed(tmp_path):
    mgr = _project(tmp_path)
    path = mgr.project_record("inv_model")["record_path"]
    changed = json.loads((tmp_path / "parcels.geojson").read_text())
    changed["features"][0]["properties"]["YR"] = "2010"
    (tmp_path / "parcels.geojson").write_text(json.dumps(changed))
    out = mgr.rerun_record(path, id_suffix="_v2")
    assert out["reproduced"] is False
    assert any("attribute layer" in w and "changed" in w for w in out["warnings"])
    assert any("differ from the record" in w for w in out["warnings"])


def test_rerun_refuses_missing_or_old_records(tmp_path):
    mgr = _manager(tmp_path)
    with pytest.raises(FileNotFoundError):
        mgr.rerun_record(str(tmp_path / "nope.json"))
    (tmp_path / "old.json").write_text(json.dumps({"record_version": 0}))
    with pytest.raises(ValueError, match="version"):
        mgr.rerun_record(str(tmp_path / "old.json"))


@needs_idd
def test_record_tools_through_mcp(tmp_path, monkeypatch):
    from ubem_mcp_server import server

    mgr = _project(tmp_path)
    monkeypatch.setattr(server, "stock_manager", mgr)

    async def run():
        _, rec = await server.mcp.call_tool("export_project_record", {"stock_id": "inv_model"})
        assert rec["ok"] and rec["steps"][-1] == "inv_model"
        _, rerun = await server.mcp.call_tool("rerun_project_record",
                                              {"record_path": rec["record_path"], "id_suffix": "_mcp"})
        assert rerun["ok"] and rerun["reproduced"] and rerun["stock_id"] == "inv_model_mcp"
        _, bad = await server.mcp.call_tool("export_project_record", {"stock_id": "nope"})
        assert bad["ok"] is False and bad["error_type"] == "not_found"
    asyncio.run(run())


@needs_idd
def test_rerun_with_simulate_submits_one_batch_per_model_set(tmp_path, monkeypatch):
    from ubem_mcp_server import server

    mgr = _project(tmp_path)
    monkeypatch.setattr(server, "stock_manager", mgr)
    submitted = []

    def fake_submit(**kw):
        submitted.append(kw)
        return {"batch_id": f"b{len(submitted)}"}

    monkeypatch.setattr(server.batch_manager, "submit_batch", fake_submit)

    async def run():
        path = mgr.project_record("inv_model")["record_path"]
        _, out = await server.mcp.call_tool("rerun_project_record", {"record_path": path, "simulate": True})
        assert out["ok"] and [b["model_set_id"] for b in out["batches"]] == ["baseline", "baseline__led"]
        assert any("get_batch_status" in s for s in out["next_steps"])
    asyncio.run(run())
    assert submitted[0]["weather_file"] == EPW
    assert submitted[0]["metadata"] == {"stock_id": "inv_model_rerun", "model_set_id": "baseline"}
    assert submitted[0]["output_directory"].endswith("models/baseline/runs")
