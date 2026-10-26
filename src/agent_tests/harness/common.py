"""
Shared parts of the agent tests: workspaces, checks and scoring.

Runs inside the UBEM-MCP environment (it reads what the server wrote). A
check looks only at the server's outputs and the agent's final answer, never
at how the client works, so every client and model is scored the same way.

    python -m agent_tests.harness.common prepare <run_dir> <task_id>
    python -m agent_tests.harness.common check <run_dir> <task_id> <transcript.json>
"""

import csv
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
TASKS = json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))["tasks"]
CLIENT_NOTE = ("Files the user mentions are in the working folder of the UBEM-MCP tools; refer to them by file "
               "name. Answer the user's questions directly and give the numbers they ask for.")


def task(task_id: str) -> Dict[str, Any]:
    for t in TASKS:
        if t["id"] == task_id:
            return t
    raise KeyError(f"Unknown task {task_id!r}; tasks: {[t['id'] for t in TASKS]}")


def prepare(run_dir: Path, task_id: str) -> Path:
    """A fresh workspace with the task's input files."""
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    for name in task(task_id)["files"]:
        shutil.copy(ROOT / "fixtures" / name, run_dir / name)
    return run_dir


# --------------------------------------------------------------------------- #
# Reading the answer
# --------------------------------------------------------------------------- #

_NUM = re.compile(r"(?<![\w.])[-+−]?\d[\d,]*(?:\.\d+)?(?:\s*(?:k|thousand|million|M)\b)?", re.I)


def numbers(text: str) -> List[float]:
    out = []
    for m in _NUM.finditer(text or ""):
        s = m.group(0).replace("−", "-").replace(",", "").strip()
        mult = 1.0
        low = s.lower()
        for suffix, factor in (("thousand", 1e3), ("million", 1e6), ("k", 1e3), ("m", 1e6)):
            if low.endswith(suffix):
                mult, s = factor, s[: -len(suffix)].strip()
                break
        try:
            out.append(float(s) * mult)
        except ValueError:
            continue
    return out


def mentions(text: str, expected: float, rel: float = 0.03, abs_tol: float = 0.0,
             scales=(1, 1e3, 1e-3, 1e6, 1e-6), signless: bool = True) -> bool:
    """Does the answer state `expected` (in any of the usual unit scalings)?"""
    for n in numbers(text):
        for sc in scales:
            v = n * sc
            for e in ((expected, -expected) if signless else (expected,)):
                if abs(v - e) <= max(abs(e) * rel, abs_tol):
                    return True
    return False


# --------------------------------------------------------------------------- #
# Reading the server's outputs
# --------------------------------------------------------------------------- #

def _json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def baseline_summary(out: Path) -> Optional[Dict[str, Any]]:
    best = None
    for p in out.glob("stocks/*/models/*/results/*/summary.json"):
        manifest = p.parents[2] / "manifest.json"
        if manifest.exists() and _json(manifest).get("scenario"):
            continue
        s = _json(p)
        n = (s.get("coverage") or {}).get("buildings_with_results", 0)
        if best is None or n > best[0]:
            best = (n, s)
    return best[1] if best else None


def comparison(out: Path) -> Optional[Dict[str, Any]]:
    hits = sorted(out.glob("stocks/*/models/*/results/*/comparison/comparison.json"), key=lambda p: p.stat().st_mtime)
    return _json(hits[-1]) if hits else None


def scenario_summary(out: Path) -> Optional[Dict[str, Any]]:
    """The latest aggregated results of a scenario model set (one derived by retrofit measures)."""
    hits = [p for p in out.glob("stocks/*/models/*/results/*/summary.json")
            if (p.parents[2] / "manifest.json").exists() and _json(p.parents[2] / "manifest.json").get("scenario")]
    hits.sort(key=lambda p: p.stat().st_mtime)
    return _json(hits[-1]) if hits else None


def district_pair(out: Path) -> Optional[Dict[str, Any]]:
    """Baseline and scenario district results, from a comparison or, if the agent aggregated
    the two model sets separately instead, from the two aggregated results."""
    c = comparison(out)
    if c:
        d = c["district"]
        peaks = d["peak_kw"]["electricity"]
        return {"baseline_kwh": d["baseline_kwh"], "scenario_kwh": d["scenario_kwh"],
                "baseline_peak_hour": peaks["baseline"]["hour_of_year"],
                "scenario_peak_hour": peaks["scenario"]["hour_of_year"]}
    b, s = baseline_summary(out), scenario_summary(out)
    if not (b and s):
        return None
    return {"baseline_kwh": b["district"]["total_kwh"], "scenario_kwh": s["district"]["total_kwh"],
            "baseline_peak_hour": b["district"]["peak_kw"]["electricity"]["hour_of_year"],
            "scenario_peak_hour": s["district"]["peak_kw"]["electricity"]["hour_of_year"]}


def measured_comparison(out: Path) -> Optional[Dict[str, Any]]:
    hits = sorted(out.glob("stocks/*/measured/*/comparison/*/summary.json"), key=lambda p: p.stat().st_mtime)
    return _json(hits[-1]) if hits else None


def data_stock(out: Path) -> Optional[Dict[str, Any]]:
    for p in out.glob("stocks/*/meta.json"):
        m = _json(p)
        if (m.get("summary") or {}).get("mode") == "data":
            return m
    return None


def resstock_expected(out: Path) -> Dict[str, float]:
    """Expected ResStock numbers, from the agent's downloads or fetched here."""
    from ubem_mcp_server.config import Config
    from ubem_mcp_server.urban.stock_manager import StockManager

    cfg = Config()
    cfg.paths.output_dir = str(out)
    m = StockManager(cfg)
    base = m.fetch_reference("resstock", "AZ", "04019")["dataset_id"]
    upg = m.fetch_reference("resstock", "AZ", "04019", upgrade=4)["dataset_id"]
    q = m.query_reference(base, filters={"building_type": "Single-Family Detached"})["overall"]
    s_sfd = m.query_reference(upg, baseline_dataset_id=base,
                              filters={"building_type": "Single-Family Detached"})["overall"]
    s_all = m.query_reference(upg, baseline_dataset_id=base)["overall"]
    return {"count": q["buildings_represented"], "eui": q["eui_kwh_m2"], "eui_median": q["eui_kwh_m2_p25_p50_p75"][1],
            "savings_sfd": s_sfd["site_energy_savings_percent"], "savings_all": s_all["site_energy_savings_percent"]}


# --------------------------------------------------------------------------- #
# Checks: name -> (outputs dir, final answer) -> (passed, detail)
# --------------------------------------------------------------------------- #

def _c_district_simulated(out, ans):
    s = baseline_summary(out)
    n = (s or {}).get("coverage", {}).get("buildings_with_results", 0)
    return n >= 6, f"{n} buildings with results"


def _c_answer_district_total(out, ans):
    s = baseline_summary(out)
    if not s:
        return False, "no results"
    total = s["district"]["total_kwh"]
    return mentions(ans, total, rel=0.03), f"expected {total / 1000:.1f} MWh"


def _c_answer_peak(out, ans):
    s = baseline_summary(out)
    try:
        kw = s["district"]["peak_kw"]["electricity"]["kw"]
    except (TypeError, KeyError):
        return False, "no peak in results"
    return mentions(ans, kw, rel=0.05), f"expected {kw:.0f} kW"


def _c_mentions_assumptions(out, ans):
    return bool(re.search(r"assum|default|inferred|guess|not known|unknown|missing", ans, re.I)), ""


def _c_scenario_compared(out, ans):
    c = comparison(out)
    return c is not None, "comparison found" if c else "no comparison"


def _c_answer_scenario_savings(out, ans):
    c = comparison(out)
    if not c:
        return False, "no comparison"
    pct = c["district"]["savings_pct"]
    return mentions(ans, pct, rel=0.0, abs_tol=1.0, scales=(1,)), f"expected {pct:.1f}%"


def _c_no_simulation(out, ans):
    models = list(out.glob("stocks/*/models/*/manifest.json"))
    return not models, f"{len(models)} model sets"


def _c_data_stock_complete(out, ans):
    m = data_stock(out)
    n = (m or {}).get("summary", {}).get("num_buildings")
    return n == 10, f"data stock with {n} structures"


def _c_export_with_parcel(out, ans):
    for p in out.glob("stocks/*/exports/*.csv"):
        with open(p, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if any(h.upper() == "PARCEL" for h in header):
            return True, p.name
    return False, "no CSV export with a PARCEL column"


def _c_answer_structure_count(out, ans):
    return mentions(ans, 10, rel=0.0, scales=(1,)), "expected 10"


def _c_answer_known_year_share(out, ans):
    m = data_stock(out)
    if not m:
        return False, "no data stock"
    share = m["summary"]["known_share"]["year_built"]
    ok = mentions(ans, share * 100, rel=0.0, abs_tol=2.0, scales=(1,)) or \
        mentions(ans, share, rel=0.0, abs_tol=0.02, scales=(1,)) or \
        bool(re.search(rf"\b{round(share * 10)}\s*(of|out of|/)\s*10\b", ans))
    return ok, f"expected {share:.0%}"


def _c_mentions_personal(out, ans):
    return bool(re.search(r"owner|mailing|MAIL_ADDR|personal|privacy|names?\b", ans, re.I)), ""


def _c_measured_compared(out, ans):
    s = measured_comparison(out)
    n = (s or {}).get("meters_compared", 0)
    return n >= 6, f"{n} meters compared"


def _c_answer_measured_error(out, ans):
    s = measured_comparison(out)
    if not s:
        return False, "no comparison"
    pct = s["district"][s["main_quantity"]]["percent_error"]
    return mentions(ans, pct, rel=0.0, abs_tol=1.0, scales=(1,)), f"expected {pct:.1f}%"


def _c_mentions_weather(out, ans):
    return bool(re.search(r"weather|typical (meteorological )?year|TMY", ans, re.I)), ""


def _c_resstock_fetched(out, ans):
    ok = (out / "reference_data" / "resstock_az_g0400190_upgrade0" / "meta.json").exists()
    return ok, "ResStock Pima County baseline downloaded" if ok else "not downloaded"


def _c_answer_sfd_count(out, ans):
    e = resstock_expected(out)
    return mentions(ans, e["count"], rel=0.02), f"expected {e['count']:.0f}"


def _c_answer_sfd_eui(out, ans):
    e = resstock_expected(out)
    ok = any(mentions(ans, v * f, rel=0.04, scales=(1,)) for v in (e["eui"], e["eui_median"])
             for f in (1.0, 0.316998, 1 / 10.7639))  # kWh/m2, kBtu/ft2, kWh/ft2
    return ok, f"expected {e['eui']} (median {e['eui_median']}) kWh/m2"


def _c_answer_heat_pump_savings(out, ans):
    e = resstock_expected(out)
    ok = any(mentions(ans, v, rel=0.0, abs_tol=1.5, scales=(1,)) for v in (e["savings_sfd"], e["savings_all"]))
    return ok, f"expected {e['savings_sfd']}% (all homes {e['savings_all']}%)"


def _campus_expected():
    return _json(ROOT / "fixtures" / "campus_section_4_1_expected.json")


def _c_campus_reproduced(out, ans):
    """The agent's baseline and scenario equal the published Section 3.1 run (to 0.1%)."""
    d, e = district_pair(out), _campus_expected()
    if not d:
        return False, "no baseline and scenario results"
    ok = all(abs(d[k] - e[k]) <= 0.001 * e[k] for k in ("baseline_kwh", "scenario_kwh"))
    ok = ok and all(d[f"{w}_peak_hour"] == e["peak_electricity_kw"][w]["hour_of_year"]
                    for w in ("baseline", "scenario"))
    return ok, (f"baseline {d['baseline_kwh'] / 1e6:.3f} vs {e['baseline_kwh'] / 1e6:.3f} GWh, "
                f"scenario {d['scenario_kwh'] / 1e6:.3f} vs {e['scenario_kwh'] / 1e6:.3f} GWh")


def _c_answer_campus_numbers(out, ans):
    e = _campus_expected()
    wanted = {"baseline GWh": (e["baseline_kwh"] / 1e6, 0.02), "scenario GWh": (e["scenario_kwh"] / 1e6, 0.02),
              "gas change %": (e["natural_gas_change_pct"], 0.02),
              "scenario peak MW": (e["peak_electricity_kw"]["scenario"]["kw"] / 1e3, 0.03)}
    missing = [k for k, (v, rel) in wanted.items() if not mentions(ans, v, rel=rel)]
    return not missing, "missing: " + ", ".join(missing) if missing else "all stated"


CHECKS: Dict[str, Callable[[Path, str], Any]] = {
    "campus_reproduced": _c_campus_reproduced, "answer_campus_numbers": _c_answer_campus_numbers,
    "district_simulated": _c_district_simulated, "answer_district_total": _c_answer_district_total,
    "answer_peak_electricity": _c_answer_peak, "mentions_assumptions": _c_mentions_assumptions,
    "scenario_compared": _c_scenario_compared, "answer_scenario_savings": _c_answer_scenario_savings,
    "no_simulation": _c_no_simulation, "data_stock_complete": _c_data_stock_complete,
    "export_with_parcel": _c_export_with_parcel, "answer_structure_count": _c_answer_structure_count,
    "answer_known_year_share": _c_answer_known_year_share, "mentions_personal_columns": _c_mentions_personal,
    "measured_compared": _c_measured_compared, "answer_measured_district_error": _c_answer_measured_error,
    "mentions_weather_year": _c_mentions_weather, "resstock_fetched": _c_resstock_fetched,
    "answer_sfd_count": _c_answer_sfd_count, "answer_sfd_eui": _c_answer_sfd_eui,
    "answer_heat_pump_savings": _c_answer_heat_pump_savings,
}


def score(run_dir: Path, task_id: str, transcript: Dict[str, Any]) -> Dict[str, Any]:
    out = run_dir / "outputs"
    ans = transcript.get("final_answer") or ""
    results = {}
    for name in task(task_id)["checks"]:
        try:
            ok, detail = CHECKS[name](out, ans)
        except Exception as e:  # noqa: BLE001 - a broken check is a failed check, with the reason
            ok, detail = False, f"check error: {type(e).__name__}: {e}"
        results[name] = {"passed": bool(ok), "detail": detail}
    calls = transcript.get("tool_calls") or []
    ubem_calls = [c for c in calls if c.get("server", "ubem") == "ubem"]
    return {
        "task": task_id, "client": transcript.get("client"), "model": transcript.get("model"),
        "passed": all(r["passed"] for r in results.values()),
        "checks_passed": sum(r["passed"] for r in results.values()), "checks_total": len(results),
        "checks": results,
        "tool_calls": len(ubem_calls), "failed_tool_calls": sum(1 for c in ubem_calls if c.get("ok") is False),
        "distinct_tools": sorted({c["name"] for c in ubem_calls}),
        "used_guide": any(c["name"] == "get_workflow_guide" for c in ubem_calls) or bool(transcript.get("used_guide")),
        "non_ubem_tool_calls": len(calls) - len(ubem_calls),
        "duration_s": transcript.get("duration_s"), "turns": transcript.get("turns"), "usage": transcript.get("usage"),
        "error": transcript.get("error"),
    }


def main(argv: List[str]) -> int:
    cmd = argv[0]
    if cmd == "prepare":
        print(prepare(Path(argv[1]), argv[2]))
        return 0
    if cmd == "check":
        transcript = json.loads(Path(argv[3]).read_text(encoding="utf-8"))
        print(json.dumps(score(Path(argv[1]), argv[2], transcript), indent=1))
        return 0
    if cmd == "prompt":
        print(task(argv[1])["prompt"])
        return 0
    raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
