"""
Benchmark how UBEM-MCP's GIS-to-results workflow scales with the number of
buildings and EnergyPlus worker processes.

For each (number of buildings, workers) pair, a nested random subset of the
stock's buildings goes through the same steps the MCP tools run:
model generation, a background EnergyPlus batch, and results aggregation.
Each step is timed, and the memory of the running EnergyPlus processes is
sampled every half second. Per-building simulation times are written with each
model's size (floor area, zones, shading surfaces) to show what drives them.

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/benchmark_scaling.py --osm-bbox 32.228 -110.942 32.238 -110.930 \
        --weather-file <weather.epw> --sizes 10 25 50 100 --workers 4 --out benchmark/
    uv run python scripts/benchmark_scaling.py --geojson stock.geojson ... --workers 1 2 4 --sizes 20
"""

import argparse
import asyncio
import csv
import json
import os
import platform
import random
import statistics
import sys
import time
from pathlib import Path

from ubem_mcp_server.batch_manager import BatchManager
from ubem_mcp_server.config import Config
from ubem_mcp_server.urban.stock_manager import StockManager


class EnergyPlusMemory:
    """Samples the resident memory of running EnergyPlus processes (Linux /proc)."""

    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self.peak_total_mb = 0.0
        self.peak_process_mb = 0.0
        self.peak_processes = 0

    def sample(self):
        rss = []
        page_mb = os.sysconf("SC_PAGE_SIZE") / 2**20
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/comm") as f:
                    if not f.read().strip().lower().startswith("energyplus"):
                        continue
                with open(f"/proc/{pid}/statm") as f:
                    rss.append(int(f.read().split()[1]) * page_mb)
            except (OSError, IndexError, ValueError):
                continue
        if rss:
            self.peak_total_mb = max(self.peak_total_mb, sum(rss))
            self.peak_process_mb = max(self.peak_process_mb, max(rss))
            self.peak_processes = max(self.peak_processes, len(rss))


def _memory_gb():
    try:
        with open("/proc/meminfo") as f:
            return round(int(f.readline().split()[1]) / 2**20, 1)
    except (OSError, IndexError, ValueError):
        return None


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            return next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")), None)
    except OSError:
        return None


async def simulate(bm: BatchManager, manifest, out_dir: Path, workers: int):
    mem = EnergyPlusMemory()
    sub = bm.submit_batch([{"idf_path": m["idf_path"], "label": m["building_id"]} for m in manifest["models"]],
                          weather_file=manifest["weather_file"], max_workers=workers,
                          output_directory=str(out_dir))
    while not bm.get_status(sub["batch_id"])["is_complete"]:
        mem.sample()
        await asyncio.sleep(mem.interval_s)
    return bm.get_results(sub["batch_id"]), mem


def main(argv=None):
    ap = argparse.ArgumentParser(description="Benchmark UBEM-MCP scaling")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--osm-bbox", type=float, nargs=4, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    src.add_argument("--geojson")
    ap.add_argument("--weather-file", required=True)
    ap.add_argument("--climate-zone")
    ap.add_argument("--sizes", type=int, nargs="+", default=[10, 25, 50, 100])
    ap.add_argument("--workers", type=int, nargs="+", default=[4])
    ap.add_argument("--hvac", choices=["template", "ideal"], default="template")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.paths.output_dir = str(args.out / "outputs")
    cfg.batch.batch_output_dir = str(args.out / "outputs" / "batches")
    sm, bm = StockManager(cfg), BatchManager(cfg)

    t0 = time.perf_counter()
    stock = (sm.fetch_osm(bbox=args.osm_bbox, stock_id="bench") if args.osm_bbox
             else sm.load_from_file(args.geojson, stock_id="bench"))
    intake_s = time.perf_counter() - t0
    order = sorted(b.building_id for b in sm.get("bench").buildings)
    random.Random(args.seed).shuffle(order)
    print(f"stock: {stock['num_buildings']} buildings modeled, {stock['num_excluded']} excluded, intake {intake_s:.1f} s")

    environment = {"cpu_count": os.cpu_count(), "memory_gb": _memory_gb(), "platform": platform.platform(),
                   "processor": _cpu_model(), "python": platform.python_version()}
    runs, per_building = [], []
    for workers in args.workers:
        for n in args.sizes:
            if n > len(order):
                print(f"skipping n={n}: the stock has only {len(order)} buildings")
                continue
            set_id = f"n{n}_w{workers}"
            t0 = time.perf_counter()
            gen = sm.generate_models("bench", climate_zone=args.climate_zone, weather_file=args.weather_file,
                                     hvac=args.hvac, building_ids=order[:n], model_set_id=set_id)
            modelgen_s = time.perf_counter() - t0
            manifest = sm.model_manifest("bench", set_id)
            t0 = time.perf_counter()
            res, mem = asyncio.run(simulate(bm, manifest, Path(gen["model_directory"]) / "runs", workers))
            simulation_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            agg = sm.aggregate_results("bench", set_id, outputs={m["label"]: m for m in res["models"]},
                                       results_id=res["batch_id"], make_map=False)
            aggregate_s = time.perf_counter() - t0

            durations = {m["label"]: m.get("duration_seconds") for m in res["models"]}
            for m in manifest["models"]:
                b = sm.get("bench").get(m["building_id"])
                per_building.append({"n": n, "workers": workers, "building_id": m["building_id"],
                                     "archetype": m["archetype"], "floor_area_m2": round(b.gross_floor_area_m2, 1),
                                     "zones": m["zones"], "floors_modeled": m["floors_modeled"],
                                     "shading_surfaces": m["shading_surfaces"], "windows": m["windows"],
                                     "simulation_s": durations.get(m["building_id"])})
            ok = [d for d in durations.values() if d]
            run = {"n": n, "workers": workers, "models": gen["num_models"], "succeeded": res["counts"].get("success", 0),
                   "modelgen_s": round(modelgen_s, 1), "simulation_s": round(simulation_s, 1),
                   "aggregate_s": round(aggregate_s, 1),
                   "total_s": round(modelgen_s + simulation_s + aggregate_s, 1),
                   "buildings_per_hour": round(3600 * n / (modelgen_s + simulation_s + aggregate_s), 1),
                   "per_building_sim_mean_s": round(statistics.mean(ok), 1) if ok else None,
                   "per_building_sim_median_s": round(statistics.median(ok), 1) if ok else None,
                   "per_building_sim_max_s": round(max(ok), 1) if ok else None,
                   "peak_energyplus_memory_mb": round(mem.peak_total_mb),
                   "peak_process_memory_mb": round(mem.peak_process_mb),
                   "peak_energyplus_processes": mem.peak_processes,
                   "floor_area_with_results_m2": round(agg["coverage"]["floor_area_with_results_m2"])}
            runs.append(run)
            print(json.dumps(run))

    (args.out / "scaling.json").write_text(json.dumps(
        {"environment": environment, "intake_s": round(intake_s, 1), "stock": stock, "hvac": args.hvac,
         "runs": runs}, indent=1, default=str))
    for name, rows in (("scaling.csv", runs), ("per_building.csv", per_building)):
        if rows:
            with open(args.out / name, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
