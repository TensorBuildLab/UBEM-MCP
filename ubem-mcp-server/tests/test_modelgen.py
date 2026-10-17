"""Tests for archetype parameters, model geometry and IDF generation.

Parameter and geometry tests need no EnergyPlus; IDF generation reads the
installed EnergyPlus's IDD and is skipped without it. Simulating generated
models is covered in test_integration_energyplus.py.
"""
import math
import os
import re

import pytest
from shapely.geometry import Polygon, box

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import library
from ubem_mcp_server.urban.archetypes import ARCHETYPES, VINTAGES
from ubem_mcp_server.urban.modelgen import (
    ModelBuilder, Neighbor, core_perimeter, party_wall_heights, representative_floors,
)
from ubem_mcp_server.urban.stock import Building

IDD_PATH = Config().energyplus.idd_path
needs_idd = pytest.mark.skipif(not os.path.exists(IDD_PATH), reason="EnergyPlus IDD not installed")


def _building(fp, archetype="small_office", vintage="post2004", floors=1, f2f=3.05, bid="b1"):
    return Building(bid, fp, archetype, 2010, vintage, floors, f2f, floors * f2f)


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cz", library.CLIMATE_ZONES)
def test_every_archetype_vintage_resolves_with_expandable_schedules(cz):
    for archetype in ARCHETYPES:
        for vintage in VINTAGES:
            p = library.resolve(archetype, vintage, cz)
            assert p.wall_r > 0 and p.roof_r > 0 and 0 < p.window_shgc < 1
            assert 0 < p.window_to_wall_ratio < 1
            assert p.hvac_system in ("psz", "ptac", "pvav", "vav")
            for role, name in p.schedule_roles.items():
                if name:
                    periods = library.expand_rules(p.schedules[name]["rules"])
                    assert all(set(days) == set(library.DAY_TYPES) for _, days in periods), (archetype, role)


def test_climate_zone_normalization():
    assert library.normalize_climate_zone(" 2b ") == "2B"
    assert library.normalize_climate_zone("7A") == "7"
    with pytest.raises(ValueError):
        library.normalize_climate_zone("9Z")


def test_vintage_changes_envelope_and_hvac():
    old = library.resolve("small_office", "pre1980", "2B")
    new = library.resolve("small_office", "post2004", "2B")
    assert old.lighting_w_m2 > new.lighting_w_m2
    assert old.window_shgc > new.window_shgc
    assert old.infiltration_ach > new.infiltration_ach


def test_hvac_family_follows_vintage_system_type():
    # pre-1980 medium office is single-zone constant volume; later vintages are packaged VAV
    assert library.resolve("medium_office", "pre1980", "2B").hvac_system == "psz"
    pvav = library.resolve("medium_office", "post2004", "2B")
    assert (pvav.hvac_system, pvav.reheat_coil, pvav.ahu_heating_coil) == ("pvav", "Electric", "Gas")
    assert library.resolve("large_office", "post2004", "2B").hvac_system == "vav"
    assert library.resolve("small_hotel", "pre1980", "2B").hvac_system == "ptac"


def test_single_family_vintages_and_assumptions():
    new = library.resolve("single_family", "post2004", "2B")
    old = library.resolve("single_family", "pre1980", "2B")
    assert new.sector == old.sector == "residential"
    assert old.ela_cm2_per_m2_envelope > new.ela_cm2_per_m2_envelope
    assert old.wall_r < new.wall_r
    assert any("mid-rise apartment" in a for a in old.assumptions)
    assert new.reference_eui_mj_m2 and not old.reference_eui_mj_m2


def test_highrise_uses_midrise_reference_with_note():
    p = library.resolve("highrise_apartment", "post2004", "4A")
    assert any("midrise_apartment" in a for a in p.assumptions)


def test_combined_hot_water_schedule_is_normalized_and_scaled():
    p = library.resolve("single_family", "post2004", "2B")
    combined = p.schedules["_combined_hot_water"]
    assert max(max(r["values"]) for r in combined["rules"]) == pytest.approx(1.0)
    raw_total = sum(h["peak_flow_m3_s"] for h in library._residential()["loads"]["hot_water"])
    area = library._residential()["conditioned_area_m2"]
    assert p.hot_water_m3_s_per_m2 == pytest.approx(raw_total * combined["peak_scale"] / area)


def test_expand_rules_other_fills_remaining_days():
    rules = [{"through": "12/31", "days": ["WD"], "values": [1.0] * 24},
             {"through": "12/31", "days": ["Other"], "values": [0.0] * 24}]
    (_, days), = library.expand_rules(rules)
    assert days["Monday"][0] == 1.0 and days["Sunday"][0] == 0.0 and days["Holiday"][0] == 0.0


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_representative_floors():
    assert representative_floors(1) == [(0, 1, True, True)]
    assert representative_floors(3) == [(0, 1, True, False), (1, 1, False, False), (2, 1, False, True)]
    assert representative_floors(12) == [(0, 1, True, False), (1, 10, False, False), (11, 1, False, True)]
    assert sum(m for _, m, _, _ in representative_floors(12)) == 12


def test_core_perimeter_rectangle_preserves_area():
    fp = box(0, 0, 50, 30)
    zones = core_perimeter(fp)
    assert zones is not None and len(zones) == 5
    assert sum(z.area for z in zones) == pytest.approx(fp.area, rel=1e-6)
    core = zones[-1]
    assert core.area == pytest.approx((50 - 2 * 4.57) * (30 - 2 * 4.57), rel=1e-6)


def test_core_perimeter_l_shape_and_fallbacks():
    l_shape = Polygon([(0, 0), (40, 0), (40, 15), (15, 15), (15, 40), (0, 40)])
    zones = core_perimeter(l_shape)
    assert zones is not None and len(zones) == 7
    assert sum(z.area for z in zones) == pytest.approx(l_shape.area, rel=1e-6)
    assert core_perimeter(box(0, 0, 10, 10)) is None  # too small for a core
    circle_ish = Polygon([(20 * math.cos(a), 20 * math.sin(a)) for a in [i * 2 * math.pi / 30 for i in range(30)]])
    assert core_perimeter(circle_ish) is None  # too many edges


def test_party_walls_detected_only_for_touching_neighbors():
    fp = box(0, 0, 10, 10)
    walls = party_wall_heights(fp, [Neighbor(box(10, 0, 20, 10), 7.0), Neighbor(box(0, 30, 10, 40), 20.0)])
    assert len(walls) == 1 and list(walls.values()) == [7.0]


# --------------------------------------------------------------------------- #
# IDF generation
# --------------------------------------------------------------------------- #

def _count(text, cls):
    return len(re.findall(rf"^{re.escape(cls)},", text, re.M))


@needs_idd
def test_small_office_idf_structure():
    from ubem_mcp_server.urban.idd import load_idd

    b = _building(box(0, 0, 27.69, 18.46))
    mb = ModelBuilder(load_idd(IDD_PATH), library.resolve("small_office", "post2004", "2B"))
    text = mb.build(b)
    assert mb.info.zoning == "core_perimeter" and mb.info.num_zones == 5
    assert _count(text, "Zone") == 5
    assert _count(text, "HVACTemplate:System:Unitary") == 5
    assert _count(text, "FenestrationSurface:Detailed") == 4
    # interzone walls are paired: every Surface boundary names an existing wall
    walls = set(re.findall(r"^\s+(F\d+_\w+_Wall\d+),", text, re.M))
    refs = re.findall(r"^\s+Surface,\s*!- Outside Boundary Condition\n\s+(F\d+_\w+_Wall\d+),", text, re.M)
    assert refs and set(refs) <= walls
    assert text.count("Version,") == 1


@needs_idd
def test_tall_building_uses_multiplier_and_vav_plant():
    from ubem_mcp_server.urban.idd import load_idd

    b = _building(box(0, 0, 70, 45), "large_office", "post2004", floors=12, f2f=3.96)
    mb = ModelBuilder(load_idd(IDD_PATH), library.resolve("large_office", "post2004", "4A"))
    text = mb.build(b)
    assert mb.info.floors_modeled == [(1, 1), (2, 10), (12, 1)]
    assert re.search(r"^\s+10,\s*!- Multiplier", text, re.M)
    for cls in ("HVACTemplate:Plant:Chiller", "HVACTemplate:Plant:Boiler", "HVACTemplate:System:VAV"):
        assert _count(text, cls) >= 1


@needs_idd
def test_row_house_party_wall_and_shading():
    from ubem_mcp_server.urban.idd import load_idd

    b = _building(box(0, 0, 8, 12), "single_family", "pre1980", floors=2, f2f=3.0)
    neighbors = [Neighbor(box(8, 0, 16, 12), 6.0), Neighbor(box(0, 20, 10, 30), 9.0)]
    mb = ModelBuilder(load_idd(IDD_PATH), library.resolve("single_family", "pre1980", "2B"))
    text = mb.build(b, neighbors)
    assert mb.info.party_walls == 1
    assert text.count("Party wall") >= 3  # construction + one adiabatic wall per floor
    assert mb.info.shading_surfaces == 4  # the detached neighbor's four walls; the attached one is skipped
    assert _count(text, "ZoneInfiltration:EffectiveLeakageArea") == 2


@needs_idd
def test_ideal_loads_option():
    from ubem_mcp_server.urban.idd import load_idd

    mb = ModelBuilder(load_idd(IDD_PATH), library.resolve("strip_mall", "1980_2004", "3B"), hvac="ideal")
    text = mb.build(_building(box(0, 0, 60, 20)))
    assert _count(text, "HVACTemplate:Zone:IdealLoadsAirSystem") == mb.info.num_zones
    assert "HVACTemplate:System" not in text


def test_detailed_footprint_is_simplified_within_area_tolerance():
    from ubem_mcp_server.urban.modelgen import MAX_FOOTPRINT_VERTICES, model_footprint

    # A 40 x 25 m building traced with ~180 vertices (wobbly OSM-style outline).
    pts = []
    for i in range(180):
        t = i / 180
        if t < 0.25:
            x, y = 160 * t, 0
        elif t < 0.5:
            x, y = 40, 100 * (t - 0.25)
        elif t < 0.75:
            x, y = 40 - 160 * (t - 0.5), 25
        else:
            x, y = 0, 25 - 100 * (t - 0.75)
        pts.append((x + 0.05 * math.sin(i), y + 0.05 * math.cos(i)))
    fp = Polygon(pts)
    model_fp, zones, note = model_footprint(fp, commercial=True)
    assert len(model_fp.exterior.coords) - 1 <= MAX_FOOTPRINT_VERTICES
    assert model_fp.area == pytest.approx(fp.area, rel=0.05)
    assert zones is not None  # simplification lets core/perimeter zoning succeed
    assert note and "simplified" in note
    # Residential footprints are simplified only as far as the vertex cap needs.
    res_fp, res_zones, _ = model_footprint(box(0, 0, 10, 8), commercial=False)
    assert res_zones is None and res_fp.equals(box(0, 0, 10, 8))
