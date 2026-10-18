"""
Measured energy use and model-versus-measured comparison — new in UBEM-MCP.
See NOTICE.md.

Measured data (utility bills, benchmarking disclosures such as NYC Local Law
84 or Seattle's building benchmarking, campus meters) arrive as a table with
one row per meter or property, annual or monthly. Rows are matched to
buildings by a shared identifier (a parcel or property id) or by location; a
row that covers several buildings (a campus meter, a parcel with several
footprints) is compared with the sum of those buildings' models. Only the
mapped columns are read, so names and addresses in the table are never kept.

Metrics follow ASHRAE Guideline 14: NMBE and CV(RMSE) over months for each
meter, and the same statistics across meters for annual data, plus district
totals and the spread of percent errors that UBEM validation studies report.
"""

import csv
import math
import re
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from shapely.geometry import Point

from .query import value_of

# Energy units -> kWh; area units -> m2.
ENERGY_UNITS = {"kwh": 1.0, "mwh": 1000.0, "gwh": 1e6, "wh": 0.001, "kbtu": 0.29307107, "mbtu": 293.07107,
                "mmbtu": 293.07107, "btu": 0.00029307107, "therm": 29.307107, "therms": 29.307107,
                "mj": 1 / 3.6, "gj": 1000 / 3.6}
AREA_UNITS = {"m2": 1.0, "ft2": 0.09290304, "sf": 0.09290304, "sqft": 0.09290304}
ENERGY_KEYS = ("electricity", "natural_gas", "district_heating", "district_cooling", "total")
AREA_KEY = "floor_area"
G14_MONTHLY = {"nmbe_percent": 5.0, "cvrmse_percent": 15.0}
_MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                           "oct", "nov", "dec"))}
# Unit words count only between non-letters, so "electricity_kwh" and
# "Use (kWh)" match but "gja" or "sfx" do not.
_W = r"(?<![a-z]){}(?![a-z])"
_HINTS = [(r"mmbtu", "mmbtu"), (r"kbtu", "kbtu"), (r"therm", "therm"), (_W.format("mwh"), "mwh"),
          (_W.format("kwh"), "kwh"), (_W.format("gj"), "gj"), (_W.format("mj"), "mj"),
          (r"ft2|ft²|sq\.? ?ft|" + _W.format("sf"), "ft2"), (r"m2|m²", "m2")]


def detect_unit(column: str) -> Optional[str]:
    low = column.lower()
    for pattern, unit in _HINTS:
        if re.search(pattern, low):
            return unit
    return None


def _number(text: Any) -> Optional[float]:
    if text is None:
        return None
    s = str(text).strip().replace(",", "")
    if not s:
        return None
    try:
        x = float(s)
    except ValueError:
        return None  # "Not Available", "N/A", ...
    return x if math.isfinite(x) else None


def _norm_id(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    if re.fullmatch(r"-?\d+\.0+", s):
        s = s.split(".")[0]
    return s or None


def _month(v: Any) -> Optional[int]:
    s = str(v or "").strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})", s)
    if m:
        return int(m.group(2))
    if s.isdigit() and 1 <= int(s) <= 12:
        return int(s)
    return _MONTHS.get(s[:3])


class _Groups:
    """Union-find over rows that share buildings."""

    def __init__(self):
        self.parent: Dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        self.parent[self.find(a)] = self.find(b)


def load_measured(stock, rows: Sequence[Dict[str, str]], columns: Dict[str, str],
                  units: Optional[Dict[str, str]] = None, id_column: Optional[str] = None,
                  match_field: Optional[str] = None, lat_column: Optional[str] = None,
                  lon_column: Optional[str] = None, month_column: Optional[str] = None,
                  max_distance_m: float = 10.0) -> Dict[str, Any]:
    """Match measured rows to a stock's buildings and convert to kWh and m2.

    `columns` maps electricity / natural_gas / district_heating /
    district_cooling / total / floor_area to column names. Units come from
    `units` or are read from the column names, e.g. "Electricity Use (kWh)".
    """
    bad = set(columns) - set(ENERGY_KEYS) - {AREA_KEY}
    if bad:
        raise ValueError(f"Unknown measured quantities {sorted(bad)}; use {list(ENERGY_KEYS) + [AREA_KEY]}")
    if not set(columns) & set(ENERGY_KEYS):
        raise ValueError("Map at least one energy column, e.g. columns={'electricity': 'Electricity Use (kWh)'}")
    header = set(rows[0]) if rows else set()
    needed = list(columns.values()) + [c for c in (id_column, lat_column, lon_column, month_column) if c]
    missing = [c for c in needed if c not in header]
    if missing:
        raise KeyError(f"Column(s) not in the table: {missing}; available: {sorted(header)[:40]}")
    if bool(lat_column) != bool(lon_column):
        raise ValueError("Give both lat_column and lon_column")
    if not id_column and not lat_column:
        raise ValueError("Give id_column (with match_field) or lat_column and lon_column to match rows to buildings")

    factors: Dict[str, float] = {}
    for key, col in columns.items():
        unit = ((units or {}).get(key) or detect_unit(col) or "").lower()
        table = AREA_UNITS if key == AREA_KEY else ENERGY_UNITS
        if unit not in table:
            raise ValueError(f"Unit of {key} column {col!r} unknown; give units={{'{key}': one of {sorted(table)}}}")
        factors[key] = table[unit]

    # Building index for matching
    by_id: Dict[str, List[str]] = {}
    if id_column:
        field = match_field or "building_id"
        for b in stock.buildings:
            v = _norm_id(b.building_id if field == "building_id" else value_of(b, field))
            if v:
                by_id.setdefault(v, []).append(b.building_id)
        if not by_id:
            raise KeyError(f"No building has a value for {field!r}; check match_field")
    tree = None
    if lat_column:
        from shapely import STRtree
        footprints = [b.footprint for b in stock.buildings]
        tree = STRtree(footprints)

    groups = _Groups()
    matched_rows: List[Tuple[int, List[str], Dict[str, Optional[float]], Optional[int]]] = []
    unmatched: List[str] = []
    no_data = 0
    for i, row in enumerate(rows):
        values = {k: (None if (x := _number(row.get(c))) is None else x * factors[k]) for k, c in columns.items()}
        if all(values[k] is None for k in values if k != AREA_KEY):
            no_data += 1
            continue
        month = _month(row.get(month_column)) if month_column else None
        if month_column and month is None:
            unmatched.append(f"row {i + 2}: unreadable month {row.get(month_column)!r}")
            continue
        ids: List[str] = []
        if id_column:
            ids = by_id.get(_norm_id(row.get(id_column)) or "", [])
        if not ids and tree is not None:
            lat, lon = _number(row.get(lat_column)), _number(row.get(lon_column))
            if lat is not None and lon is not None:
                pt = stock.frame.to_local(Point(lon, lat))
                hits = [stock.buildings[int(j)] for j in tree.query(pt.buffer(max_distance_m))]
                if hits:
                    best = min(hits, key=lambda b: b.footprint.distance(pt))
                    ids = [best.building_id]
        if not ids:
            unmatched.append(str(row.get(id_column)) if id_column else f"row {i + 2}")
            continue
        matched_rows.append((i, ids, values, month))

    # Rows that share a building form one group, compared with the sum of its buildings.
    first_row_of: Dict[str, int] = {}
    for i, ids, _, _ in matched_rows:
        for bid in ids:
            if bid in first_row_of:
                groups.union(i, first_row_of[bid])
            else:
                first_row_of[bid] = i
    out: Dict[int, Dict[str, Any]] = {}
    for i, ids, values, month in matched_rows:
        g = out.setdefault(groups.find(i), {"building_ids": set(), "rows": 0, "annual": {}, "monthly": {},
                                            "floor_area_m2": None, "_areas": {}})
        g["building_ids"].update(ids)
        g["rows"] += 1
        if values.get(AREA_KEY) is not None:
            g["_areas"][i] = values[AREA_KEY]
        for k, v in values.items():
            if k == AREA_KEY or v is None:
                continue
            if month:
                g["monthly"].setdefault(k, [None] * 12)
                g["monthly"][k][month - 1] = (g["monthly"][k][month - 1] or 0.0) + v
            else:
                g["annual"][k] = g["annual"].get(k, 0.0) + v

    result_groups = []
    for n, g in enumerate(sorted(out.values(), key=lambda g: sorted(g["building_ids"])[0])):
        monthly = g.pop("monthly")
        annual = g.pop("annual")
        for k, series in monthly.items():
            if all(x is not None for x in series):
                annual[k] = sum(series)
        if g["_areas"]:
            # Monthly tables repeat the area on every row; distinct rows of an annual table add up.
            g["floor_area_m2"] = (max(g["_areas"].values()) if monthly else sum(g["_areas"].values()))
        del g["_areas"]
        result_groups.append({"group_id": f"g{n + 1:04d}", "building_ids": sorted(g["building_ids"]),
                              "rows": g["rows"], "annual_kwh": annual,
                              **({"monthly_kwh": monthly} if monthly else {}),
                              "floor_area_m2": g["floor_area_m2"]})
    return {
        "quantities": sorted(columns), "monthly": bool(month_column), "units_to_kwh_or_m2": factors,
        "groups": result_groups,
        "match": {"method": "id" if id_column and not lat_column else "location" if not id_column else "id, then location",
                  "rows": len(rows), "rows_matched": len(matched_rows), "rows_unmatched": len(unmatched),
                  "rows_without_data": no_data,
                  "buildings_matched": len(first_row_of), "groups": len(result_groups),
                  "groups_with_several_buildings": sum(1 for g in result_groups if len(g["building_ids"]) > 1),
                  "unmatched_examples": unmatched[:10]},
    }


def read_table(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def monthly_from_hourly(series: Sequence[float]) -> List[float]:
    days = [31, 29 if len(series) >= 8784 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    out, i = [], 0
    for d in days:
        out.append(sum(series[i:i + 24 * d]))
        i += 24 * d
    return out


def g14(measured: Sequence[float], modeled: Sequence[float], p: int = 1) -> Optional[Dict[str, float]]:
    """ASHRAE Guideline 14 NMBE and CV(RMSE), in percent."""
    pairs = [(m, s) for m, s in zip(measured, modeled) if m is not None and s is not None]
    n = len(pairs)
    if n - p <= 0:
        return None
    mean = sum(m for m, _ in pairs) / n
    if mean == 0:
        return None
    nmbe = sum(m - s for m, s in pairs) / ((n - p) * mean) * 100
    cv = math.sqrt(sum((m - s) ** 2 for m, s in pairs) / (n - p)) / mean * 100
    return {"n": n, "nmbe_percent": round(nmbe, 2), "cvrmse_percent": round(cv, 2)}


def _pct(model: float, meas: float) -> Optional[float]:
    return round((model - meas) / meas * 100, 1) if meas else None


def _spread(errors: Iterable[Optional[float]]) -> Optional[Dict[str, Any]]:
    e = [x for x in errors if x is not None]
    if not e:
        return None
    a = [abs(x) for x in e]
    return {"n": len(e), "median_percent_error": round(statistics.median(e), 1),
            "median_absolute_percent_error": round(statistics.median(a), 1),
            "within_10_percent": round(sum(x <= 10 for x in a) / len(a), 3),
            "within_25_percent": round(sum(x <= 25 for x in a) / len(a), 3),
            "within_50_percent": round(sum(x <= 50 for x in a) / len(a), 3)}


# Annual site EUI (kWh/m2) outside this range is almost always a data error
# (a missing meter, a unit slip, floor area of the wrong building).
PLAUSIBLE_EUI_KWH_M2 = (10.0, 2000.0)


def compare(dataset: Dict[str, Any], modeled: Dict[str, Dict[str, Any]], archetypes: Dict[str, Optional[str]],
            p: int = 1, weather_note: Optional[str] = None) -> Dict[str, Any]:
    """Compare measured groups with modeled buildings.

    `modeled[building_id]` = {"annual_kwh": {fuel: kWh}, "monthly_kwh": {fuel: [12]}, "floor_area_m2"}.
    Meters whose measured EUI is implausible are listed but left out of the
    statistics.
    """
    rows, skipped = [], []
    for g in dataset["groups"]:
        missing = [b for b in g["building_ids"] if b not in modeled]
        if missing:
            skipped.append({"group_id": g["group_id"], "reason": "no simulation results",
                            "buildings": missing[:5]})
            continue
        mods = [modeled[b] for b in g["building_ids"]]
        model_annual: Dict[str, float] = {}
        for m in mods:
            for fuel, v in m["annual_kwh"].items():
                model_annual[fuel] = model_annual.get(fuel, 0.0) + v
        model_annual["total"] = sum(v for k, v in model_annual.items() if k != "total")
        area_model = sum(m.get("floor_area_m2") or 0.0 for m in mods)
        row: Dict[str, Any] = {"group_id": g["group_id"], "building_ids": g["building_ids"],
                               "archetypes": sorted({archetypes.get(b) or "unknown" for b in g["building_ids"]}),
                               "measured_kwh": {}, "modeled_kwh": {}, "percent_error": {}}
        meas_annual = dict(g["annual_kwh"])
        if "total" not in meas_annual and set(meas_annual) - {"total"}:
            # Compare like with like: the total of the fuels that were measured.
            meas_annual["total_of_measured_fuels"] = sum(meas_annual.values())
            model_annual["total_of_measured_fuels"] = sum(model_annual.get(f, 0.0) for f in g["annual_kwh"])
        for key, meas in meas_annual.items():
            mod = model_annual.get(key, 0.0)
            row["measured_kwh"][key] = round(meas, 1)
            row["modeled_kwh"][key] = round(mod, 1)
            row["percent_error"][key] = _pct(mod, meas)
        row["floor_area_m2"] = {"measured": g.get("floor_area_m2"), "modeled": round(area_model, 1) or None}
        tot_key = "total" if "total" in meas_annual else "total_of_measured_fuels"
        if g.get("floor_area_m2") and area_model and tot_key in meas_annual:
            row["eui_kwh_m2"] = {"measured": round(meas_annual[tot_key] / g["floor_area_m2"], 1),
                                 "modeled": round(model_annual[tot_key] / area_model, 1)}
            row["area_ratio"] = round(area_model / g["floor_area_m2"], 2)
            lo, hi = PLAUSIBLE_EUI_KWH_M2
            if not lo <= row["eui_kwh_m2"]["measured"] <= hi:
                row["suspect"] = (f"measured EUI {row['eui_kwh_m2']['measured']} kWh/m2 is outside "
                                  f"{lo:g}-{hi:g}; probably a data error")
        if g.get("monthly_kwh"):
            row["monthly"] = {}
            for fuel, meas_m in g["monthly_kwh"].items():
                if fuel == "total":
                    mod_m = [sum(m["monthly_kwh"].get(f, [0.0] * 12)[k] for m in mods for f in m["monthly_kwh"])
                             for k in range(12)]
                else:
                    mod_m = [sum(m["monthly_kwh"].get(fuel, [0.0] * 12)[k] for m in mods) for k in range(12)]
                stats = g14(meas_m, mod_m, p)
                if stats:
                    stats["meets_guideline14_monthly"] = (abs(stats["nmbe_percent"]) <= G14_MONTHLY["nmbe_percent"]
                                                          and stats["cvrmse_percent"] <= G14_MONTHLY["cvrmse_percent"])
                    row["monthly"][fuel] = stats
        rows.append(row)

    suspect = [r for r in rows if r.get("suspect")]
    all_rows, rows = rows, [r for r in rows if not r.get("suspect")]
    keys = sorted({k for r in rows for k in r["measured_kwh"]})
    district, across, spread = {}, {}, {}
    for k in keys:
        rs = [r for r in rows if k in r["measured_kwh"]]
        meas = [r["measured_kwh"][k] for r in rs]
        mod = [r["modeled_kwh"][k] for r in rs]
        district[k] = {"meters": len(rs), "measured_kwh": round(sum(meas)), "modeled_kwh": round(sum(mod)),
                       "percent_error": _pct(sum(mod), sum(meas))}
        across[k] = g14(meas, mod, p)
        spread[k] = _spread(r["percent_error"][k] for r in rs)
    main_key = "total" if "total" in keys else ("total_of_measured_fuels" if "total_of_measured_fuels" in keys
                                                 else (keys[0] if keys else None))
    by_arch: Dict[str, List[float]] = {}
    for r in rows:
        if main_key and len(r["archetypes"]) == 1 and r["percent_error"].get(main_key) is not None:
            by_arch.setdefault(r["archetypes"][0], []).append(r["percent_error"][main_key])
    caveats = []
    if weather_note:
        caveats.append(weather_note)
    area_off = [r for r in rows if r.get("area_ratio") and abs(r["area_ratio"] - 1) > 0.25]
    if area_off:
        caveats.append(f"{len(area_off)} meter(s) have modeled floor area more than 25% from the measured floor "
                       f"area; compare their EUI rather than totals")
    if skipped:
        caveats.append(f"{len(skipped)} meter(s) were not compared because some of their buildings have no "
                       f"simulation results")
    if suspect:
        caveats.append(f"{len(suspect)} meter(s) left out of the statistics because their measured EUI is "
                       f"implausible ({', '.join(r['group_id'] for r in suspect[:5])}); check those records")
    monthly_rows = [r for r in rows if r.get("monthly")]
    summary: Dict[str, Any] = {
        "meters_compared": len(rows), "meters_skipped": len(skipped), "main_quantity": main_key,
        "meters_suspect": [{"group_id": r["group_id"], "building_ids": r["building_ids"][:5],
                            "reason": r["suspect"]} for r in suspect],
        "district": district, "across_meters_guideline14": across, "percent_error_spread": spread,
        "by_archetype": {a: {"meters": len(v), "median_percent_error": round(statistics.median(v), 1)}
                         for a, v in sorted(by_arch.items())},
        "largest_differences": [{"group_id": r["group_id"], "building_ids": r["building_ids"][:5],
                                 "archetypes": r["archetypes"], "percent_error": r["percent_error"].get(main_key)}
                                for r in sorted((r for r in rows if r["percent_error"].get(main_key) is not None),
                                                key=lambda r: -abs(r["percent_error"][main_key]))[:10]],
        "caveats": caveats, "p": p,
    }
    if monthly_rows:
        stats = [s for r in monthly_rows for s in r["monthly"].values()]
        summary["monthly_guideline14"] = {
            "thresholds": G14_MONTHLY, "meter_fuel_pairs": len(stats),
            "meeting_thresholds": sum(1 for s in stats if s["meets_guideline14_monthly"]),
            "note": "Guideline 14 thresholds are written for calibrating individual buildings; archetype "
                    "models are not calibrated, so they are a reference, not a pass mark"}
    return {"summary": summary, "rows": all_rows, "skipped": skipped}
