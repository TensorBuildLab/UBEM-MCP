"""
Check that the worked examples' results still hold for the current code.

For each example's output folder, write a project record of every stock (the
recorded inputs and a hash of each model that was simulated), then rebuild the
stocks from that record and regenerate every model set with the current code
(`rerun_project_record` with simulate=false). EnergyPlus is deterministic, so a
model set whose regenerated models all match the simulated ones gives the same
results without simulating again; any model that differs is listed.

Usage (inside the dev container, from src/):
    uv run python scripts/verify_examples.py outputs/case_study:case_study \
        outputs/examples/measured/outputs:lqa_model,lqa_model_fuel --out outputs/paper/verify.json
"""

import argparse
import json
import sys
from pathlib import Path


def check(output_dir: Path, stock_ids):
    from ubem_mcp_server.api import UBEM

    u = UBEM(workspace=str(output_dir), output_dir=str(output_dir), raise_on_error=False)
    report = {}
    for i, sid in enumerate(stock_ids):
        rec = u.export_project_record(stock_id=sid)
        if not rec.get("ok"):
            report[sid] = {"error": rec.get("error")}
            continue
        rerun = u.rerun_project_record(record_path=rec["record_path"], id_suffix=f"_verify{i}", simulate=False)
        report[sid] = {
            "record": rec["record_path"], "record_warnings": rec.get("warnings"),
            "reproduced": rerun.get("reproduced"),
            "warnings": rerun.get("warnings"),
            "model_sets": [{k: ms.get(k) for k in ("model_set_id", "num_models", "identical", "different",
                                                     "different_buildings", "models_identical")}
                           for ms in rerun.get("model_sets") or []],
            "error": rerun.get("error"),
        }
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="output_dir:stock_id[,stock_id...]")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    full = {}
    for t in a.targets:
        folder, _, ids = t.partition(":")
        full[folder] = check(Path(folder).resolve(), [s for s in ids.split(",") if s])
    text = json.dumps(full, indent=1, default=str)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8")
    print(text)
    bad = [sid for f in full.values() for sid, r in f.items() if not r.get("reproduced")]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
