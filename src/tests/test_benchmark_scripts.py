"""Tests for the validation and benchmark scripts that do not need EnergyPlus."""
import csv
import json

import pytest

from scripts import plot_benchmarks, validate_archetypes
from ubem_mcp_server.urban.library import _commercial


def test_validation_cases_cover_the_reference_library():
    cases = validate_archetypes.build_cases("all")
    lib = _commercial()["archetypes"]
    assert len(cases) == 3 * len(lib) + 1
    for archetype, vintage, (w, d), floors, f2f in cases:
        if archetype == "single_family":
            continue
        form = lib[archetype]["vintages"][vintage]["form"]
        modeled = w * d * floors
        above_ground = form["floor_area_m2"] * floors / (floors + (1 if form["has_basement"] else 0))
        assert modeled == pytest.approx(above_ground, rel=0.01), archetype
        assert f2f == form["floor_to_floor_m"]
    assert len(validate_archetypes.build_cases("core")) == 8


def _case(cz, archetype, vintage, sim, ref, refr=0.0):
    return {"case": f"{archetype}_{vintage}", "climate_zone": cz, "archetype": archetype, "vintage": vintage,
            "sim_eui_no_refrigeration": sim, "ref_eui_no_refrigeration": ref,
            "diff_no_refrigeration_pct": round((sim / ref - 1) * 100, 1),
            "sim_end_uses": {"electricity": {"cooling": sim / 2, "interior_lighting": sim / 2}},
            "ref_end_uses": {"electricity": {"cooling": ref / 2, "interior_lighting": ref / 2,
                                             **({"refrigeration": refr} if refr else {})}}}


def test_summary_statistics():
    rows = [_case("2B", "a", "post2004", 110, 100), _case("2B", "b", "post2004", 75, 100),
            _case("2B", "c", "post2004", 100, 100), {"case": "x", "error": "failed"}]
    s = validate_archetypes.summarize(rows)
    assert (s["compared"], s["failed"]) == (3, 1)
    assert s["mean_abs_diff_pct"] == pytest.approx(35 / 3, abs=0.1)
    assert (s["within_10_pct"], s["within_20_pct"], s["within_30_pct"]) == (2, 2, 3)
    assert s["cv_rmse_pct"] == pytest.approx(100 * ((100 + 625) / 3) ** 0.5 / 100, abs=0.1)


def test_end_use_grouping():
    g = plot_benchmarks._grouped({"electricity": {"fans": 2, "pumps": 1, "exterior_lighting": 3},
                                  "natural_gas": {"water_systems": 4, "something_new": 5}})
    assert g["Fans & pumps"] == 3 and g["Lighting"] == 3 and g["Water heating"] == 4 and g["Equipment"] == 5


def test_plots_are_written(tmp_path):
    vdir = tmp_path / "v2B"
    vdir.mkdir()
    rows = [_case("2B", "small_office", "post2004", 500, 520), _case("2B", "supermarket", "post2004", 900, 800, 300)]
    (vdir / "validation.json").write_text(json.dumps({"summary": {}, "cases": rows}))
    bdir = tmp_path / "bench"
    bdir.mkdir()
    (bdir / "scaling.json").write_text(json.dumps({"runs": [
        {"n": 5, "workers": 2, "total_s": 60}, {"n": 10, "workers": 2, "total_s": 110}]}))
    with open(bdir / "per_building.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["zones", "shading_surfaces", "simulation_s"])
        w.writeheader()
        w.writerows([{"zones": 2, "shading_surfaces": 10, "simulation_s": 12.0},
                     {"zones": 5, "shading_surfaces": 40, "simulation_s": 30.0}])
    out = tmp_path / "fig"
    assert plot_benchmarks.main(["--validation", str(vdir), "--scaling", str(bdir), "--out", str(out)]) == 0
    assert {p.name for p in out.iterdir()} == {"validation_parity.png", "validation_end_uses_2B.png", "scaling.png"}
