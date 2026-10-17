"""
Archetype parameter library: resolves (archetype, vintage, climate zone) to
the envelope, internal loads, schedules and HVAC efficiencies used to build
an EnergyPlus model.

Data come from two files built by scripts/ in this repository:
  - data/reference_buildings.json: DOE Commercial Reference Buildings
    (Deru et al. 2011), 16 types x 3 vintages x 16 climate zones
  - data/residential_prototype.json: DOE/PNNL IECC 2006 single-family
    prototype, 15 climate zones

Assumptions that are not read directly from those sources are recorded in
the returned parameters' `assumptions` list.
"""

import copy
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_DATA = Path(__file__).parent / "data"

# Reference Buildings climate zones; 3B-CA is the coastal-California variant.
CLIMATE_ZONES = ("1A", "2A", "2B", "3A", "3B-CA", "3B", "3C", "4A", "4B", "4C",
                 "5A", "5B", "6A", "6B", "7", "8")

# Archetypes without their own reference model use the closest one.
_COMMERCIAL_PROXY = {"highrise_apartment": "midrise_apartment"}

# EnergyPlus air-film resistances (m2K/W) removed when converting a U-factor
# that includes films into an assembly R-value.
_WALL_FILMS = 0.12 + 0.03
_ROOF_FILMS = 0.11 + 0.04


@dataclass
class ArchetypeParams:
    archetype: str
    vintage: str
    climate_zone: str
    sector: str
    # envelope (assembly R-values exclude air films)
    wall_r: float
    roof_r: float
    floor_r: float
    wall_construction: str
    roof_construction: str
    window_u: float
    window_shgc: float
    window_vt: float
    window_to_wall_ratio: float
    # internal loads, per m2 of conditioned floor area
    people_per_m2: float
    lighting_w_m2: float
    electric_equipment: List[Dict[str, Any]]  # [{name, w_per_m2, schedule, fraction_*}]
    gas_equipment: List[Dict[str, Any]]
    outdoor_air_l_s_m2: float
    outdoor_air_l_s_person: Optional[float]
    # infiltration: either design-flow ACH (commercial) or effective leakage area (residential)
    infiltration_ach: Optional[float]
    ela_cm2_per_m2_envelope: Optional[float]
    # hot water
    hot_water_m3_s_per_m2: float
    hot_water_schedule: Optional[str]
    water_heater_fuel: str
    water_heater_efficiency: float
    water_heater_setpoint_c: float
    # HVAC
    hvac_system: str  # psz | ptac | pvav | vav
    reheat_coil: Optional[str]  # VAV zone reheat: Electric | HotWater
    ahu_heating_coil: Optional[str]  # VAV air-handler heating coil: Gas | HotWater | None
    cooling_cop: float
    heating_efficiency: float
    heating_fuel: str
    economizer: bool
    # schedules: role -> schedule name, and the schedule definitions
    schedule_roles: Dict[str, str]
    schedules: Dict[str, Dict[str, Any]]
    exterior_lighting_w_per_m2: float = 0.0
    # retrofit-only settings (baseline models keep the defaults)
    roof_solar_absorptance: float = 0.7
    heat_pump_heating_cop: Optional[float] = None  # air-source heat pump instead of furnace/resistance
    exterior_lighting_schedule: Optional[str] = None
    reference_eui_mj_m2: Dict[str, Dict[str, float]] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _commercial() -> Dict[str, Any]:
    with open(_DATA / "reference_buildings.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _residential() -> Dict[str, Any]:
    with open(_DATA / "residential_prototype.json", encoding="utf-8") as f:
        return json.load(f)


def normalize_climate_zone(cz: str) -> str:
    c = str(cz).strip().upper().replace(" ", "")
    if c in CLIMATE_ZONES:
        return c
    if c in ("7A", "7B"):
        return "7"
    if c in ("8A", "8B"):
        return "8"
    raise ValueError(f"Unknown climate zone {cz!r}; valid: {', '.join(CLIMATE_ZONES)}")


def data_sources() -> Dict[str, Any]:
    return {"commercial": _commercial()["source"], "residential": _residential()["source"]}


_CENTRAL_PLANT = {"large_office", "hospital", "large_hotel"}


def hvac_family(archetype: str, hvac: Dict[str, Any]):
    """(family, reheat coil, air-handler heating coil) from a reference
    building's system description, simplified to what HVACTemplate objects
    can express. Families: psz (packaged single-zone, incl. constant-volume
    single-zone), ptac, pvav (packaged VAV with reheat), vav (central chiller
    and boiler)."""
    system = str(hvac.get("system_type") or "").upper()
    heating = str(hvac.get("heating_type") or "").lower()
    if archetype == "small_hotel":
        return "ptac", None, None
    if archetype in _CENTRAL_PLANT:
        return "vav", "HotWater", "HotWater"
    if "VAV" in system:
        reheat = "Electric" if "electric reheat" in heating else "HotWater"
        return "pvav", reheat, "Gas" if "furnace" in heating else "None"
    return "psz", None, None


def resolve(archetype: str, vintage: str, climate_zone: str) -> ArchetypeParams:
    cz = normalize_climate_zone(climate_zone)
    if archetype == "single_family":
        return _resolve_single_family(vintage, cz)
    return _resolve_commercial(archetype, vintage, cz)


def _resolve_commercial(archetype: str, vintage: str, cz: str) -> ArchetypeParams:
    lib = _commercial()["archetypes"]
    assumptions = []
    source = _COMMERCIAL_PROXY.get(archetype, archetype)
    if source != archetype:
        assumptions.append(f"{archetype} uses the {source} reference building (no reference model of its own)")
    if source not in lib:
        raise KeyError(f"No reference data for archetype {archetype!r}")
    v = lib[source]["vintages"][vintage]
    c = v["climate"][cz]
    loads = v["loads"]
    roles = v["schedule_roles"]

    electric = [{"name": "plug_and_process", "w_per_m2": loads["electric_equipment_w_m2"],
                 "schedule": roles["equipment"], "fraction_latent": 0.0, "fraction_radiant": 0.5,
                 "fraction_lost": 0.0}]
    gas = []
    if loads["gas_equipment_w_m2"] > 0:
        gas_sched = roles.get("gas_equipment") or roles["equipment"]
        gas.append({"name": "gas_process", "w_per_m2": loads["gas_equipment_w_m2"], "schedule": gas_sched,
                    "fraction_latent": 0.1, "fraction_radiant": 0.2, "fraction_lost": 0.6})

    family, reheat, ahu_heat = hvac_family(source, v["hvac"])
    hvac_heating_fuel = "Electricity" if c.get("heating_efficiency") and c["heating_efficiency"] >= 0.99 else "NaturalGas"
    ext = c.get("reference_eui_mj_m2", {}).get("electricity", {}).get("exterior_lighting", 0.0)
    return ArchetypeParams(
        archetype=archetype, vintage=vintage, climate_zone=cz, sector="commercial",
        wall_r=c["wall_r_m2k_w"], roof_r=c["roof_r_m2k_w"], floor_r=c.get("floor_r_m2k_w") or 0.0,
        wall_construction=str(c.get("wall_construction") or "Steel frame"),
        roof_construction=str(c.get("roof_construction") or "IEAD"),
        window_u=c["window_u_w_m2k"], window_shgc=c["window_shgc"], window_vt=c.get("window_vt") or c["window_shgc"],
        window_to_wall_ratio=v["form"]["window_to_wall_ratio"],
        people_per_m2=loads["people_per_m2"], lighting_w_m2=loads["lighting_w_m2"],
        electric_equipment=electric, gas_equipment=gas,
        outdoor_air_l_s_m2=loads["outdoor_air_l_s_m2"], outdoor_air_l_s_person=loads.get("outdoor_air_l_s_person"),
        infiltration_ach=loads["infiltration_ach"], ela_cm2_per_m2_envelope=None,
        hot_water_m3_s_per_m2=loads["hot_water_l_h_m2"] / 1000.0 / 3600.0,
        hot_water_schedule=roles.get("hot_water"),
        water_heater_fuel="Electricity" if str(v["hvac"].get("water_heater_fuel")).lower().startswith("elec") else "NaturalGas",
        water_heater_efficiency=v["hvac"].get("water_heater_efficiency") or 0.8,
        water_heater_setpoint_c=60.0,
        hvac_system=family, reheat_coil=reheat, ahu_heating_coil=ahu_heat,
        cooling_cop=c.get("cooling_cop") or 3.0,
        heating_efficiency=c.get("heating_efficiency") or 0.8,
        heating_fuel=hvac_heating_fuel,
        economizer=str(c.get("economizer") or "").lower().startswith("differential"),
        schedule_roles=dict(roles),
        schedules=copy.deepcopy(v["schedules"]),
        # Spread the reference exterior-lighting energy over ~4,380 night hours.
        exterior_lighting_w_per_m2=ext * 1e6 / 3600.0 / 4380.0,
        exterior_lighting_schedule=None,
        reference_eui_mj_m2=c.get("reference_eui_mj_m2", {}),
        assumptions=assumptions + [
            "loads and schedules are the reference building's area-weighted averages (one space type per building)",
            "refrigeration, elevators and kitchen exhaust from the reference building are not modeled",
        ],
    )


def _resolve_single_family(vintage: str, cz: str) -> ArchetypeParams:
    res = _residential()
    res_cz = "3B" if cz == "3B-CA" else cz
    c = res["climate"][res_cz]
    loads = res["loads"]
    area = res["conditioned_area_m2"]
    assumptions = ["single-family loads, schedules and IECC 2006 envelope from the DOE/PNNL residential prototype"]
    if res_cz != cz:
        assumptions.append(f"climate zone {cz} uses the residential prototype for {res_cz}")

    wall_r = max(1.0 / c["wall_u_w_m2k"] - _WALL_FILMS, 0.05)
    roof_r = max(1.0 / c["ceiling_u_w_m2k"] - _ROOF_FILMS, 0.05)
    window_u, shgc, vt = c["window_u_w_m2k"], c["window_shgc"], c["window_vt"]
    cop, furnace = c["hvac"]["cooling_cop"], c["hvac"]["furnace_efficiency"]
    ela = c["effective_leakage_area_cm2_at_4pa"] / res["envelope_area_m2"]
    floor_r = 0.0

    if vintage != "post2004":
        # Older houses: envelope, leakage and equipment efficiency follow the
        # Reference Buildings mid-rise apartment of the same vintage (the
        # closest residential construction in that dataset).
        ref = _commercial()["archetypes"]["midrise_apartment"]["vintages"]
        old, new = ref[vintage], ref["post2004"]
        rc = old["climate"][cz]
        wall_r, roof_r = rc["wall_r_m2k_w"], rc["roof_r_m2k_w"]
        window_u, shgc, vt = rc["window_u_w_m2k"], rc["window_shgc"], rc.get("window_vt") or rc["window_shgc"]
        cop, furnace = rc.get("cooling_cop") or cop, rc.get("heating_efficiency") or furnace
        floor_r = rc.get("floor_r_m2k_w") or 0.0
        ratio = old["loads"]["infiltration_ach"] / new["loads"]["infiltration_ach"]
        ela *= ratio
        assumptions.append(
            f"{vintage} single-family envelope, HVAC efficiency and leakage (x{ratio:.2f} of IECC 2006) taken from "
            f"the Reference Buildings {vintage} mid-rise apartment")

    hw_total = sum(h["peak_flow_m3_s"] for h in loads["hot_water"])
    hot_water = combine_schedules(res["schedules"], [(h["flow_schedule"], h["peak_flow_m3_s"]) for h in loads["hot_water"]])
    ext_w = sum(e["watts"] for e in loads["exterior_lights"] if "exterior" in e["name"].lower())
    ext_sched = next((e["schedule"] for e in loads["exterior_lights"] if "exterior" in e["name"].lower()), None)
    return ArchetypeParams(
        archetype="single_family", vintage=vintage, climate_zone=cz, sector="residential",
        wall_r=wall_r, roof_r=roof_r, floor_r=floor_r,
        wall_construction="Wood frame", roof_construction="Attic",
        window_u=window_u, window_shgc=shgc, window_vt=vt,
        window_to_wall_ratio=c["window_to_wall_ratio"],
        people_per_m2=loads["people_per_dwelling"] / area,
        lighting_w_m2=sum(e["w_per_m2"] for e in loads["lights"]),
        electric_equipment=copy.deepcopy(loads["electric_equipment"]),
        gas_equipment=copy.deepcopy(loads["gas_equipment"]),
        outdoor_air_l_s_m2=0.0, outdoor_air_l_s_person=None,
        infiltration_ach=None, ela_cm2_per_m2_envelope=ela,
        # The combined schedule is normalized to peak 1; scale the flow to match.
        hot_water_m3_s_per_m2=hw_total * hot_water["peak_scale"] / area,
        hot_water_schedule="_combined_hot_water",
        water_heater_fuel="NaturalGas", water_heater_efficiency=loads["water_heater"]["efficiency"],
        water_heater_setpoint_c=48.0,
        hvac_system="psz", reheat_coil=None, ahu_heating_coil=None, cooling_cop=cop, heating_efficiency=furnace, heating_fuel="NaturalGas",
        economizer=False,
        schedule_roles={
            "occupancy": loads["occupancy_schedule"], "activity": loads["activity_schedule"],
            "lighting": loads["lights"][0]["schedule"],
            "heating_setpoint": loads["heating_setpoint_schedule"],
            "cooling_setpoint": loads["cooling_setpoint_schedule"],
            "infiltration": None, "hvac_operation": None,
        },
        schedules={**copy.deepcopy(res["schedules"]), "_combined_hot_water": hot_water},
        exterior_lighting_w_per_m2=ext_w / area,
        exterior_lighting_schedule=ext_sched,
        reference_eui_mj_m2=c["reference_eui_mj_m2"] if vintage == "post2004" else {},
        assumptions=assumptions + [
            "hot-water fixtures are combined into one flow schedule weighted by peak flow",
            "the attic is not modeled; the ceiling insulation is applied to the roof",
        ],
    )


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

DAY_TYPES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
             "Holiday", "SummerDesignDay", "WinterDesignDay")
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}

# Day-type labels used in the source data -> the EnergyPlus day types they cover.
_LABELS = {
    "all": set(DAY_TYPES), "alldays": set(DAY_TYPES),
    "wd": _WEEKDAYS, "weekdays": _WEEKDAYS, "weekday": _WEEKDAYS,
    "weekends": {"Saturday", "Sunday"}, "weekend": {"Saturday", "Sunday"}, "we": {"Saturday", "Sunday"},
    "sat": {"Saturday"}, "saturday": {"Saturday"},
    "sun": {"Sunday"}, "sunday": {"Sunday"},
    "hol": {"Holiday"}, "holiday": {"Holiday"}, "holidays": {"Holiday"},
    "summerdesign": {"SummerDesignDay"}, "summerdesignday": {"SummerDesignDay"},
    "winterdesign": {"WinterDesignDay"}, "winterdesignday": {"WinterDesignDay"},
    "monday": {"Monday"}, "tuesday": {"Tuesday"}, "wednesday": {"Wednesday"},
    "thursday": {"Thursday"}, "friday": {"Friday"},
    # Only used with RunPeriodControl:SpecialDays, which generated models don't define.
    "customday1": set(), "customday2": set(),
}


def expand_rules(rules: List[Dict[str, Any]]):
    """[(through 'M/D', {day_type: [24 values]})] with every day type covered.

    "Other"/"AllOtherDays" fills the day types not named earlier in the same
    period, which is how both data sources use it.
    """
    periods, order = {}, []
    for r in rules:
        thru = r["through"]
        if thru not in periods:
            periods[thru] = {}
            order.append(thru)
        days = periods[thru]
        for label in r["days"]:
            key = label.strip().lower().replace(" ", "").replace(":", "")
            if key in ("other", "allotherdays"):
                targets = set(DAY_TYPES) - set(days)
            else:
                if key not in _LABELS:
                    raise ValueError(f"Unknown day type {label!r}")
                targets = _LABELS[key]
            for d in targets:
                days.setdefault(d, list(r["values"]))
    out = []
    for thru in order:
        days = periods[thru]
        missing = set(DAY_TYPES) - set(days)
        if missing:
            # Design days default to the regular weekday profile.
            fallback = days.get("Wednesday") or next(iter(days.values()))
            for d in missing:
                days[d] = list(fallback)
        out.append((thru, days))
    return out


def combine_schedules(schedules: Dict[str, Dict[str, Any]], weighted: List[tuple]) -> Dict[str, Any]:
    """Weighted average of fraction schedules, as one full-year day-type profile.

    Multiple "Through" periods are averaged by their length in days.
    """
    total = sum(w for _, w in weighted)
    acc = {d: [0.0] * 24 for d in DAY_TYPES}
    for name, w in weighted:
        periods = expand_rules(schedules[name]["rules"])
        start = 0
        span = {}
        for thru, _days in periods:
            end = _day_of_year(thru)
            span[thru] = end - start
            start = end
        for thru, days in periods:
            share = span[thru] / 365.0
            for d in DAY_TYPES:
                for h in range(24):
                    acc[d][h] += w / total * share * days[d][h]
    peak = max(max(v) for v in acc.values()) or 1.0
    return {
        "type": "Fraction",
        "rules": [{"through": "12/31", "days": [d], "values": [round(x / peak, 6) for x in acc[d]]} for d in DAY_TYPES],
        "peak_scale": peak,
    }


def _day_of_year(md: str) -> int:
    m, d = (int(x) for x in md.split("/"))
    cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    return cum[m - 1] + d
