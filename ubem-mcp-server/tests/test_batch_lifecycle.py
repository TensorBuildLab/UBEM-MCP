"""Batches and the processes they start: cancel stops runs in progress, a
stopping server stops its batches and records them as cancelled, a batch left
'running' by a dead server is reported as cancelled, and the stdio server
exits, stopping its EnergyPlus runs, when its client disconnects.

A stand-in `energyplus` that only sleeps keeps these tests fast; POSIX only."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ubem_mcp_server import batch_manager as bm
from ubem_mcp_server.config import Config

pytestmark = pytest.mark.skipif(os.name != "posix", reason="uses a shell-script stand-in for EnergyPlus")

SAMPLE_DIR = Path(__file__).parent.parent / "sample_files"
IDF = str(SAMPLE_DIR / "1ZoneUncontrolled.idf")


def _fake_energyplus(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "energyplus"
    exe.write_text("#!/bin/sh\nsleep 60 &\nwait\n")  # a helper child, as EnergyPlus starts ExpandObjects
    exe.chmod(0o755)
    (folder / "Energy+.idd").write_text("! stand-in IDD\n")
    return exe


def _config(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = str(tmp_path / "outputs")
    cfg.paths.workspace_root = str(tmp_path)
    cfg.paths.sample_files_path = str(SAMPLE_DIR)
    cfg.batch.batch_output_dir = str(tmp_path / "outputs" / "batches")
    cfg.energyplus.executable_path = str(_fake_energyplus(tmp_path / "fakeplus"))
    cfg.energyplus.idd_path = ""
    return cfg


async def _until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.1)


def _sleepers():
    """Live `sleep 60` processes (the stand-in's helper), from /proc."""
    found = []
    for d in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
            state = next((ln.split()[1] for ln in (d / "status").read_text().splitlines() if ln.startswith("State:")), "")
        except OSError:
            continue
        if cmd == "sleep 60" and state != "Z":
            found.append(d.name)
    return found


@pytest.mark.asyncio
async def test_cancel_stops_runs_in_progress(tmp_path):
    manager = bm.BatchManager(_config(tmp_path))
    batch = manager.submit_batch(models=[IDF] * 3, max_workers=2)
    bid = batch["batch_id"]
    await _until(lambda: len(bm._RUNNING) == 2)
    start = time.monotonic()
    result = manager.cancel_batch(bid)
    assert result["running_models_stopped"] == 2
    await _until(lambda: manager.get_status(bid)["is_complete"])
    assert time.monotonic() - start < 15
    status = manager.get_status(bid)
    assert status["status"] == "cancelled" and status["counts"] == {"cancelled": 3}
    await _until(lambda: not bm._RUNNING and not _sleepers())  # helper children stopped too


@pytest.mark.asyncio
async def test_shutdown_stops_runs_and_records_cancelled(tmp_path):
    cfg = _config(tmp_path)
    manager = bm.BatchManager(cfg)
    bid = manager.submit_batch(models=[IDF] * 2, max_workers=1)["batch_id"]
    await _until(lambda: len(bm._RUNNING) == 1)
    assert manager.shutdown("server stopped in a test") == 1
    assert not bm._RUNNING
    on_disk = json.loads((Path(cfg.batch.batch_output_dir) / bid / "manifest.json").read_text())
    assert on_disk["status"] == "cancelled" and on_disk["error"] == "server stopped in a test"
    assert {m["status"] for m in on_disk["models"]} == {"cancelled"}


def test_batch_left_running_by_a_dead_server_reads_as_cancelled(tmp_path):
    cfg = _config(tmp_path)
    folder = Path(cfg.batch.batch_output_dir) / "deadbeef0001"
    folder.mkdir(parents=True)
    job = bm.BatchJob(batch_id="deadbeef0001", created_at="2026-10-25T10:00:00", status="running", max_workers=1,
                      continue_on_error=True, output_directory=str(folder),
                      models=[bm.ModelSpec(input_idf_path=IDF, status="running"),
                              bm.ModelSpec(input_idf_path=IDF, status="success")])
    from dataclasses import asdict
    (folder / "manifest.json").write_text(json.dumps(asdict(job)))
    status = bm.BatchManager(cfg).get_status("deadbeef0001")
    assert status["status"] == "cancelled" and status["is_complete"]
    assert status["counts"] == {"cancelled": 1, "success": 1}


def _rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def _read(proc, want_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == want_id:
            return msg
    raise AssertionError(f"no response {want_id}")


def test_stdio_server_exits_and_stops_runs_when_client_disconnects(tmp_path):
    exe = _fake_energyplus(tmp_path / "fakeplus")
    env = {**os.environ, "UBEM_WORKSPACE_ROOT": str(tmp_path), "EPLUS_IDD_PATH": str(exe.parent / "Energy+.idd"),
           "UBEM_LOG_LEVEL": "WARNING", "MCP_TRANSPORT": "stdio"}
    proc = subprocess.Popen([sys.executable, "-m", "ubem_mcp_server", "serve"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, cwd=tmp_path)
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}}})
        _read(proc, 1)
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "run_batch_simulation", "arguments": {"models": [IDF, IDF], "max_workers": 2}}})
        reply = _read(proc, 2)
        assert "batch_id" in json.dumps(reply)
        deadline = time.monotonic() + 20
        while len(_sleepers()) < 2:
            assert time.monotonic() < deadline, "runs did not start"
            time.sleep(0.2)
        proc.stdin.close()  # the client goes away
        proc.wait(timeout=30)
        deadline = time.monotonic() + 15
        while _sleepers():
            assert time.monotonic() < deadline, "EnergyPlus runs outlived the server"
            time.sleep(0.2)
        manifests = list((tmp_path / "outputs" / "batches").glob("*/manifest.json"))
        assert manifests and json.loads(manifests[0].read_text())["status"] == "cancelled"
    finally:
        if proc.poll() is None:
            proc.kill()
