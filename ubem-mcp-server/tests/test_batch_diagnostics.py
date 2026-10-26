"""Batch failure diagnosis, run timing and progress."""

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ubem_mcp_server import batch_manager as bm
from ubem_mcp_server.config import Config
from ubem_mcp_server.utils import err_parser

from .test_batch_manager import SAMPLE_DIR, _make_config

EPW = str(SAMPLE_DIR / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw")

OK_ERR = """Program Version,EnergyPlus, Version 26.1.0-abc, YMD=2026.10.17 10:00,
   ** Warning ** GetSurfaceData: CAUTION -- Interzone surfaces are occurring in the same zone(s).
   **   ~~~   ** ...use Output:Diagnostics,DisplayExtraWarnings; to show more details.
   ************* Testing Individual Branch Integrity
   ************* EnergyPlus Warmup Error Summary. During Warmup: 0 Warning; 0 Severe Errors.
   ************* EnergyPlus Completed Successfully-- 1 Warning; 0 Severe Errors; Elapsed Time=00hr 00min  2.10sec
"""

FATAL_ERR = """Program Version,EnergyPlus, Version 26.1.0-abc, YMD=2026.10.17 10:00,
   ** Severe  ** Construction="R13WALL", Did not find matching material for layer="MISSING_LAYER".
   **   ~~~   ** in Construction input.
   ** Severe  ** GetSurfaceData: Wall:Exterior="WALL 3" has no valid construction.
   **  Fatal  ** GetSurfaceData: Errors discovered, program terminates.
   ...Summary of Errors that led to program termination:
   ..... Reference severe error count=2
   ************* EnergyPlus Terminated--Fatal Error Detected. 0 Warning; 2 Severe Errors; Elapsed Time=00hr 00min  0.20sec
"""


def test_parse_successful_err():
    s = err_parser.parse_err_text(OK_ERR)
    assert s == {"completed": True, "warnings": 1, "severe": 0, "fatal": False, "messages": []}


def test_parse_fatal_err_keeps_first_messages_with_continuations():
    s = err_parser.parse_err_text(FATAL_ERR)
    assert s["completed"] is False and s["fatal"] and s["severe"] == 2 and s["warnings"] == 0
    assert s["messages"][0] == {"level": "severe", "message": 'Construction="R13WALL", Did not find matching '
                                                               'material for layer="MISSING_LAYER". in Construction input.'}
    assert [m["level"] for m in s["messages"]] == ["severe", "severe", "fatal"]


def test_failures_group_by_cause_across_buildings():
    def failure(label, construction):
        text = FATAL_ERR.replace("R13WALL", construction).replace("WALL 3", f"WALL {label}")
        return {"label": label, "error": "EnergyPlus exited with code 1",
                "err_summary": err_parser.parse_err_text(text)}

    groups = err_parser.group_failures([failure("b1", "WALL_A"), failure("b2", "WALL_B"), failure("b3", "W_C"),
                                        {"label": "b4", "error": "Weather file not found: x.epw"}])
    assert [g["count"] for g in groups] == [3, 1]
    assert groups[0]["examples"] == ["b1", "b2", "b3"]
    assert "construction or material" in groups[0]["hint"]
    assert "weather file" in groups[1]["hint"]


@pytest.mark.asyncio
async def test_status_reports_progress_running_and_diagnosis(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    release = threading.Event()

    def fake_worker(config, idf_path, weather_file, output_directory, sim_kwargs):
        release.wait(5)
        if "Evap" in idf_path:
            return {"success": False, "error": "EnergyPlus exited with code 1", "simulation_seconds": 0.2,
                    "started_at": "2026-10-17T10:00:00", "err_summary": err_parser.parse_err_text(FATAL_ERR)}
        return {"success": True, "simulation_seconds": 12.5, "started_at": "2026-10-17T10:00:00",
                "err_summary": err_parser.parse_err_text(OK_ERR)}

    monkeypatch.setattr(bm, "_run_single_model_worker", fake_worker)
    manager = bm.BatchManager(cfg)
    models = [str(SAMPLE_DIR / "1ZoneUncontrolled.idf")] * 4 + [str(SAMPLE_DIR / "1ZoneEvapCooler.idf")]
    batch = manager.submit_batch(models=models, max_workers=2)
    await asyncio.sleep(0.1)
    running = manager.get_status(batch["batch_id"])
    assert running["counts"] == {"running": 2, "queued": 3}  # only as many running as there are workers
    assert running["progress"]["finished"] == 0
    release.set()
    for _ in range(100):
        status = manager.get_status(batch["batch_id"])
        if status["is_complete"]:
            break
        await asyncio.sleep(0.05)
    assert status["counts"] == {"success": 4, "failed": 1}
    assert status["progress"]["mean_simulation_seconds"] == 12.5
    cause = status["diagnosis"]["failure_causes"][0]
    assert cause["count"] == 1 and cause["hint"] and "material" in cause["example_message"]
    models = manager.get_results(batch["batch_id"])["models"]
    assert {m["duration_seconds"] for m in models if m["status"] == "success"} == {12.5}  # run time, not queue time


def test_executor_uses_threads_not_processes():
    """Worker processes (forkserver, spawn) re-import the caller's main script;
    threads that each start an EnergyPlus process do not."""
    ex = bm._make_executor(2)
    try:
        assert isinstance(ex, ThreadPoolExecutor)
    finally:
        ex.shutdown()


_cfg = Config()


@pytest.mark.energyplus
@pytest.mark.skipif(not os.path.exists(_cfg.energyplus.idd_path), reason="EnergyPlus not installed")
@pytest.mark.asyncio
async def test_real_failure_is_diagnosed(tmp_path):
    broken = tmp_path / "broken.idf"
    text = (SAMPLE_DIR / "1ZoneUncontrolled.idf").read_text()
    broken.write_text(text.replace("R13LAYER;                !- Outside Layer", "MISSING_LAYER;  !- Outside Layer"))
    manager = bm.BatchManager(_make_config(tmp_path))
    batch = manager.submit_batch(models=[str(broken), str(SAMPLE_DIR / "1ZoneUncontrolled.idf")],
                                 weather_file=EPW)
    deadline = time.time() + 300
    while not (status := manager.get_status(batch["batch_id"]))["is_complete"]:
        assert time.time() < deadline
        await asyncio.sleep(0.5)
    assert status["counts"] == {"success": 1, "failed": 1}
    cause = status["diagnosis"]["failure_causes"][0]
    assert "MISSING_LAYER" in cause["example_message"] and cause["hint"]
    by_label = {m["label"]: m for m in manager.get_results(batch["batch_id"])["models"]}
    failed = by_label["broken"]
    assert failed["result"]["err_summary"]["severe"] >= 1 and failed["result"]["err_summary"]["fatal"]
    assert "MISSING_LAYER" in failed["error"]
    ok = by_label["1ZoneUncontrolled"]
    assert ok["result"]["err_summary"]["completed"] is True
    assert 0 < ok["duration_seconds"] < status["progress"]["elapsed_seconds"] + 5


@pytest.mark.asyncio
async def test_get_batch_status_can_wait_for_completion(tmp_path, monkeypatch):
    from ubem_mcp_server import server

    cfg = _make_config(tmp_path)
    release = threading.Event()

    def fake_worker(config, idf_path, weather_file, output_directory, sim_kwargs):
        release.wait(5)
        return {"success": True, "simulation_seconds": 1.0}

    monkeypatch.setattr(bm, "_run_single_model_worker", fake_worker)
    manager = bm.BatchManager(cfg)
    monkeypatch.setattr(server, "batch_manager", manager)
    batch = manager.submit_batch(models=[str(SAMPLE_DIR / "1ZoneUncontrolled.idf")], max_workers=1)

    _, quick = await server.mcp.call_tool("get_batch_status", {"batch_id": batch["batch_id"], "wait_seconds": 1})
    assert quick["is_complete"] is False and "wait_seconds" in quick["next_steps"][0]
    threading.Timer(0.5, release.set).start()
    start = time.monotonic()
    _, done = await server.mcp.call_tool("get_batch_status", {"batch_id": batch["batch_id"], "wait_seconds": 30})
    assert done["is_complete"] and time.monotonic() - start < 10
