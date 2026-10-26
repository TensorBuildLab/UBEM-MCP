"""The Python library from a plain script, as users write it: no
`if __name__ == "__main__":` guard, run from a file and from stdin.

Batch workers once ran as forkserver processes, which re-import the caller's
main script: an unguarded script ran again in every worker, and code read from
stdin failed in every worker while the batch still looked finished. Workers
are threads now; these tests keep it that way with real EnergyPlus runs."""

import json
import os
import subprocess
import sys

import pytest

from ubem_mcp_server.config import Config

from .test_integration_energyplus import EPW
from .test_urban_stock import fc, feature, rect

SCRIPT = """
import json, os
from ubem_mcp_server.api import UBEM

print("START", flush=True)
u = UBEM(workspace=os.environ["WS"], max_workers=2)
u.load_building_stock(geojson_path="pair.geojson", stock_id="pair", default_year=1960, license="CC0-1.0")
run = u.simulate_building_stock(stock_id="pair", model_set_id="base", climate_zone="3C",
                                weather_file=os.environ["EPW"])
status = u.wait(run["batch"]["batch_id"], poll_seconds=1, timeout=600)
agg = u.aggregate_stock_results(batch_id=run["batch"]["batch_id"], make_map=False)
print("RESULT " + json.dumps({"counts": status["counts"], "kwh": agg["district"]["total_kwh"]}), flush=True)
u.close()
"""

_cfg = Config()
pytestmark = [pytest.mark.energyplus,
              pytest.mark.skipif(not os.path.exists(_cfg.energyplus.idd_path), reason="EnergyPlus not installed")]


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pair.geojson").write_text(json.dumps(fc(
        feature(rect(0, 0, 12, 10), building="house", id="h1"),
        feature(rect(40, 0, 12, 10), building="house", id="h2"))))
    return ws


def _check(proc):
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert proc.stdout.count("START") == 1, "the script ran more than once"
    result = json.loads(proc.stdout.split("RESULT ", 1)[1])
    assert result["counts"] == {"success": 2}, (result, proc.stderr[-3000:])
    assert result["kwh"] > 0


@pytest.mark.parametrize("how", ["file", "stdin"])
def test_unguarded_script_runs_a_batch_once(tmp_path, how):
    ws = _workspace(tmp_path)
    env = {**os.environ, "WS": str(ws), "EPW": EPW, "UBEM_LOG_LEVEL": "WARNING"}
    if how == "file":
        script = tmp_path / "study.py"
        script.write_text(SCRIPT)
        proc = subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True,
                              timeout=900)
    else:
        proc = subprocess.run([sys.executable, "-"], input=SCRIPT, cwd=tmp_path, env=env, capture_output=True,
                              text=True, timeout=900)
    _check(proc)
