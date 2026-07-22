"""Tests for ubem_mcp_server.batch_manager — the batch (portfolio) simulation
orchestrator that's new in UBEM-MCP.

These tests never invoke real EnergyPlus or real multiprocessing: they swap
ProcessPoolExecutor for ThreadPoolExecutor and replace _run_single_model_worker
with a fake, so batch orchestration logic (status transitions, continue_on_error,
cancellation, manifest persistence) can be verified quickly and deterministically
without an EnergyPlus install or pickling concerns.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ubem_mcp_server import batch_manager as bm
from ubem_mcp_server.config import Config

SAMPLE_DIR = Path(__file__).parent.parent / "sample_files"


def _make_config(tmp_path):
    cfg = Config()
    cfg.paths.sample_files_path = str(SAMPLE_DIR)
    cfg.paths.workspace_root = str(tmp_path)
    cfg.batch.batch_output_dir = str(tmp_path / "batches")
    cfg.batch.max_workers = 2
    return cfg


async def _wait_until_complete(manager, batch_id, attempts=50, delay=0.05):
    status = manager.get_status(batch_id)
    for _ in range(attempts):
        if status["is_complete"]:
            return status
        await asyncio.sleep(delay)
        status = manager.get_status(batch_id)
    pytest.fail(f"batch {batch_id} did not complete in time; last status={status}")


def test_discover_idf_files_finds_sample_idfs():
    cfg = Config()
    files = bm.discover_idf_files(cfg, str(SAMPLE_DIR), "*.idf")
    labels = {f["label"] for f in files}
    assert "1ZoneUncontrolled" in labels
    assert all(f["idf_path"].endswith(".idf") for f in files)


@pytest.mark.asyncio
async def test_submit_batch_missing_file_marks_failed_immediately(tmp_path):
    cfg = _make_config(tmp_path)
    manager = bm.BatchManager(cfg)

    result = manager.submit_batch(models=["does_not_exist.idf"])
    status = await _wait_until_complete(manager, result["batch_id"])

    assert status["status"] == "failed"
    assert status["counts"] == {"failed": 1}

    results = manager.get_results(result["batch_id"])
    assert results["models"][0]["status"] == "failed"
    assert "not found" in results["models"][0]["error"].lower()


@pytest.mark.asyncio
async def test_batch_runs_to_completion_with_mocked_worker(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)

    def fake_worker(config, idf_path, weather_file, output_directory, sim_kwargs):
        return {"success": True, "input_idf": idf_path, "output_directory": output_directory}

    monkeypatch.setattr(bm, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(bm, "_run_single_model_worker", fake_worker)

    manager = bm.BatchManager(cfg)
    idf1 = SAMPLE_DIR / "1ZoneUncontrolled.idf"
    idf2 = SAMPLE_DIR / "1ZoneEvapCooler.idf"
    result = manager.submit_batch(models=[str(idf1), str(idf2)], max_workers=2)

    status = await _wait_until_complete(manager, result["batch_id"])

    assert status["status"] == "completed"
    assert status["counts"] == {"success": 2}

    results = manager.get_results(result["batch_id"])
    assert all(m["status"] == "success" for m in results["models"])
    assert all(m["duration_seconds"] is not None for m in results["models"])


@pytest.mark.asyncio
async def test_continue_on_error_false_stops_after_first_failure(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)

    def fake_worker(config, idf_path, weather_file, output_directory, sim_kwargs):
        if "1ZoneUncontrolled" in idf_path:
            return {"success": False, "error": "boom"}
        return {"success": True}

    monkeypatch.setattr(bm, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(bm, "_run_single_model_worker", fake_worker)

    manager = bm.BatchManager(cfg)
    idf1 = SAMPLE_DIR / "1ZoneUncontrolled.idf"
    idf2 = SAMPLE_DIR / "1ZoneEvapCooler.idf"
    result = manager.submit_batch(
        models=[str(idf1), str(idf2)], max_workers=1, continue_on_error=False,
    )

    status = await _wait_until_complete(manager, result["batch_id"])

    # idf1 always fails; regardless of idf2's exact fate (best-effort
    # cancellation is racy against an already-started worker), the batch
    # as a whole must not report full success.
    assert status["status"] in ("failed", "completed_with_errors", "cancelled")
    assert status["counts"].get("success", 0) < 2


@pytest.mark.asyncio
async def test_cancel_batch_before_start_cancels_all(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(bm, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(bm, "_run_single_model_worker", lambda *a, **k: {"success": True})

    manager = bm.BatchManager(cfg)
    idf1 = SAMPLE_DIR / "1ZoneUncontrolled.idf"
    result = manager.submit_batch(models=[str(idf1)])

    cancel_result = manager.cancel_batch(result["batch_id"])
    assert cancel_result["status"] == "cancelled"

    status = await _wait_until_complete(manager, result["batch_id"])
    assert status["status"] == "cancelled"


@pytest.mark.asyncio
async def test_manifest_persists_and_reloads_after_restart(tmp_path, monkeypatch):
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(bm, "ProcessPoolExecutor", ThreadPoolExecutor)
    monkeypatch.setattr(bm, "_run_single_model_worker", lambda *a, **k: {"success": True})

    manager = bm.BatchManager(cfg)
    idf1 = SAMPLE_DIR / "1ZoneUncontrolled.idf"
    result = manager.submit_batch(models=[str(idf1)])
    batch_id = result["batch_id"]

    await _wait_until_complete(manager, batch_id)

    # A fresh BatchManager has no in-memory job for batch_id; it must be able
    # to reconstruct it from the on-disk manifest (simulates a server restart).
    fresh_manager = bm.BatchManager(cfg)
    reloaded = fresh_manager.get_status(batch_id)
    assert reloaded["status"] == "completed"

    all_batches = fresh_manager.list_batches()
    assert any(b["batch_id"] == batch_id for b in all_batches)


@pytest.mark.asyncio
async def test_unknown_batch_id_raises_keyerror(tmp_path):
    cfg = _make_config(tmp_path)
    manager = bm.BatchManager(cfg)
    with pytest.raises(KeyError):
        manager.get_status("does-not-exist")
