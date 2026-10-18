"""Collect agent-test scores into results/summary.json and results/summary.md.

    python agent_tests/summarize.py
"""
import json
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def main():
    rows = []
    for p in sorted((RESULTS / "runs").glob("*/score.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        s["run"] = p.parent.name
        rows.append(s)
    groups = defaultdict(list)
    for s in rows:
        groups[(s["client"], s["model"], s["task"])].append(s)
    table = []
    for (client, model, task), runs in sorted(groups.items()):
        n = len(runs)
        table.append({
            "client": client, "model": model, "task": task, "runs": n,
            "passed": sum(r["passed"] for r in runs),
            "checks_passed": sum(r["checks_passed"] for r in runs), "checks_total": sum(r["checks_total"] for r in runs),
            "mean_tool_calls": round(sum(r["tool_calls"] for r in runs) / n, 1),
            "failed_tool_calls": sum(r["failed_tool_calls"] for r in runs),
            "non_ubem_tool_calls": sum(r.get("non_ubem_tool_calls", 0) for r in runs),
            "used_guide": sum(1 for r in runs if r["used_guide"]),
            "mean_duration_s": round(sum(r["duration_s"] or 0 for r in runs) / n),
            "failed_checks": sorted({k for r in runs for k, v in r["checks"].items() if not v["passed"]}),
        })
    (RESULTS / "summary.json").write_text(json.dumps({"by_task": table, "runs": rows}, indent=1), encoding="utf-8")
    lines = ["| Client | Model | Task | Runs passed | Checks | Tool calls (mean) | Failed calls | Guide used | Time (s) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in table:
        lines.append(f"| {r['client']} | {r['model']} | {r['task']} | {r['passed']}/{r['runs']} | "
                     f"{r['checks_passed']}/{r['checks_total']} | {r['mean_tool_calls']} | {r['failed_tool_calls']} | "
                     f"{r['used_guide']}/{r['runs']} | {r['mean_duration_s']} |")
    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
