"""Integration tests that run the real EnergyPlus engine.

Unlike test_batch_manager.py (which mocks the worker and swaps in threads),
these tests exercise the real ProcessPoolExecutor path and real EnergyPlus
simulations. They are skipped automatically when no EnergyPlus install is
found, and can be selected or excluded explicitly with ``-m energyplus`` /
``-m "not energyplus"``.
"""
import asyncio
import os
from pathlib import Path

import pytest

from ubem_mcp_server.batch_manager import BatchManager
from ubem_mcp_server.config import Config

SAMPLE_DIR = Path(__file__).parent.parent / "sample_files"
EPW = str(SAMPLE_DIR / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw")

_cfg = Config()
pytestmark = [
    pytest.mark.energyplus,
    pytest.mark.skipif(
        not os.path.exists(_cfg.energyplus.idd_path),
        reason=f"EnergyPlus not installed (IDD not found at {_cfg.energyplus.idd_path!r})",
    ),
]


def _make_config(tmp_path):
    cfg = Config()
    cfg.paths.sample_files_path = str(SAMPLE_DIR)
    cfg.paths.output_dir = str(tmp_path / "outputs")
    cfg.batch.batch_output_dir = str(tmp_path / "batches")
    cfg.batch.max_workers = 2
    return cfg


def _err_summary(output_directory, prefix):
    err = Path(output_directory) / f"{prefix}.err"
    assert err.exists(), f"missing {err}"
    return err.read_text(errors="replace")


async def _wait_until_complete(manager, batch_id, timeout=600):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        status = manager.get_status(batch_id)
        if status["is_complete"]:
            return status
        await asyncio.sleep(1)
    pytest.fail(f"batch {batch_id} did not complete within {timeout}s")


async def test_real_batch_runs_models_in_worker_processes(tmp_path):
    cfg = _make_config(tmp_path)
    manager = BatchManager(cfg)

    submitted = manager.submit_batch(
        models=[
            str(SAMPLE_DIR / "1ZoneUncontrolled.idf"),
            {"idf_path": str(SAMPLE_DIR / "1ZoneEvapCooler.idf"), "label": "evap"},
            "does_not_exist.idf",
        ],
        weather_file=EPW,
        continue_on_error=True,
    )
    status = await _wait_until_complete(manager, submitted["batch_id"])

    assert status["status"] == "completed_with_errors"
    assert status["counts"] == {"success": 2, "failed": 1}

    results = manager.get_results(submitted["batch_id"])
    by_label = {m["label"]: m for m in results["models"]}
    for label, prefix in (("1ZoneUncontrolled", "1ZoneUncontrolled"), ("evap", "1ZoneEvapCooler")):
        model = by_label[label]
        assert model["status"] == "success"
        out_dir = Path(model["output_directory"])
        assert (out_dir / f"{prefix}Table.htm").exists()
        assert "EnergyPlus Completed Successfully" in _err_summary(out_dir, prefix)
        assert " 0 Severe Errors" in _err_summary(out_dir, prefix)

    assert by_label["does_not_exist"]["status"] == "failed"

    # The manifest lets a fresh manager (e.g. after a server restart) see the batch.
    reloaded = BatchManager(cfg).get_results(submitted["batch_id"])
    assert reloaded["status"] == "completed_with_errors"
    assert {m["label"]: m["status"] for m in reloaded["models"]} == {
        label: m["status"] for label, m in by_label.items()
    }


async def test_real_batch_leaves_no_temp_files_in_input_directory(tmp_path):
    # eppy's IDF.run() writes a temporary copy next to the input IDF; with
    # several workers sharing one input directory those copies must not leak.
    src = tmp_path / "inputs"
    src.mkdir()
    idf = src / "1ZoneUncontrolled.idf"
    idf.write_bytes((SAMPLE_DIR / "1ZoneUncontrolled.idf").read_bytes())

    cfg = _make_config(tmp_path)
    manager = BatchManager(cfg)
    submitted = manager.submit_batch(
        models=[{"idf_path": str(idf), "label": f"copy{i}"} for i in range(3)],
        weather_file=EPW,
        design_day=True,
        annual=False,
    )
    status = await _wait_until_complete(manager, submitted["batch_id"])

    assert status["counts"] == {"success": 3}
    assert sorted(p.name for p in src.iterdir()) == ["1ZoneUncontrolled.idf"]


async def test_stock_to_models_to_batch(tmp_path):
    """GeoJSON stock -> archetype models -> parallel EnergyPlus runs."""
    import json

    from ubem_mcp_server.urban.results import annual_end_uses, find_sql
    from ubem_mcp_server.urban.stock_manager import StockManager

    lon0, lat0 = -122.39, 37.62
    m_lat, m_lon = 111_320.0, 111_320.0 * 0.7934

    def rect(x, y, w, d):
        ll = lambda a, b: [lon0 + a / m_lon, lat0 + b / m_lat]  # noqa: E731
        return {"type": "Polygon", "coordinates": [[ll(x, y), ll(x + w, y), ll(x + w, y + d), ll(x, y + d), ll(x, y)]]}

    src = tmp_path / "blocks.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": rect(0, 0, 12, 9), "properties": {"id": "house", "building": "house",
                                                                           "building:levels": 2, "start_date": 1975}},
        {"type": "Feature", "geometry": rect(12, 0, 12, 9), "properties": {"id": "twin", "building": "house",
                                                                            "building:levels": 2}},
        {"type": "Feature", "geometry": rect(40, 0, 27.7, 18.5), "properties": {"id": "office", "building": "office",
                                                                                 "start_date": 2008}},
    ]}))
    cfg = _make_config(tmp_path)
    cfg.paths.workspace_root = str(tmp_path)
    sm = StockManager(cfg)
    sm.load_from_file(str(src), stock_id="demo")
    gen = sm.generate_models("demo", "3C", weather_file=EPW, building_ids=["house", "office"], model_set_id="t")
    assert gen["num_models"] == 2 and gen["num_failed"] == 0
    manifest = sm.model_manifest("demo", "t")
    house = next(m for m in manifest["models"] if m["building_id"] == "house")
    assert house["party_walls"] == 1  # shares a wall with "twin"

    manager = BatchManager(cfg)
    submitted = manager.submit_batch(
        models=[{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]],
        weather_file=EPW, metadata={"stock_id": "demo", "model_set_id": "t"})
    status = await _wait_until_complete(manager, submitted["batch_id"], timeout=900)
    assert status["counts"] == {"success": 2}
    assert status["metadata"] == {"stock_id": "demo", "model_set_id": "t"}
    for m in manager.get_results(submitted["batch_id"])["models"]:
        assert " 0 Severe Errors" in _err_summary(m["output_directory"], m["label"])
        res = annual_end_uses(find_sql(m["output_directory"]))
        eui = res["total_mj"] / res["conditioned_area_m2"]
        assert 200 < eui < 1500, (m["label"], eui)

    outputs = {m["label"]: m for m in manager.get_results(submitted["batch_id"])["models"]}
    summary = sm.aggregate_results("demo", "t", outputs=outputs, results_id=submitted["batch_id"], make_map=False)
    assert summary["coverage"]["buildings_with_results"] == 2
    assert summary["district"]["peak_kw"]["electricity"]["kw"] > 0
    assert summary["district"]["total_kwh"] == pytest.approx(
        sum(g["total_kwh"] for g in summary["by_archetype"].values()))
