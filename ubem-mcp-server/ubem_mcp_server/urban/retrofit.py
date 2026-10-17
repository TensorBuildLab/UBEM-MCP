"""
Retrofit scenarios for a building stock.

A scenario is a list of measures applied to the archetype parameters of the
buildings it targets, before their models are generated; every other input
(geometry, neighbors, weather, schedules) stays identical to the baseline, so
the difference between baseline and scenario results is the measures'
effect. Each model records which measures it received and how.

Scenario specification (JSON):

    {"name": "deep_retrofit",
     "measures": [{"type": "lighting", "target_w_m2": 6.5},
                  {"type": "heat_pump", "heating_cop": 3.3, "vintages": ["pre1980"]}],
     "archetypes": ["single_family"],      # optional: buildings the scenario targets
     "vintages": ["pre1980", "1980_2004"],  # optional
     "building_ids": [...],                 # optional
     "adoption_rate": 0.5,                  # optional: share of targeted buildings retrofitted
     "seed": 1}                             # optional: makes the adopting sample reproducible

Each measure may also carry its own "archetypes" / "vintages" filters.
"""

import copy
import random
import re
from dataclasses import replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .archetypes import ARCHETYPES, VINTAGES
from .library import ArchetypeParams

Spec = Dict[str, Any]


def _frac(spec: Spec, key: str, lo: float = 0.0, hi: float = 0.95) -> float:
    v = spec[key]
    if not isinstance(v, (int, float)) or not lo < v <= hi:
        raise ValueError(f"{spec['type']}: {key} must be in ({lo}, {hi}], got {v!r}")
    return float(v)


def _pos(spec: Spec, key: str, lo: float = 0.0, hi: float = 1e6) -> float:
    v = spec[key]
    if not isinstance(v, (int, float)) or not lo < v <= hi:
        raise ValueError(f"{spec['type']}: {key} must be a number in ({lo}, {hi}], got {v!r}")
    return float(v)


def _one_of(spec: Spec, *keys: str) -> str:
    present = [k for k in keys if spec.get(k) is not None]
    if len(present) != 1:
        raise ValueError(f"{spec['type']}: give exactly one of {', '.join(keys)}")
    return present[0]


# --------------------------------------------------------------------------- #
# Measures: (params, spec) -> (new params, note) or (params, None) if not applicable
# --------------------------------------------------------------------------- #

Result = Tuple[ArchetypeParams, Optional[str]]


def _lighting(p: ArchetypeParams, s: Spec) -> Result:
    key = _one_of(s, "target_w_m2", "reduction")
    new = min(p.lighting_w_m2, _pos(s, key)) if key == "target_w_m2" else p.lighting_w_m2 * (1 - _frac(s, key))
    if new >= p.lighting_w_m2 - 1e-9:
        return p, None
    return replace(p, lighting_w_m2=new), f"lighting {p.lighting_w_m2:.2f} -> {new:.2f} W/m2"


def _plug_loads(p: ArchetypeParams, s: Spec) -> Result:
    r = _frac(s, "reduction")
    if not p.electric_equipment:
        return p, None
    eq = copy.deepcopy(p.electric_equipment)
    before = sum(e["w_per_m2"] for e in eq)
    for e in eq:
        e["w_per_m2"] *= 1 - r
    return replace(p, electric_equipment=eq), f"plug loads {before:.2f} -> {before * (1 - r):.2f} W/m2"


def _insulation(attr: str, label: str):
    def apply(p: ArchetypeParams, s: Spec) -> Result:
        key = _one_of(s, "target_r", "added_r")
        cur = getattr(p, attr)
        new = max(cur, _pos(s, key, hi=20)) if key == "target_r" else cur + _pos(s, key, hi=20)
        if new <= cur + 1e-9:
            return p, None
        return replace(p, **{attr: new}), f"{label} R {cur:.2f} -> {new:.2f} m2K/W"
    return apply


def _windows(p: ArchetypeParams, s: Spec) -> Result:
    u = _pos(s, "u_factor", hi=7)
    if u >= p.window_u - 1e-9:
        return p, None
    shgc = _pos(s, "shgc", hi=0.99) if s.get("shgc") is not None else p.window_shgc
    vt = _pos(s, "vt", hi=0.99) if s.get("vt") is not None else p.window_vt
    return (replace(p, window_u=u, window_shgc=shgc, window_vt=vt),
            f"windows U {p.window_u:.2f} -> {u:.2f} W/m2K, SHGC {p.window_shgc:.2f} -> {shgc:.2f}")


def _air_sealing(p: ArchetypeParams, s: Spec) -> Result:
    r = _frac(s, "reduction", hi=0.9)
    if p.ela_cm2_per_m2_envelope is not None:
        return (replace(p, ela_cm2_per_m2_envelope=p.ela_cm2_per_m2_envelope * (1 - r)),
                f"effective leakage area -{100 * r:.0f}%")
    if p.infiltration_ach:
        return replace(p, infiltration_ach=p.infiltration_ach * (1 - r)), f"infiltration -{100 * r:.0f}%"
    return p, None


def _cool_roof(p: ArchetypeParams, s: Spec) -> Result:
    a = _pos(s, "solar_absorptance", hi=1.0) if s.get("solar_absorptance") is not None else 0.3
    if a >= p.roof_solar_absorptance - 1e-9:
        return p, None
    return replace(p, roof_solar_absorptance=a), f"roof solar absorptance {p.roof_solar_absorptance:.2f} -> {a:.2f}"


def _hvac_efficiency(p: ArchetypeParams, s: Spec) -> Result:
    if s.get("cooling_cop") is None and s.get("heating_efficiency") is None:
        raise ValueError("hvac_efficiency: give cooling_cop and/or heating_efficiency")
    changes, notes = {}, []
    if s.get("cooling_cop") is not None and _pos(s, "cooling_cop", hi=10) > p.cooling_cop:
        changes["cooling_cop"] = float(s["cooling_cop"])
        notes.append(f"cooling COP {p.cooling_cop:.2f} -> {s['cooling_cop']:.2f}")
    if (s.get("heating_efficiency") is not None and p.heating_fuel == "NaturalGas"
            and _pos(s, "heating_efficiency", hi=1.0) > p.heating_efficiency):
        changes["heating_efficiency"] = float(s["heating_efficiency"])
        notes.append(f"gas heating efficiency {p.heating_efficiency:.2f} -> {s['heating_efficiency']:.2f}")
    if not changes:
        return p, None
    return replace(p, **changes), "; ".join(notes)


def _heat_pump(p: ArchetypeParams, s: Spec) -> Result:
    cop = _pos(s, "heating_cop", hi=8) if s.get("heating_cop") is not None else 3.3
    if p.hvac_system not in ("psz", "ptac"):
        return p, None  # central VAV systems are out of scope for this measure
    changes: Dict[str, Any] = {"heat_pump_heating_cop": cop, "heating_fuel": "Electricity"}
    note = f"{p.hvac_system} {p.heating_fuel} heating -> air-source heat pump (heating COP {cop:.2f}"
    if s.get("cooling_cop") is not None and _pos(s, "cooling_cop", hi=10) > p.cooling_cop:
        changes["cooling_cop"] = float(s["cooling_cop"])
        note += f", cooling COP {p.cooling_cop:.2f} -> {s['cooling_cop']:.2f}"
    return replace(p, **changes), note + ", electric backup)"


def _hp_water_heater(p: ArchetypeParams, s: Spec) -> Result:
    cop = _pos(s, "cop", hi=6) if s.get("cop") is not None else 3.0
    if p.hot_water_m3_s_per_m2 <= 0:
        return p, None
    return (replace(p, water_heater_fuel="Electricity", water_heater_efficiency=cop),
            f"{p.water_heater_fuel} water heater -> heat pump water heater (constant COP {cop:.1f}; "
            "ambient-temperature effects not modeled)")


MEASURES: Dict[str, Dict[str, Any]] = {
    "lighting": {"apply": _lighting, "keys": {"target_w_m2", "reduction"}, "params": "target_w_m2 (W/m2) or reduction (0-0.95)",
                 "description": "Lower interior lighting power density, e.g. an LED retrofit"},
    "plug_loads": {"apply": _plug_loads, "keys": {"reduction"}, "params": "reduction (0-0.95)",
                   "description": "Reduce electric plug and process loads"},
    "wall_insulation": {"apply": _insulation("wall_r", "wall"), "keys": {"target_r", "added_r"}, "params": "target_r or added_r (m2K/W)",
                        "description": "Raise the exterior-wall assembly R-value"},
    "roof_insulation": {"apply": _insulation("roof_r", "roof"), "keys": {"target_r", "added_r"}, "params": "target_r or added_r (m2K/W)",
                        "description": "Raise the roof assembly R-value"},
    "windows": {"apply": _windows, "keys": {"u_factor", "shgc", "vt"}, "params": "u_factor (W/m2K), optional shgc, vt",
                "description": "Replace windows (applied only where the new U-factor is lower)"},
    "air_sealing": {"apply": _air_sealing, "keys": {"reduction"}, "params": "reduction (0-0.9)",
                    "description": "Reduce infiltration (design ACH or effective leakage area)"},
    "cool_roof": {"apply": _cool_roof, "keys": {"solar_absorptance"}, "params": "optional solar_absorptance (default 0.3)",
                  "description": "Reflective roof membrane"},
    "hvac_efficiency": {"apply": _hvac_efficiency, "keys": {"cooling_cop", "heating_efficiency"}, "params": "cooling_cop and/or heating_efficiency",
                        "description": "Higher-efficiency like-for-like HVAC equipment"},
    "heat_pump": {"apply": _heat_pump, "keys": {"heating_cop", "cooling_cop"}, "params": "optional heating_cop (default 3.3), cooling_cop",
                  "description": "Replace packaged single-zone / PTAC heating with an air-source heat pump "
                                 "(electric backup); not applied to central VAV systems"},
    "heat_pump_water_heater": {"apply": _hp_water_heater, "keys": {"cop"}, "params": "optional cop (default 3.0)",
                               "description": "Electrify service hot water with a heat pump water heater "
                                              "(modeled as constant COP)"},
}

_SCENARIO_KEYS = {"name", "description", "measures", "archetypes", "vintages", "building_ids", "adoption_rate", "seed"}
_FILTER_KEYS = {"type", "archetypes", "vintages"}


def list_measures() -> List[Dict[str, str]]:
    return [{"type": k, "parameters": v["params"], "description": v["description"]} for k, v in MEASURES.items()]


def validate_scenario(spec: Spec) -> Spec:
    """Check a scenario specification and return a normalized copy."""
    if not isinstance(spec, dict):
        raise ValueError("A scenario must be a JSON object")
    unknown = set(spec) - _SCENARIO_KEYS
    if unknown:
        raise ValueError(f"Unknown scenario keys: {sorted(unknown)}")
    name = str(spec.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
        raise ValueError("Scenario name must be 1-48 letters, digits, '_' or '-'")
    measures = spec.get("measures")
    if not measures or not isinstance(measures, list):
        raise ValueError("A scenario needs a non-empty list of measures")
    out = copy.deepcopy(spec)
    for m in out["measures"]:
        if not isinstance(m, dict) or m.get("type") not in MEASURES:
            raise ValueError(f"Unknown measure {m!r}; available: {', '.join(MEASURES)}")
        extra = set(m) - _FILTER_KEYS - MEASURES[m["type"]]["keys"]
        if extra:
            raise ValueError(f"measure {m['type']}: unknown parameters {sorted(extra)}; "
                             f"expected {MEASURES[m['type']]['params']}")
        _check_filters(m, f"measure {m['type']}")
    _check_filters(out, "scenario")
    rate = out.setdefault("adoption_rate", 1.0)
    if not isinstance(rate, (int, float)) or not 0 < rate <= 1:
        raise ValueError(f"adoption_rate must be in (0, 1], got {rate!r}")
    out.setdefault("seed", 0)
    # Dry-run every measure on a representative archetype to surface parameter errors now.
    from .library import resolve
    for m in out["measures"]:
        try:
            MEASURES[m["type"]]["apply"](resolve("small_office", "pre1980", "4A"), m)
        except KeyError as e:
            raise ValueError(f"measure {m['type']}: missing parameter {e}; expected {MEASURES[m['type']]['params']}") from None
    return out


def _check_filters(obj: Spec, where: str) -> None:
    for key, valid in (("archetypes", ARCHETYPES), ("vintages", VINTAGES)):
        vals = obj.get(key)
        if vals is None:
            continue
        if isinstance(vals, str):
            vals = obj[key] = [vals]
        bad = [v for v in vals if v not in valid]
        if bad:
            raise ValueError(f"{where}: unknown {key} {bad}")


def _matches(obj: Spec, archetype: str, vintage: str) -> bool:
    return ((not obj.get("archetypes") or archetype in obj["archetypes"])
            and (not obj.get("vintages") or vintage in obj["vintages"]))


def adopting_buildings(scenario: Spec, buildings: Iterable[Any]) -> Tuple[Set[str], int]:
    """(ids of buildings that adopt the scenario, number targeted)."""
    ids = set(scenario.get("building_ids") or [])
    targeted = sorted(b.building_id for b in buildings
                      if _matches(scenario, b.archetype, b.vintage) and (not ids or b.building_id in ids))
    rate = scenario.get("adoption_rate", 1.0)
    if rate >= 1:
        return set(targeted), len(targeted)
    k = round(rate * len(targeted))
    return set(random.Random(scenario.get("seed", 0)).sample(targeted, k)), len(targeted)


def scenario_hook(scenario: Spec, adopters: Set[str]) -> Callable[[Any, ArchetypeParams], Tuple[ArchetypeParams, List[Dict[str, str]]]]:
    """A parameter hook for model generation that applies the scenario."""
    def hook(building, params: ArchetypeParams):
        if building.building_id not in adopters:
            return params, []
        applied = []
        for m in scenario["measures"]:
            if not _matches(m, building.archetype, building.vintage):
                continue
            params, note = MEASURES[m["type"]]["apply"](params, m)
            if note:
                applied.append({"measure": m["type"], "change": note})
        return params, applied
    return hook


def summarize_adoption(models: Sequence[Dict[str, Any]], targeted: int) -> Dict[str, Any]:
    from collections import Counter

    per_measure = Counter(a["measure"] for m in models for a in m.get("retrofit", []))
    return {
        "buildings_targeted": targeted,
        "buildings_retrofitted": sum(1 for m in models if m.get("retrofit")),
        "measure_applications": dict(per_measure),
    }
