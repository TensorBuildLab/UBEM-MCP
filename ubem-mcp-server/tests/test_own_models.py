"""Users' own EnergyPlus models inside a model set (set_building_models)."""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import own_models, record

from .test_project_record import EPW, _project
from .test_urban_stock import _manager

SAMPLE = Path(__file__).parent.parent / "sample_files" / "1ZoneUncontrolled.idf"
_cfg = Config()
needs_idd = pytest.mark.skipif(not os.path.exists(_cfg.energyplus.idd_path), reason="EnergyPlus IDD not installed")


def test_prepare_adds_only_missing_outputs_and_says_so(tmp_path):
    info = own_models.prepare_supplied_model(str(SAMPLE), str(tmp_path / "out.idf"), "26.1.0")
    text = (tmp_path / "out.idf").read_text()
    assert info["energyplus_version"] == "25.1"
    assert "Output:SQLite" in info["outputs_added"]
    assert "Output:Meter Electricity:Facility hourly" in info["outputs_added"]
    assert "Output:Table:SummaryReports" not in info["outputs_added"]  # the model already has AllSummary
    assert any("EnergyPlus 25.1" in w and "IDFVersionUpdater" in w for w in info["warnings"])
    assert text.startswith("! Supplied model 1ZoneUncontrolled.idf")
    assert SAMPLE.read_text().strip() in text  # the user's model itself is unchanged

    again = own_models.prepare_supplied_model(str(tmp_path / "out.idf"), str(tmp_path / "out2.idf"), "25.1")
    assert again["outputs_added"] == [] and again["warnings"] == []


def test_prepare_fixes_sqlite_option_in_place(tmp_path):
    src = tmp_path / "m.idf"
    src.write_text("Version,26.1;\nBuilding,B,0,City,0.04,0.4,FullExterior,25,6;\n"
                   "Output:SQLite,\n  Simple,  !- Option Type\n  JtoKWH;\n")
    info = own_models.prepare_supplied_model(str(src), str(tmp_path / "o.idf"), "26.1.0")
    text = (tmp_path / "o.idf").read_text()
    assert "Output:SQLite option SimpleAndTabular" in info["outputs_added"]
    assert sum(line.startswith("Output:SQLite") for line in text.splitlines()) == 1 and "SimpleAndTabular,  !- Option Type" in text


def test_prepare_rejects_what_is_not_a_model(tmp_path):
    (tmp_path / "a.osm").write_text("OS:Version;")
    with pytest.raises(ValueError, match="OpenStudio"):
        own_models.prepare_supplied_model(str(tmp_path / "a.osm"), str(tmp_path / "x.idf"))
    (tmp_path / "b.idf").write_text("Version,26.1;\n! Building, commented out;\n")
    with pytest.raises(ValueError, match="Building"):
        own_models.prepare_supplied_model(str(tmp_path / "b.idf"), str(tmp_path / "x.idf"))
    with pytest.raises(FileNotFoundError):
        own_models.prepare_supplied_model(str(tmp_path / "none.idf"), str(tmp_path / "x.idf"))


@needs_idd
def test_set_building_models_makes_a_new_set_and_keeps_the_old(tmp_path):
    mgr = _project(tmp_path)
    base_before = mgr.model_manifest("inv_model", "baseline")
    out = mgr.set_building_models("inv_model", "baseline", {"h1": str(SAMPLE)}, license="CC-BY-4.0",
                                  attribution="Example model, U.S. DOE")
    assert out["model_set_id"] == "baseline__own" and out["num_supplied"] == 1
    assert out["supplied"][0]["building_id"] == "h1" and out["supplied"][0]["outputs_added"]
    assert mgr.model_manifest("inv_model", "baseline") == base_before  # original untouched

    m = mgr.model_manifest("inv_model", "baseline__own")
    h1 = next(e for e in m["models"] if e["building_id"] == "h1")
    assert h1["model_source"] == "supplied" and h1["hvac_system"] == own_models.SUPPLIED_HVAC
    assert h1["supplied_sha256"] == record.sha256_file(str(SAMPLE))
    assert h1["idf_sha256"] == record.sha256_file(h1["idf_path"])
    others = [e for e in m["models"] if e["building_id"] != "h1"]
    assert all(Path(e["idf_path"]).parent.name == "baseline__own" for e in others)
    assert {e["idf_sha256"] for e in others} <= {e["idf_sha256"] for e in base_before["models"]}
    assert m["recipe"]["op"] == "set_building_models" and m["based_on"] == "baseline"
    assert "Example model, U.S. DOE" in m["attribution"]

    with pytest.raises(KeyError, match="nope"):
        mgr.set_building_models("inv_model", "baseline", {"nope": str(SAMPLE)}, new_model_set_id="x")
    with pytest.raises(ValueError, match="already exists"):
        mgr.set_building_models("inv_model", "baseline", {"h1": str(SAMPLE)})
    with pytest.raises(ValueError, match="scenario"):
        mgr.set_building_models("inv_model", "baseline__led", {"h1": str(SAMPLE)}, new_model_set_id="y")


@needs_idd
def test_scenarios_leave_supplied_models_unchanged_and_rerun_reproduces(tmp_path):
    mgr = _project(tmp_path)
    mgr.set_building_models("inv_model", "baseline", {"h1": str(SAMPLE)})
    gen = mgr.generate_scenario("inv_model", "baseline__own", {"name": "led", "measures": [
        {"type": "lighting", "reduction": 0.4}], "seed": 1})
    m = mgr.model_manifest("inv_model", gen["model_set_id"])
    h1 = next(e for e in m["models"] if e["building_id"] == "h1")
    assert h1["model_source"] == "supplied" and h1["retrofit"] == []
    assert Path(h1["idf_path"]).read_text() == Path(
        next(e for e in mgr.model_manifest("inv_model", "baseline__own")["models"]
             if e["building_id"] == "h1")["idf_path"]).read_text()
    assert m["adoption"]["supplied_models_unchanged"] == ["h1"]
    assert m["num_models"] == len(m["models"]) == mgr.model_manifest("inv_model", "baseline")["num_models"]

    rec = mgr.project_record("inv_model")
    assert {s["model_set_id"] for s in rec["model_sets"]} >= {"baseline__own", "baseline__own__led"}
    rerun = mgr.rerun_record(rec["record_path"])
    assert rerun["reproduced"], rerun["warnings"]


@pytest.mark.energyplus
@needs_idd
def test_supplied_model_simulates_and_aggregates(tmp_path, monkeypatch):
    from ubem_mcp_server import server
    from ubem_mcp_server.batch_manager import BatchManager

    mgr = _project(tmp_path)
    cfg = mgr.config
    cfg.batch.batch_output_dir = str(tmp_path / "batches")
    monkeypatch.setattr(server, "stock_manager", mgr)
    monkeypatch.setattr(server, "batch_manager", BatchManager(cfg))

    async def run():
        _, own = await server.mcp.call_tool("set_building_models", {
            "stock_id": "inv_model", "model_set_id": "baseline", "building_models": {"h1": str(SAMPLE)}})
        assert own["ok"] and "simulate_model_set" in own["next_steps"][0]
        _, sim = await server.mcp.call_tool("simulate_model_set", {"stock_id": "inv_model",
                                                                   "model_set_id": own["model_set_id"]})
        assert sim["ok"], sim
        batch_id = sim["batch"]["batch_id"]
        deadline = time.time() + 900
        while True:
            _, st = await server.mcp.call_tool("get_batch_status", {"batch_id": batch_id})
            if st["is_complete"]:
                break
            assert time.time() < deadline
            await asyncio.sleep(2)
        assert st["counts"].get("success") == st["total_models"], st
        _, agg = await server.mcp.call_tool("aggregate_stock_results", {"batch_id": batch_id, "make_map": False})
        assert agg["ok"] and agg["coverage"]["buildings_with_results"] == st["total_models"]
        return agg

    agg = asyncio.run(run())
    rows = {r["building_id"]: r for r in _csv(Path(agg["files"]["buildings_csv"]))}
    assert rows["h1"]["hvac_system"] == own_models.SUPPLIED_HVAC
    assert float(rows["h1"]["floor_area_m2"]) == pytest.approx(232.26, rel=0.01)  # the supplied model's own area


def _csv(path):
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))
