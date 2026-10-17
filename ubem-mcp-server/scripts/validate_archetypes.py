"""
Validate UBEM-MCP's generated archetype models against the DOE reference
simulations they are derived from.

For each case, a model is generated from a footprint with the reference
building's own dimensions and floor count, simulated with EnergyPlus for the
given weather file, and its site energy use intensity (EUI) by fuel and end
use is compared with the reference model's published results for the same
climate zone (Commercial Reference Buildings v1.3_5.0; IECC 2006 single-family
prototype).

Usage (inside the dev container, from ubem-mcp-server/):
    uv run python scripts/validate_archetypes.py --epw <weather.epw> --climate-zone 2B \
        --out validation/2B [--workers 8]
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from shapely.geometry import box

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban.idd import load_idd
from ubem_mcp_server.urban.library import resolve
from ubem_mcp_server.urban.modelgen import ModelBuilder, epw_location
from ubem_mcp_server.urban.results import annual_end_uses, find_sql
from ubem_mcp_server.urban.stock import Building

# (archetype, vintage, footprint width x depth (m), floors, floor-to-floor (m)),
# using the reference buildings' published dimensions (Deru et al. 2011) and
# the IECC single-family prototype (2 x 110.4 m2).
CASES = [
    ("small_office", "pre1980", (27.69, 18.46), 1, 3.05),
    ("small_office", "post2004", (27.69, 18.46), 1, 3.05),
    ("medium_office", "pre1980", (49.91, 33.27), 3, 3.96),
    ("medium_office", "post2004", (49.91, 33.27), 3, 3.96),
    ("standalone_retail", "post2004", (54.25, 42.29), 1, 6.10),
    ("warehouse", "post2004", (100.58, 48.07), 1, 8.53),
    ("midrise_apartment", "post2004", (46.33, 16.84), 4, 3.05),
    ("single_family", "post2004", (12.19, 9.06), 2, 2.74),
]


def _run(job):
    (archetype, vintage, (w, d), floors, f2f), epw, cz, out, idd_path = job
    b = Building(f"{archetype}_{vintage}", box(0, 0, w, d), archetype, None, vintage, floors, f2f, floors * f2f)
    params = resolve(archetype, vintage, cz)
    model_dir = Path(out) / b.building_id
    model_dir.mkdir(parents=True, exist_ok=True)
    idf = model_dir / "in.idf"
    idf.write_text(ModelBuilder(load_idd(idd_path), params).build(b, [], epw_location(epw)))
    # ExpandObjects writes into the working directory, so each run gets its own.
    subprocess.run(["energyplus", "-w", epw, "-d", str(model_dir), "-x", str(idf)],
                   capture_output=True, text=True, cwd=model_dir)
    err = (model_dir / "eplusout.err").read_text(errors="replace") if (model_dir / "eplusout.err").exists() else ""
    sql = find_sql(str(model_dir))
    if not sql or "EnergyPlus Completed Successfully" not in err:
        return {"case": b.building_id, "error": "simulation failed; see " + str(model_dir / "eplusout.err")}
    res = annual_end_uses(sql)
    area = res["conditioned_area_m2"]
    sim = {f: {k: v / area for k, v in eu.items()} for f, eu in res["end_uses_mj"].items()}
    ref = params.reference_eui_mj_m2
    sim_total = sum(sum(x.values()) for x in sim.values())
    ref_total = sum(sum(x.values()) for x in ref.values()) if ref else None
    return {
        "case": b.building_id, "archetype": archetype, "vintage": vintage, "floor_area_m2": round(area, 1),
        "sim_eui_mj_m2": round(sim_total, 1), "ref_eui_mj_m2": ref_total and round(ref_total, 1),
        "diff_pct": round((sim_total / ref_total - 1) * 100, 1) if ref_total else None,
        "severe_errors": err.count("** Severe"),
        "sim_end_uses": {f: {k: round(v, 1) for k, v in eu.items()} for f, eu in sim.items()},
        "ref_end_uses": ref,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate generated archetype models against DOE reference results")
    ap.add_argument("--epw", required=True)
    ap.add_argument("--climate-zone", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)

    idd_path = Config().energyplus.idd_path
    jobs = [(c, args.epw, args.climate_zone, str(args.out), idd_path) for c in CASES]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(_run, jobs))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "validation.json").write_text(json.dumps(rows, indent=1))

    lines = [f"Climate zone {args.climate_zone}, weather {Path(args.epw).name}", "",
             "| Case | Floor area (m2) | Simulated EUI (MJ/m2) | Reference EUI (MJ/m2) | Difference |",
             "|---|---:|---:|---:|---:|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['case']} | | | | {r['error']} |")
        else:
            diff = f"{r['diff_pct']:+.1f}%" if r["diff_pct"] is not None else "n/a"
            lines.append(f"| {r['case']} | {r['floor_area_m2']:.0f} | {r['sim_eui_mj_m2']:.0f} | "
                         f"{r['ref_eui_mj_m2'] or 'n/a'} | {diff} |")
    table = "\n".join(lines)
    (args.out / "validation.md").write_text(table + "\n")
    print(table)
    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
