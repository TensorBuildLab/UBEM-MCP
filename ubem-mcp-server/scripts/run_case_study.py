"""
Run a retrofit case study end to end with the same steps the MCP tools run:
load a building stock, resolve its weather, generate and simulate the
baseline models, then generate, simulate and compare a retrofit scenario.

The default scenario is the electrification package used in the paper:
heat pumps for space heating (COP 3.3), heat-pump water heaters in
single-family houses (COP 3.0) and a 40% lighting power reduction.

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/run_case_study.py --geojson outputs/case_study/campus_source.geojson \
        --default-year 1960 --out outputs/case_study [--workers 4] [--scenario scenario.json]
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from ubem_mcp_server.batch_manager import BatchManager
from ubem_mcp_server.config import Config
from ubem_mcp_server.urban.stock_manager import StockManager

ELECTRIFY = {"name": "electrify", "measures": [
    {"type": "heat_pump", "heating_cop": 3.3},
    {"type": "heat_pump_water_heater", "cop": 3.0, "archetypes": ["single_family"]},
    {"type": "lighting", "reduction": 0.4}]}


async def simulate(sm: StockManager, bm: BatchManager, stock_id: str, model_set_id: str, workers: int):
    manifest = sm.model_manifest(stock_id, model_set_id)
    run_dir = sm.stock_dir(stock_id) / "models" / model_set_id / "runs"
    sub = bm.submit_batch([{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]],
                          weather_file=manifest["weather_file"], max_workers=workers, output_directory=str(run_dir))
    while not bm.get_status(sub["batch_id"])["is_complete"]:
        await asyncio.sleep(5)
    res = bm.get_results(sub["batch_id"])
    print(f"{model_set_id}: {res['status']} {res['counts']}", flush=True)
    return sm.aggregate_results(stock_id, model_set_id, outputs={m["label"]: m for m in res["models"]})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a UBEM-MCP retrofit case study")
    ap.add_argument("--geojson", required=True)
    ap.add_argument("--default-year", type=int)
    ap.add_argument("--stock-id", default="case_study")
    ap.add_argument("--scenario", type=Path, help="scenario JSON (default: the electrification package)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    cfg = Config()
    cfg.paths.output_dir = str(args.out)
    cfg.batch.batch_output_dir = str(args.out / "batches")
    sm, bm = StockManager(cfg), BatchManager(cfg)
    scenario = json.loads(args.scenario.read_text()) if args.scenario else ELECTRIFY
    timings = {}

    t0 = time.perf_counter()
    loader = {"default_year": args.default_year} if args.default_year else {}
    stock = sm.load_from_file(args.geojson, stock_id=args.stock_id, **loader)
    weather = sm.resolve_weather(args.stock_id)
    timings["intake_and_weather_s"] = time.perf_counter() - t0
    print(f"stock: {stock['num_buildings']} buildings, weather {Path(weather['epw']).name}, "
          f"climate zone {weather['library_climate_zone']}", flush=True)

    t0 = time.perf_counter()
    base = sm.generate_models(args.stock_id, model_set_id="baseline")
    timings["baseline_modelgen_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    base_summary = asyncio.run(simulate(sm, bm, args.stock_id, base["model_set_id"], args.workers))
    timings["baseline_simulate_aggregate_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    scen = sm.generate_scenario(args.stock_id, base["model_set_id"], scenario)
    timings["scenario_modelgen_s"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    asyncio.run(simulate(sm, bm, args.stock_id, scen["model_set_id"], args.workers))
    timings["scenario_simulate_aggregate_s"] = time.perf_counter() - t0
    comparison = sm.compare_scenario(args.stock_id, scen["model_set_id"])

    out = {"stock": stock, "weather": weather, "scenario": scenario,
           "timings_s": {k: round(v, 1) for k, v in timings.items()},
           "baseline": base_summary, "comparison": comparison}
    (args.out / "case_study.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: comparison.get(k) for k in ("coverage", "district")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
