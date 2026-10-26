"""
Build UBEM-MCP's single-family archetype from the DOE/PNNL residential
prototype buildings (IECC 2006 single-family, gas furnace, slab-on-grade).

Source: https://www.energycodes.gov/prototype-building-models (residential
models, EnergyPlus 23.1), one package per IECC climate zone.

For each climate zone this extracts, from the prototype IDF and its tabular
results:
  - envelope: exterior-wall, ceiling (attic floor) and window U-factors, window
    SHGC/VT and window-to-wall ratio
  - HVAC: DX cooling COP and furnace efficiency
  - the prototype's simulated end-use energy intensities (for validation)
and once (they are the same in every zone) the internal loads and schedules:
lighting, each appliance's electric and gas equipment, occupancy, thermostat
setpoints, hot-water fixtures and the living zone's effective leakage area.

Usage (from src/):
    python scripts/build_residential_library.py [--cache-dir .resstd_cache]
"""

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

ZIP_URL = "https://www.energycodes.gov/sites/default/files/2024-12/resstd_CZ{cz}.zip"
MODEL_VARIANT = r"gasfurnace\+slab\+IECC_2006"
CLIMATE_ZONES = ["1A", "2A", "2B", "3A", "3B", "3C", "4A", "4B", "4C", "5A", "5B", "6A", "6B", "7", "8"]
FT2_TO_M2 = 0.09290304
KBTU_TO_MJ = 1.05505585
BTU_FT2F_TO_W_M2K = 5.678263
LIVING_ZONE = "living_unit1"


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def fetch(cache_dir: Path, cz: str):
    """Return (idf_path, table_path) of the zone's base single-family model.

    Most zones have exactly US+SF+CZ<cz>+...; some (e.g. 1A) only ship
    suffixed sub-variants (CZ1AWH, CZ1AWHT, ...), so take the exact match if
    present, else the shortest variant.
    """
    zip_path = cache_dir / f"resstd_CZ{cz}.zip"
    if not zip_path.exists():
        url = ZIP_URL.format(cz=cz)
        print(f"downloading {url}", file=sys.stderr)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (UBEM-MCP library builder)"})
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - fixed https host
            zip_path.write_bytes(resp.read())
    z = zipfile.ZipFile(zip_path)
    pattern = re.compile(rf"^US\+SF\+CZ{re.escape(cz)}([A-Z]*)\+{MODEL_VARIANT}\.idf$")
    matches = sorted((n for n in z.namelist() if pattern.match(n)), key=len)
    if not matches:
        raise FileNotFoundError(f"no single-family {MODEL_VARIANT} model for CZ {cz} in {zip_path.name}")
    stem = matches[0][:-4]
    idf_path, htm_path = cache_dir / f"{stem}.idf", cache_dir / f"{stem}.table.htm"
    if not (idf_path.exists() and htm_path.exists()):
        idf_path.write_bytes(z.read(f"{stem}.idf"))
        htm_path.write_bytes(z.read(f"{stem}.table.htm"))
    return idf_path, htm_path


# --------------------------------------------------------------------------- #
# IDF parsing
# --------------------------------------------------------------------------- #

def parse_idf(text: str):
    """Return [(object_type, [fields...])] with comments removed."""
    clean = "\n".join(line.split("!", 1)[0] for line in text.splitlines())
    out = []
    for m in re.finditer(r"(?s)([A-Za-z][A-Za-z0-9:]*)\s*,(.*?);", clean):
        out.append((m.group(1).strip(), [f.strip() for f in m.group(2).split(",")]))
    return out


def objects(idf, kind):
    return [f for k, f in idf if k.lower() == kind.lower()]


def _get(fields, i):
    """Field i, or "" when an object omits trailing optional fields."""
    return fields[i] if i < len(fields) else ""


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Schedules -> {"type", "rules": [{"through", "days", "values"[24]}]}
# --------------------------------------------------------------------------- #

def _hourly_from_untils(pairs):
    """[(hour_end, value)] -> 24 hourly values."""
    values, start = [], 0
    for hour_end, v in pairs:
        values += [v] * (hour_end - start)
        start = hour_end
    if len(values) != 24:
        raise ValueError(f"schedule does not cover 24 h: {pairs}")
    return values


def _until_hour(token):
    m = re.search(r"(\d{1,2}):(\d{2})", token)
    if not m:
        raise ValueError(f"bad Until token {token!r}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if minute:
        raise ValueError(f"sub-hourly Until {token!r} not supported")
    return hour


def compact_rules(fields):
    rules, through, days, pairs = [], None, None, []

    def flush():
        if days is not None and pairs:
            rules.append({"through": through, "days": days, "values": _hourly_from_untils(pairs)})

    i = 2
    while i < len(fields):
        tok = fields[i]
        low = tok.lower()
        if low.startswith("through"):
            flush()
            through, days, pairs = tok.split(":", 1)[1].strip(), None, []
        elif low.startswith("for"):
            flush()
            days, pairs = [d for d in re.split(r"[\s,]+", tok.split(":", 1)[1].strip()) if d], []
        elif low.startswith("until"):
            pairs.append((_until_hour(tok), _f(fields[i + 1])))
            i += 1
        i += 1
    flush()
    return rules


def resolve_schedule(idf, name):
    by_name = {}
    for k, f in idf:
        if k.lower().startswith("schedule:") and f:
            by_name.setdefault(f[0].lower(), (k, f))
    kind, f = by_name[name.lower()]
    k = kind.lower()
    if k == "schedule:compact":
        return {"type": f[1], "rules": compact_rules(f)}
    if k == "schedule:constant":
        return {"type": f[1], "rules": [{"through": "12/31", "days": ["AllDays"], "values": [_f(f[2])] * 24}]}
    if k == "schedule:year":
        rules = []
        for j in range(2, len(f), 5):
            week, end_m, end_d = f[j], f[j + 3], f[j + 4]
            wk_kind, wk = by_name[week.lower()]
            if wk_kind.lower() != "schedule:week:compact":
                raise ValueError(f"{name}: unsupported week schedule {wk_kind}")
            for d in range(1, len(wk), 2):
                day_types = [t for t in re.split(r"[\s,]+", re.sub(r"(?i)^for:?", "", wk[d]).strip()) if t]
                dkind, dsch = by_name[wk[d + 1].lower()]
                if dkind.lower() != "schedule:day:hourly":
                    raise ValueError(f"{name}: unsupported day schedule {dkind}")
                rules.append({"through": f"{int(end_m)}/{int(end_d)}", "days": day_types,
                              "values": [_f(v) for v in dsch[2:26]]})
        return {"type": f[1], "rules": rules}
    raise ValueError(f"{name}: unsupported schedule type {kind}")


# --------------------------------------------------------------------------- #
# Tabular results
# --------------------------------------------------------------------------- #

def html_table(htm: str, title: str):
    i = htm.find(f"<b>{title}</b>")
    if i < 0:
        raise KeyError(title)
    tab = re.findall(r"(?s)<table.*?</table>", htm[i:i + 60000])[0]
    rows = []
    for row in re.findall(r"(?s)<tr>(.*?)</tr>", tab):
        rows.append([html.unescape(re.sub(r"<.*?>", "", c)).strip() for c in re.findall(r"(?s)<td.*?>(.*?)</td>", row)])
    return rows


def envelope(htm: str):
    area = {r[0]: _f(r[1]) for r in html_table(htm, "Building Area") if len(r) > 1}
    cond_ft2 = area["Net Conditioned Building Area"]

    walls = [r for r in html_table(htm, "Opaque Exterior")[1:] if r[1] == "EXTERIOR WALL" and "UNIT1" in r[0]]
    wall_area = sum(_f(r[5]) for r in walls)
    wall_u = sum(_f(r[3]) * _f(r[5]) for r in walls) / wall_area

    ceiling = next(r for r in html_table(htm, "Opaque Interior")[1:] if r[0] == "CEILING_UNIT1")
    fen = next(r for r in html_table(htm, "Exterior Fenestration") if r and r[0] == "Total or Average")
    wwr = next(r for r in html_table(htm, "Window-Wall Ratio") if r and r[0].startswith("Gross Window-Wall"))

    end_uses = {}
    rows = html_table(htm, "End Uses")
    header = rows[0]
    for r in rows[1:]:
        if not r or not r[0] or r[0].startswith("Total"):
            continue
        for fuel_col, fuel in (("Electricity", "electricity"), ("Natural Gas", "natural_gas")):
            col = next(i for i, h in enumerate(header) if h.startswith(fuel_col))
            v = _f(r[col])
            if v:
                end_uses.setdefault(fuel, {})[r[0].lower().replace(" ", "_")] = round(
                    v * KBTU_TO_MJ / (cond_ft2 * FT2_TO_M2), 3)
    return {
        "conditioned_area_m2": round(cond_ft2 * FT2_TO_M2, 2),
        # Living-unit envelope the leakage area applies to (walls + attic floor).
        "envelope_area_m2": round((wall_area + _f(ceiling[5])) * FT2_TO_M2, 2),
        "wall_u_w_m2k": round(wall_u * BTU_FT2F_TO_W_M2K, 4),
        "ceiling_u_w_m2k": round(_f(ceiling[3]) * BTU_FT2F_TO_W_M2K, 4),
        "window_u_w_m2k": round(_f(fen[8]) * BTU_FT2F_TO_W_M2K, 4),
        "window_shgc": _f(fen[9]),
        "window_vt": _f(fen[10]),
        "window_to_wall_ratio": round(_f(wwr[1]) / 100, 4),
        "reference_eui_mj_m2": end_uses,
        "reference_eui_mj_m2_total": round(sum(sum(d.values()) for d in end_uses.values()), 2),
    }


# --------------------------------------------------------------------------- #
# Loads (living zone)
# --------------------------------------------------------------------------- #

def loads(idf, area_m2):
    sched_names = set()

    def entries(kind, level_idx, extra):
        out = []
        for f in objects(idf, kind):
            if f[1].lower() != LIVING_ZONE or (_f(f[level_idx]) or 0) <= 0:
                continue
            sched_names.add(f[2])
            out.append({"name": f[0], "schedule": f[2], "w_per_m2": round(_f(f[level_idx]) / area_m2, 5),
                        **{k: _f(f[i]) for k, i in extra.items()}})
        return out

    lights = entries("Lights", 4, {"return_air_fraction": 7, "fraction_radiant": 8, "fraction_visible": 9})
    electric = entries("ElectricEquipment", 4, {"fraction_latent": 7, "fraction_radiant": 8, "fraction_lost": 9})
    gas = entries("GasEquipment", 4, {"fraction_latent": 7, "fraction_radiant": 8, "fraction_lost": 9})

    people = next(f for f in objects(idf, "People") if f[1].lower() == LIVING_ZONE)
    known = {f[0].lower() for k, f in idf if k.lower().startswith("schedule:") and f}
    activity = next(v for v in people[7:] if v.lower() in known)
    sched_names.update([people[2], activity])

    tstat = next(f for f in objects(idf, "ZoneControl:Thermostat") if f[1].lower() == LIVING_ZONE)
    dual = next(f for f in objects(idf, "ThermostatSetpoint:DualSetpoint") if f[0] == tstat[4])
    sched_names.update([dual[1], dual[2]])

    hot_water = []
    for f in objects(idf, "WaterUse:Equipment"):
        target, hot = _get(f, 4), _get(f, 5)
        sched_names.update(s for s in (f[3], target, hot) if s)
        hot_water.append({"name": f[0], "peak_flow_m3_s": _f(f[2]), "flow_schedule": f[3],
                          "target_temperature_schedule": target or None, "hot_supply_temperature_schedule": hot or None})
    heater = objects(idf, "WaterHeater:Mixed")[0]
    sched_names.add(heater[2])

    ext = [{"name": f[0], "schedule": f[1], "watts": _f(f[2])} for f in objects(idf, "Exterior:Lights")]
    sched_names.update(e["schedule"] for e in ext)

    # Effective leakage area of the living zone's envelope (walls to outdoors,
    # ceiling to attic). Each leakage component applies once per surface.
    ela = {f[0]: _f(f[1]) for f in objects(idf, "AirflowNetwork:MultiZone:Surface:EffectiveLeakageArea")}
    surfaces = {f[0]: f for f in objects(idf, "BuildingSurface:Detailed")}
    living_ela = 0.0
    for f in objects(idf, "AirflowNetwork:MultiZone:Surface"):
        surf = surfaces.get(f[0])
        if surf and surf[3].lower() == LIVING_ZONE and f[1] in ela and not f[1].lower().startswith("zoneleak_garage"):
            living_ela += ela[f[1]]

    cooling = objects(idf, "Coil:Cooling:DX:SingleSpeed")[0]
    furnace = objects(idf, "Coil:Heating:Fuel")[0]
    return {
        "people_per_dwelling": _f(people[4]),
        "occupancy_schedule": people[2],
        "activity_schedule": activity,
        "lights": lights,
        "electric_equipment": electric,
        "gas_equipment": gas,
        "heating_setpoint_schedule": dual[1],
        "cooling_setpoint_schedule": dual[2],
        "hot_water": hot_water,
        "water_heater": {"fuel": heater[10], "efficiency": _f(heater[11]), "tank_m3": _f(heater[1]),
                         "setpoint_schedule": heater[2]},
        "exterior_lights": ext,
        "effective_leakage_area_cm2_at_4pa": round(living_ela * 1e4, 2),
        "hvac": {"cooling_cop": _f(cooling[4]), "furnace_efficiency": _f(furnace[3]), "fuel": furnace[2]},
    }, sorted(s for s in sched_names if s)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build(cache_dir: Path) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lib = {
        "source": {
            "title": "DOE/PNNL residential prototype building models (IECC 2006, single-family)",
            "model": "single-family, gas furnace + DX cooling, slab-on-grade, IECC 2006",
            "url": "https://www.energycodes.gov/prototype-building-models",
            "energyplus_version": None,
            "built_on": date.today().isoformat(),
            "files": {},
        },
        "climate": {},
    }
    base = None
    for cz in CLIMATE_ZONES:
        idf_path, htm_path = fetch(cache_dir, cz)
        for p in (idf_path, htm_path):
            lib["source"]["files"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        text = idf_path.read_text(encoding="latin-1")
        idf = parse_idf(text)
        v = re.search(r"Version,\s*([0-9.]+)", text)
        lib["source"]["energyplus_version"] = v.group(1) if v else None
        env = envelope(htm_path.read_text(encoding="latin-1"))
        ld, sched_names = loads(idf, env["conditioned_area_m2"])
        lib["climate"][cz] = {"model": idf_path.stem,**{k: env[k] for k in env if k not in ("conditioned_area_m2", "envelope_area_m2")},
                              "hvac": ld.pop("hvac"),
                              "effective_leakage_area_cm2_at_4pa": ld.pop("effective_leakage_area_cm2_at_4pa")}
        if base is None:
            base = {
                "conditioned_area_m2": env["conditioned_area_m2"],
                "envelope_area_m2": env["envelope_area_m2"],
                "num_floors": 2,
                "loads": ld,
                "schedules": {n: resolve_schedule(idf, n) for n in sched_names},
            }
        elif ld["lights"] != base["loads"]["lights"]:
            print(f"warning: CZ {cz} lighting differs from {CLIMATE_ZONES[0]}; using {CLIMATE_ZONES[0]}", file=sys.stderr)
    lib.update(base)
    return lib


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--cache-dir", type=Path, default=here / ".resstd_cache")
    ap.add_argument("--out", type=Path, default=here / "ubem_mcp_server" / "urban" / "data" / "residential_prototype.json")
    args = ap.parse_args(argv)
    lib = build(args.cache_dir)
    args.out.write_text(json.dumps(lib, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(lib['climate'])} climate zones, {len(lib['schedules'])} schedules)")


if __name__ == "__main__":
    main()
