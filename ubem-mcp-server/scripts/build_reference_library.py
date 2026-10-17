"""
Build UBEM-MCP's commercial archetype library from the U.S. DOE Commercial
Reference Buildings (Deru et al., NREL/TP-5500-46861, 2011).

Source data: the reference-building summary workbooks published on the Open
Energy Data Initiative (OEDI), submissions 156-171, licensed CC BY 4.0:
16 building types x 3 vintages (pre-1980, post-1980, new construction 2004),
each with 16 climate locations.

For each building type and vintage this extracts:
  - form: floor area, floors, floor-to-floor height, window-to-wall ratio
  - loads: area-weighted people, lighting, plug/process (electric and gas),
    service hot water, outdoor air and infiltration from the zone summary
  - hvac: system/heating/cooling type, fan control, water-heater efficiency
  - schedules: hourly schedules (occupancy, lighting, equipment, setpoints,
    infiltration, HVAC operation, hot water), mapped to roles per type
  - per climate zone: wall/roof/floor R-values, window U/SHGC/VT, cooling COP,
    heating efficiency, economizer, and the reference simulation's end-use
    energy intensities (used to validate generated models)

Usage (from ubem-mcp-server/):
    uv run --with openpyxl python scripts/build_reference_library.py \
        [--cache-dir .refbldg_cache] [--out ubem_mcp_server/urban/data/reference_buildings.json]

The workbooks are downloaded once into --cache-dir. The output JSON is
committed to the repository so the server does not need network access.
"""

import argparse
import hashlib
import json
import re
import statistics
import sys
import urllib.request
from datetime import date
from pathlib import Path

import openpyxl

OEDI_SUBMISSIONS = {
    156: ("full_service_restaurant", "fullservicerestaurant"),
    157: ("hospital", "hospital"),
    158: ("large_hotel", "largehotel"),
    159: ("large_office", "largeoffice"),
    160: ("medium_office", "mediumoffice"),
    161: ("midrise_apartment", "midriseapartment"),
    162: ("outpatient", "outpatient"),
    163: ("primary_school", "primaryschool"),
    164: ("quick_service_restaurant", "quickservicerestaurant"),
    165: ("secondary_school", "secondaryschool"),
    166: ("small_hotel", "smallhotel"),
    167: ("small_office", "smalloffice"),
    168: ("standalone_retail", "standaloneretail"),
    169: ("strip_mall", "stripmall"),
    170: ("supermarket", "supermarket"),
    171: ("warehouse", "warehouse"),
}
VINTAGE_FILES = {"pre1980": "pre1980", "1980_2004": "post1980", "post2004": "new2004"}
FILE_URL = "https://data.openei.org/files/{id}/refbldg{slug}{vintage}v1.35.0si.xlsx"

# Schedule roles. Defaults apply to every type; overrides pick the schedule
# of the dominant space type where a building has several.
DEFAULT_ROLES = {
    "occupancy": "BLDG_OCC_SCH",
    "lighting": "BLDG_LIGHT_SCH",
    "equipment": "BLDG_EQUIP_SCH",
    "heating_setpoint": "HTGSETP_SCH",
    "cooling_setpoint": "CLGSETP_SCH",
    "hvac_operation": "HVACOperationSchd",
    "activity": "ACTIVITY_SCH",
    "infiltration": None,  # first schedule whose name starts with INFIL
    "hot_water": "BLDG_SWH_SCH",
    "gas_equipment": None,
}
ROLE_OVERRIDES = {
    "midrise_apartment": {
        "occupancy": "APT_OCC_SCH", "lighting": "APT_LIGHT_SCH", "equipment": "APT_EQP_SCH",
        "heating_setpoint": "APT_HTGSETP_SCH", "cooling_setpoint": "APT_CLGSETP_SCH",
        "hvac_operation": "Apt_Operation", "infiltration": "INFIL_SCH", "hot_water": "APT_DHW_SCH",
    },
    "small_hotel": {
        "occupancy": "GuestRoom_Occ_Sch", "lighting": "GuestRoom_Ltg_Sch_Base", "equipment": "GuestRoom_Eqp_Sch",
        "infiltration": "INFIL_SCH", "hot_water": "GuestRoom_SWH_Sch",
    },
    "large_hotel": {
        "occupancy": "GuestRoom_Occ_Sch", "lighting": "GuestRoom_Ltg_Sch_Base", "equipment": "GuestRoom_Eqp_Sch",
        "heating_setpoint": "Guest_HTGSETP_SCH", "cooling_setpoint": "Guest_CLGSETP_SCH",
    },
    # Storage is 95% of the floor area (bulk 66% at 7.2 C heating, fine 29%
    # at 15.6/26.7 C); fine-storage setpoints sit between bulk and office.
    "warehouse": {
        "heating_setpoint": "HTGSETP_FineStorage_SCH", "cooling_setpoint": "CLGSETP_FineStorage_SCH",
    },
    "hospital": {
        "occupancy": "CRITICAL_OCC_SCH", "equipment": "CRITICAL_EQUIP_SCH",
        "heating_setpoint": "CRITICAL_HTGSETP_SCH", "cooling_setpoint": "CRITICAL_CLGSETP_SCH",
    },
}
OPTIONAL_ROLES = {"hot_water", "gas_equipment", "hvac_operation", "activity"}

# LocationSummary HVAC sub-headers -> what the rows below them hold (None = skip).
HVAC_HEADERS = (
    ("HVAC Sizing", None),
    ("Air Conditioning (kW)", None),
    ("Heating (kW)", None),
    ("HVAC Efficiency", None),
    ("Air Conditioning (COP)", "cop"),
    ("Heating Efficiency", "heff"),
    ("HVAC Control - Economizer", "econ"),
    ("Fan Max Flow Rate", None),
)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def fetch(cache_dir: Path, sub_id: int, slug: str, vintage_file: str) -> Path:
    url = FILE_URL.format(id=sub_id, slug=slug, vintage=vintage_file)
    path = cache_dir / url.rsplit("/", 1)[1]
    if not path.exists():
        print(f"downloading {url}", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "UBEM-MCP reference-library builder"})
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed https host
            path.write_bytes(resp.read())
    return path


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s.lower() in ("", "none", "n/a", "na", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rows(ws):
    return list(ws.iter_rows(values_only=True))


def building_summary(ws):
    """Flatten the BuildingSummary sheet into {"Section/Subsection/Label": value}."""
    out, section, sub = {}, None, None
    for row in _rows(ws)[2:]:
        if row[0]:
            section, sub = str(row[0]).strip(), None
            continue
        label = str(row[1]).strip() if len(row) > 1 and row[1] is not None else None
        if not label:
            continue
        value = row[2] if len(row) > 2 else None
        if value is None:
            sub = label
            continue
        out[f"{section}/{sub}/{label}" if sub else f"{section}/{label}"] = value
    return out


def zone_loads(ws):
    """Area-weighted building-level loads from the ZoneSummary sheet."""
    rows = _rows(ws)
    header = [str(h).strip() if h else "" for h in rows[1]]
    col = {h: i for i, h in enumerate(header)}
    zones = []
    for row in rows[2:]:
        name = row[0]
        if name and str(name).startswith("Total"):
            break
        if not name or str(row[1]).strip() != "Yes":
            continue
        mult = _num(row[col["Multiplier"]]) or 1.0
        area = (_num(row[col["Area (m2)"]]) or 0.0) * mult
        volume = (_num(row[col["Volume (m3)"]]) or 0.0) * mult
        if area <= 0:
            continue
        people = (_num(row[col["People"]]) or 0.0) * mult
        zones.append({
            "area": area, "volume": volume, "people": people,
            "lights": (_num(row[col["Lights (W/m2)"]]) or 0.0) * area,
            "elec": (_num(row[col["Elec Plug and Process (W/m2)"]]) or 0.0) * area,
            "gas": (_num(row[col["Gas Plug and Process (W/m2)"]]) or 0.0) * area,
            "swh": (_num(row[col["SWH (L/h)"]]) or 0.0) * mult,
            "vent": (_num(row[col["Ventilation Total (L/s)"]]) or 0.0) * mult,
            "infil_m3h": (_num(row[col["Infiltration (ACH)"]]) or 0.0) * volume,
        })
    area = sum(z["area"] for z in zones)
    volume = sum(z["volume"] for z in zones)
    people = sum(z["people"] for z in zones)
    return {
        "conditioned_area_m2": round(area, 2),
        "people_per_m2": people / area,
        "lighting_w_m2": sum(z["lights"] for z in zones) / area,
        "electric_equipment_w_m2": sum(z["elec"] for z in zones) / area,
        "gas_equipment_w_m2": sum(z["gas"] for z in zones) / area,
        "hot_water_l_h_m2": sum(z["swh"] for z in zones) / area,
        "outdoor_air_l_s_m2": sum(z["vent"] for z in zones) / area,
        "outdoor_air_l_s_person": (sum(z["vent"] for z in zones) / people) if people else None,
        "infiltration_ach": (sum(z["infil_m3h"] for z in zones) / volume) if volume else 0.0,
    }


def schedules(ws):
    """Parse the Schedules sheet into {name: {"type": ..., "rules": [{through, days, values}]}}."""
    out, current, through = {}, None, None
    for row in _rows(ws)[1:]:
        name, stype, thru, days = row[0], row[1], row[2], row[3]
        values = [_num(v) for v in row[4:28]]
        if name:
            current = str(name).strip()
            out[current] = {"type": str(stype).strip() if stype else "", "rules": []}
        if current is None or days is None:
            continue
        if thru:
            through = str(thru).replace("Through", "").strip()
        if any(v is None for v in values):
            continue
        out[current]["rules"].append({
            "through": through or "12/31",
            "days": [d.strip() for d in str(days).split(",") if d.strip()],
            "values": values,
        })
    return {k: v for k, v in out.items() if v["rules"]}


def location_summary(ws):
    """Per-climate envelope, HVAC efficiency and end-use intensities."""
    rows = _rows(ws)
    cities = [c for c in rows[1][2:] if c]
    n = len(cities)
    zones_row = next(r for r in rows if r[1] and str(r[1]).startswith("ASHRAE 90.1-2004 Climate"))
    climate_zones = [str(z).strip() for z in zones_row[2:2 + n]]

    result = {cz: {"city": city} for cz, city in zip(climate_zones, cities, strict=False)}
    section, sub, mode = None, None, None
    cop, heff, econ = {cz: [] for cz in climate_zones}, {cz: [] for cz in climate_zones}, {cz: [] for cz in climate_zones}
    eui_fuel = None
    for row in rows[3:]:
        if row[0]:
            section, sub, mode = str(row[0]).strip(), None, None
            continue
        label = str(row[1]).strip() if row[1] is not None else ""
        vals = list(row[2:2 + n])
        has_vals = any(v is not None for v in vals)

        if section == "Fabric":
            if not has_vals:
                sub = label
                continue
            key = _fabric_key(sub, label)
            # The first matching block wins (e.g. a hotel's commercial-space
            # walls before its residential-space walls).
            if key and key not in result[climate_zones[0]]:
                for cz, v in zip(climate_zones, vals, strict=False):
                    result[cz][key] = v if key.endswith("construction") else _num(v)
        elif section == "HVAC":
            # Sub-headers switch what the following equipment rows mean. A
            # header can also carry values itself (e.g. a single boiler's
            # "Heating Efficiency (%)" row).
            header_mode = next((m for prefix, m in HVAC_HEADERS if label.startswith(prefix)), "")
            if header_mode != "":
                mode = header_mode
            elif not has_vals:
                mode = None
            if not has_vals or mode is None:
                continue
            for cz, v in zip(climate_zones, vals, strict=False):
                if mode == "cop" and _num(v):
                    cop[cz].append(_num(v))
                elif mode == "heff" and _num(v):
                    heff[cz].append(_num(v))
                elif mode == "econ" and v:
                    econ[cz].append(str(v))
        elif section == "Energy - End Use Intensities":
            if label.endswith("(MJ/m2)") and not has_vals:
                eui_fuel = {"Electricity (MJ/m2)": "electricity", "Gas (MJ/m2)": "natural_gas"}.get(label)
                continue
            if eui_fuel and has_vals and not label.startswith("Total"):
                end_use = re.sub(r"\s*\((elec|gas)\)\s*$", "", label).strip().lower().replace(" ", "_")
                for cz, v in zip(climate_zones, vals, strict=False):
                    x = _num(v)
                    if x:
                        result[cz].setdefault("reference_eui_mj_m2", {}).setdefault(eui_fuel, {})[end_use] = round(x, 3)
    for cz in climate_zones:
        # Efficiencies may be given as fractions or percentages.
        h = [x / 100 if x > 1.5 else x for x in heff[cz]]
        result[cz]["cooling_cop"] = round(statistics.mean(cop[cz]), 3) if cop[cz] else None
        result[cz]["heating_efficiency"] = round(statistics.mean(h), 3) if h else None
        result[cz]["economizer"] = (max(set(econ[cz]), key=econ[cz].count) if econ[cz] else None)
        eui = result[cz].get("reference_eui_mj_m2", {})
        result[cz]["reference_eui_mj_m2_total"] = round(sum(sum(d.values()) for d in eui.values()), 2)
    return result


def _fabric_key(sub, label):
    sub = (sub or "").lower()
    label = label.lower()
    if sub.startswith("exterior walls"):
        part = "wall"
    elif sub.startswith("roof"):
        part = "roof"
    elif sub.startswith("foundation"):
        part = "floor"
    elif sub.startswith("window"):
        part = "window"
    else:
        return None
    if label.startswith("construction type") and part in ("wall", "roof"):
        return f"{part}_construction"
    if label.startswith("r-value") and part in ("wall", "roof", "floor"):
        return f"{part}_r_m2k_w"
    if part == "window":
        if label.startswith("u-factor"):
            return "window_u_w_m2k"
        if label == "shgc":
            return "window_shgc"
        if label.startswith("visible transmittance"):
            return "window_vt"
    return None


def find(bs: dict, section: str, label_prefix: str):
    """Value of the first BuildingSummary key in `section` whose last part starts with label_prefix."""
    for k, v in bs.items():
        if k.split("/", 1)[0] == section and k.rsplit("/", 1)[-1].startswith(label_prefix):
            return v
    return None


def _wwr(bs: dict):
    """Total and per-orientation window-to-wall ratio."""
    by_o = {}
    for o in ("South", "East", "North", "West"):
        key = next((k for k in bs if k.startswith("Form/Window Fraction") and k.endswith("/" + o)), None)
        by_o[o] = _num(bs[key]) if key else None
    total_key = next((k for k in bs if k.startswith("Form/Window Fraction") and k.endswith("/Total")), None)
    total = _num(bs[total_key]) if total_key else None
    if total is None:
        # Some workbooks give the ratios as one text cell; fall back to the
        # definition: window area / gross exterior wall area.
        windows = _sum_text(next((v for k, v in bs.items()
                                  if k.startswith("Fabric/Window") and k.endswith("Total Area (m2)")), None))
        walls = _num(next((v for k, v in bs.items()
                           if k.startswith("Fabric/Exterior walls") and "/Gross Dimensions" in k), None))
        if windows and walls:
            total = round(windows / walls, 4)
        text = next((v for k, v in bs.items() if k == "Form/Window Fraction (Window to Wall Ratio)"), None)
        if text:
            for o in by_o:
                m = re.search(rf"{o}:\s*([0-9.]+)", str(text))
                if m:
                    by_o[o] = float(m.group(1))
    return total, by_o


def _sum_text(value):
    """Number, or the sum of numbers in text like 'South: 124
East: 0.00 ...'."""
    x = _num(value)
    if x is not None or value is None:
        return x
    nums = [float(n) for n in re.findall(r":\s*([0-9.]+)", str(value))]
    return sum(nums) if nums else None


def _typical_height(value):
    """Floor height, or the typical-floor value of text like '3.35 1st floor, 2.74 upper floors'."""
    x = _num(value)
    if x is not None or value is None:
        return x
    nums = re.findall(r"[0-9]+\.[0-9]+", str(value))
    return float(nums[-1]) if nums else None


def role_map(archetype: str, sched: dict) -> dict:
    roles = {**DEFAULT_ROLES, **ROLE_OVERRIDES.get(archetype, {})}
    if roles["infiltration"] is None:
        # Buildings without an INFIL_* schedule (e.g. warehouse) infiltrate constantly.
        roles["infiltration"] = next((n for n in sched if n.upper().startswith("INFIL")), "ALWAYS_ON")
    if roles["gas_equipment"] is None:
        roles["gas_equipment"] = next((n for n in sched if re.search(r"gas.*equip", n, re.I)), None)
    resolved = {}
    for role, name in roles.items():
        if name and name in sched:
            resolved[role] = name
        elif role not in OPTIONAL_ROLES:
            raise KeyError(f"{archetype}: schedule {name!r} for role {role!r} not found")
    return resolved


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build(cache_dir: Path) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    library = {
        "source": {
            "title": "U.S. Department of Energy Commercial Reference Buildings (v1.3_5.0)",
            "reference": ("Deru, M. et al. (2011). U.S. Department of Energy Commercial Reference Building "
                          "Models of the National Building Stock. NREL/TP-5500-46861."),
            "data": "Open Energy Data Initiative (OEDI) submissions 156-171",
            "url": "https://data.openei.org/submissions/167",
            "license": "CC BY 4.0",
            "built_on": date.today().isoformat(),
            "files": {},
        },
        "climate_zones": {},
        "archetypes": {},
    }
    for sub_id, (archetype, slug) in OEDI_SUBMISSIONS.items():
        entry = {"reference_name": slug, "vintages": {}}
        for vintage, vfile in VINTAGE_FILES.items():
            path = fetch(cache_dir, sub_id, slug, vfile)
            library["source"]["files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            bs = building_summary(wb["BuildingSummary"])
            sched = schedules(wb["Schedules"])
            roles = role_map(archetype, sched)
            loc = location_summary(wb["LocationSummary"])
            for cz, d in loc.items():
                library["climate_zones"].setdefault(cz, d["city"])
                d.pop("city", None)

            def g(key, numeric=True, bs=bs):
                v = bs.get(key)
                return _num(v) if numeric else v

            floors_raw = find(bs, "Form", "Number of Floors")
            m = re.match(r"\s*(\d+)", str(floors_raw or ""))
            f2f = (_typical_height(find(bs, "Form", "Floor to Floor Height"))
                   or _typical_height(find(bs, "Form", "Floor to Ceiling Height")))
            wwr, wwr_by_o = _wwr(bs)
            entry["vintages"][vintage] = {
                "form": {
                    "floor_area_m2": _num(find(bs, "Form", "Total Floor Area")),
                    "num_floors": int(m.group(1)) if m else None,
                    "has_basement": "basement" in str(floors_raw or "").lower(),
                    "floor_to_floor_m": f2f,
                    "window_to_wall_ratio": wwr,
                    "wwr_by_orientation": wwr_by_o,
                },
                "loads": {k: (round(v, 5) if isinstance(v, float) else v)
                          for k, v in zone_loads(wb["ZoneSummary"]).items()},
                "hvac": {
                    "system_type": g("HVAC/System Type", False),
                    "heating_type": g("HVAC/Heating Type", False),
                    "cooling_type": g("HVAC/Cooling Type", False),
                    "fan_control": g("HVAC/Fan Control", False),
                    "water_heater_fuel": g("HVAC/Service Water Heating/Fuel", False),
                    "water_heater_efficiency": (g("HVAC/Service Water Heating/Thermal Efficiency (%)") or 0) / 100 or None,
                },
                "schedule_roles": roles,
                "schedules": {name: sched[name] for name in sorted(set(roles.values()))},
                "climate": loc,
            }
        library["archetypes"][archetype] = entry
    return library


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--cache-dir", type=Path, default=here / ".refbldg_cache")
    ap.add_argument("--out", type=Path, default=here / "ubem_mcp_server" / "urban" / "data" / "reference_buildings.json")
    args = ap.parse_args(argv)
    lib = build(args.cache_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(lib, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    n = sum(len(a["vintages"]) for a in lib["archetypes"].values())
    print(f"wrote {args.out} ({len(lib['archetypes'])} archetypes, {n} archetype-vintages, "
          f"{len(lib['climate_zones'])} climate zones)")


if __name__ == "__main__":
    main()
