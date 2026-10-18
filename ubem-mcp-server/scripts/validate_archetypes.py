"""
Validate UBEM-MCP's generated archetype models against the DOE reference
simulations they are derived from.

For each case, a model is generated from a rectangular footprint with the
reference building's floor area, floor count and floor-to-floor height,
simulated with EnergyPlus for the given weather file, and its site energy use
intensity (EUI) by fuel and end use is compared with the reference model's
published results for the same climate zone (Commercial Reference Buildings
v1.3_5.0; IECC single-family prototype).

The generated models are deliberately simpler than the reference models (a
rectangular footprint, core/perimeter zoning, one space type per building, no
refrigeration, elevators or kitchen exhaust), so the comparison measures what
a UBEM built from footprints can reproduce. Because refrigeration is not
modeled, the EUI is compared both in total and without refrigeration.

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/validate_archetypes.py --epw <weather.epw> --climate-zone 2B \
        --out validation/2B [--cases all|core] [--workers 4]
"""

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from shapely.geometry import box

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban.idd import load_idd
from ubem_mcp_server.urban.library import _commercial, resolve
from ubem_mcp_server.urban.modelgen import ModelBuilder, epw_location
from ubem_mcp_server.urban.results import annual_end_uses, find_sql
from ubem_mcp_server.urban.stock import Building

VINTAGES = ("pre1980", "1980_2004", "post2004")

# Published footprint dimensions, width x depth in m (Deru et al. 2011; IECC
# single-family prototype, 2 x 110.4 m2). Other reference buildings are not
# rectangular, so they get a 1.5:1 rectangle of the same floor-plate area.
FOOTPRINTS = {
    "small_office": (27.69, 18.46),
    "medium_office": (49.91, 33.27),
    "large_office": (73.11, 48.74),
    "standalone_retail": (54.25, 42.29),
    "strip_mall": (91.44, 22.86),
    "warehouse": (100.58, 48.07),
    "midrise_apartment": (46.33, 16.84),
    "small_hotel": (54.86, 18.29),
    "quick_service_restaurant": (15.24, 15.24),
    "full_service_restaurant": (22.61, 22.61),
    "single_family": (12.19, 9.06),
}
ASPECT_RATIO = 1.5

CORE = [("small_office", "pre1980"), ("small_office", "post2004"), ("medium_office", "pre1980"),
        ("medium_office", "post2004"), ("standalone_retail", "post2004"), ("warehouse", "post2004"),
        ("midrise_apartment", "post2004"), ("single_family", "post2004")]


def build_cases(which: str):
    """(archetype, vintage, (width, depth), above-ground floors, floor-to-floor) for each case."""
    lib = _commercial()["archetypes"]
    pairs = CORE if which == "core" else (
        [(a, v) for a in sorted(lib) for v in VINTAGES] + [("single_family", "post2004")])
    cases = []
    for archetype, vintage in pairs:
        if archetype == "single_family":
            cases.append((archetype, vintage, FOOTPRINTS[archetype], 2, 2.74))
            continue
        form = lib[archetype]["vintages"][vintage]["form"]
        floors = int(form["num_floors"])
        if archetype in FOOTPRINTS:
            dims = FOOTPRINTS[archetype]
        else:
            # The reference floor area includes the basement, which is not modeled.
            plate = form["floor_area_m2"] / (floors + (1 if form["has_basement"] else 0))
            depth = math.sqrt(plate / ASPECT_RATIO)
            dims = (round(ASPECT_RATIO * depth, 2), round(depth, 2))
        cases.append((archetype, vintage, dims, floors, float(form["floor_to_floor_m"])))
    return cases


def _eui_without(eu, end_use):
    return sum(v for f in eu.values() for k, v in f.items() if k != end_use)


def _run(job):
    (archetype, vintage, (w, d), floors, f2f), epw, cz, out, idd_path = job
    b = Building(f"{archetype}_{vintage}", box(0, 0, w, d), archetype, None, vintage, floors, f2f, floors * f2f)
    params = resolve(archetype, vintage, cz)
    model_dir = Path(out) / b.building_id
    model_dir.mkdir(parents=True, exist_ok=True)
    idf = model_dir / "in.idf"
    t0 = time.perf_counter()
    text = ModelBuilder(load_idd(idd_path), params).build(b, [], epw_location(epw))
    idf.write_text(text)
    t1 = time.perf_counter()
    # ExpandObjects writes into the working directory, so each run gets its own.
    subprocess.run(["energyplus", "-w", epw, "-d", str(model_dir), "-x", str(idf)],
                   capture_output=True, text=True, cwd=model_dir)
    t2 = time.perf_counter()
    err = (model_dir / "eplusout.err").read_text(errors="replace") if (model_dir / "eplusout.err").exists() else ""
    sql = find_sql(str(model_dir))
    base = {"case": b.building_id, "archetype": archetype, "vintage": vintage, "climate_zone": cz,
            "hvac_system": params.hvac_system, "footprint_m": [w, d], "floors": floors}
    if not sql or "EnergyPlus Completed Successfully" not in err:
        return {**base, "error": "simulation failed; see " + str(model_dir / "eplusout.err")}
    res = annual_end_uses(sql)
    area = res["conditioned_area_m2"]
    sim = {f: {k: round(v / area, 2) for k, v in eu.items()} for f, eu in res["end_uses_mj"].items()}
    ref = params.reference_eui_mj_m2 or {}
    sim_total = _eui_without(sim, None)
    ref_total = _eui_without(ref, None) if ref else None
    ref_no_refr = _eui_without(ref, "refrigeration") if ref else None
    sim_no_refr = _eui_without(sim, "refrigeration")
    return {
        **base, "floor_area_m2": round(area, 1),
        "zones": text.count("\nZone,"),
        "sim_eui_mj_m2": round(sim_total, 1),
        "ref_eui_mj_m2": ref_total and round(ref_total, 1),
        "diff_pct": round((sim_total / ref_total - 1) * 100, 1) if ref_total else None,
        "sim_eui_no_refrigeration": round(sim_no_refr, 1),
        "ref_eui_no_refrigeration": ref_no_refr and round(ref_no_refr, 1),
        "diff_no_refrigeration_pct": round((sim_no_refr / ref_no_refr - 1) * 100, 1) if ref_no_refr else None,
        "severe_errors": err.count("** Severe"),
        "warnings": err.count("** Warning"),
        "modelgen_s": round(t1 - t0, 2),
        "simulation_s": round(t2 - t1, 1),
        "sim_end_uses": sim,
        "ref_end_uses": ref,
    }


def summarize(rows):
    ok = [r for r in rows if "error" not in r and r.get("ref_eui_no_refrigeration")]
    diffs = [r["diff_no_refrigeration_pct"] for r in ok]
    if not diffs:
        return {"cases": len(rows), "compared": 0}
    within = lambda x: sum(1 for d in diffs if abs(d) <= x)  # noqa: E731
    return {
        "cases": len(rows), "compared": len(ok), "failed": sum(1 for r in rows if "error" in r),
        "mean_abs_diff_pct": round(statistics.mean(abs(d) for d in diffs), 1),
        "median_abs_diff_pct": round(statistics.median(abs(d) for d in diffs), 1),
        "mean_diff_pct": round(statistics.mean(diffs), 1),
        "within_10_pct": within(10), "within_20_pct": within(20), "within_30_pct": within(30),
        "cv_rmse_pct": round(100 * math.sqrt(statistics.mean(
            (r["sim_eui_no_refrigeration"] - r["ref_eui_no_refrigeration"]) ** 2 for r in ok))
            / statistics.mean(r["ref_eui_no_refrigeration"] for r in ok), 1),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate generated archetype models against DOE reference results")
    ap.add_argument("--epw", required=True)
    ap.add_argument("--climate-zone", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cases", choices=["all", "core"], default="all")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    idd_path = Config().energyplus.idd_path
    cases = build_cases(args.cases)
    # Longest first, so the slow models do not finish last.
    cases.sort(key=lambda c: -(c[2][0] * c[2][1] * c[3]))
    # EnergyPlus runs inside each case's directory, so pass absolute paths.
    epw, out = str(Path(args.epw).resolve()), str(args.out.resolve())
    jobs = [(c, epw, args.climate_zone, out, idd_path) for c in cases]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(_run, jobs))
    rows.sort(key=lambda r: (r["archetype"], VINTAGES.index(r["vintage"])))
    args.out.mkdir(parents=True, exist_ok=True)
    summary = {"climate_zone": args.climate_zone, "weather": Path(args.epw).name, **summarize(rows)}
    (args.out / "validation.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=1))

    cols = ["case", "archetype", "vintage", "climate_zone", "hvac_system", "floor_area_m2", "floors", "zones",
            "sim_eui_mj_m2", "ref_eui_mj_m2", "diff_pct", "sim_eui_no_refrigeration", "ref_eui_no_refrigeration",
            "diff_no_refrigeration_pct", "severe_errors", "modelgen_s", "simulation_s", "error"]
    with open(args.out / "validation.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    lines = [f"Climate zone {args.climate_zone}, weather {Path(args.epw).name}", "",
             "| Case | Floor area (m2) | Simulated EUI (MJ/m2) | Reference EUI (MJ/m2) | Difference | "
             "Difference w/o refrigeration | Run time (s) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['case']} | | | | {r['error']} | | |")
            continue
        fmt = lambda x: f"{x:+.1f}%" if x is not None else "n/a"  # noqa: E731
        lines.append(f"| {r['case']} | {r['floor_area_m2']:.0f} | {r['sim_eui_mj_m2']:.0f} | "
                     f"{r['ref_eui_mj_m2'] or 'n/a'} | {fmt(r['diff_pct'])} | "
                     f"{fmt(r['diff_no_refrigeration_pct'])} | {r['simulation_s']:.0f} |")
    lines += ["", "Summary: " + json.dumps(summary)]
    table = "\n".join(lines)
    (args.out / "validation.md").write_text(table + "\n")
    print(table)
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
