"""
Aggregate the simulation results of a building stock.

Reads each building's EnergyPlus SQLite output and produces:
  - buildings.csv      per-building annual energy by fuel and end use, EUI and peaks
  - results.geojson    the stock's footprints with those results, for GIS
  - district_hourly.csv  hourly district demand by fuel (sum over buildings)
  - summary.json       totals, breakdowns by archetype and vintage, coverage
  - map.html           an interactive map of EUI (optional)

Every building of the model set is accounted for: simulated, failed, or
without results, so district totals state what they cover.
"""

import csv
import json
import math
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .results import annual_end_uses, find_sql
from .stock import BuildingStock

J_PER_KWH = 3.6e6
MJ_PER_KWH = 3.6
HOURLY_METERS = {"electricity": "Electricity:Facility", "natural_gas": "NaturalGas:Facility"}


def hourly_meters(sql_path: str, meters: Dict[str, str] = HOURLY_METERS) -> Dict[str, List[float]]:
    """Hourly run-period values (kWh per hour, i.e. average kW) of the given meters."""
    con = sqlite3.connect(f"file:{sql_path}?mode=ro", uri=True)
    try:
        out: Dict[str, List[float]] = {}
        for key, name in meters.items():
            rows = con.execute(
                "SELECT rd.Value FROM ReportData rd "
                "JOIN ReportDataDictionary d ON rd.ReportDataDictionaryIndex = d.ReportDataDictionaryIndex "
                "JOIN Time t ON rd.TimeIndex = t.TimeIndex "
                "JOIN EnvironmentPeriods e ON t.EnvironmentPeriodIndex = e.EnvironmentPeriodIndex "
                "WHERE d.Name = ? AND d.ReportingFrequency = 'Hourly' AND e.EnvironmentType = 3 "
                "AND (t.WarmupFlag IS NULL OR t.WarmupFlag = 0) ORDER BY t.TimeIndex",
                (name,),
            ).fetchall()
            if rows:
                out[key] = [v / J_PER_KWH for (v,) in rows]
        return out
    finally:
        con.close()


def building_result(sql_path: str) -> Dict[str, Any]:
    """Annual and peak results of one simulated building, in kWh."""
    annual = annual_end_uses(sql_path)
    by_fuel = {fuel: sum(uses.values()) / MJ_PER_KWH for fuel, uses in annual["end_uses_mj"].items()}
    end_uses: Dict[str, float] = {}
    for uses in annual["end_uses_mj"].values():
        for use, mj in uses.items():
            end_uses[use] = end_uses.get(use, 0.0) + mj / MJ_PER_KWH
    hourly = hourly_meters(sql_path)
    total = annual["total_mj"] / MJ_PER_KWH
    area = annual["total_area_m2"] or 0.0
    cond = annual["conditioned_area_m2"] or 0.0
    return {
        "total_kwh": total,
        "by_fuel_kwh": by_fuel,
        "by_end_use_kwh": end_uses,
        "floor_area_m2": area,
        "conditioned_area_m2": cond,
        "site_eui_kwh_m2": total / area if area else None,
        "peak_kw": {k: max(v) for k, v in hourly.items() if v},
        "hourly_kw": hourly,
    }


def _quantiles(values: Sequence[float]) -> Optional[Dict[str, float]]:
    v = sorted(x for x in values if x is not None and math.isfinite(x))
    if not v:
        return None
    if len(v) == 1:
        return {"min": v[0], "p25": v[0], "median": v[0], "p75": v[0], "max": v[0]}
    q = statistics.quantiles(v, n=4, method="inclusive")
    return {"min": v[0], "p25": q[0], "median": q[1], "p75": q[2], "max": v[-1]}


def _group(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(r[key], {"buildings": 0, "floor_area_m2": 0.0, "total_kwh": 0.0, "euis": []})
        g["buildings"] += 1
        g["floor_area_m2"] += r["floor_area_m2"]
        g["total_kwh"] += r["total_kwh"]
        g["euis"].append(r["site_eui_kwh_m2"])
    for g in groups.values():
        g["site_eui_kwh_m2"] = g["total_kwh"] / g["floor_area_m2"] if g["floor_area_m2"] else None
        g["eui_distribution"] = _quantiles(g.pop("euis"))
    return dict(sorted(groups.items()))


def aggregate(
    stock: BuildingStock,
    manifest: Dict[str, Any],
    outputs: Dict[str, Dict[str, Any]],
    out_dir: str,
    emission_factors: Optional[Dict[str, float]] = None,
    make_map: bool = True,
) -> Dict[str, Any]:
    """Aggregate a model set's results.

    `outputs` maps building_id -> {"status": ..., "output_directory": ..., "error": ...}
    as reported by the batch runner. `emission_factors` (kg CO2e per kWh, by
    fuel key such as "electricity" or "natural_gas") adds emissions.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    models = {m["building_id"]: m for m in manifest["models"]}
    rows: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    district: Dict[str, List[float]] = {}
    hours: Optional[int] = None

    for bid, m in models.items():
        run = outputs.get(bid)
        if not run or run.get("status") != "success":
            missing.append({"building_id": bid, "status": (run or {}).get("status", "not run"),
                            "error": (run or {}).get("error")})
            continue
        sql = find_sql(run["output_directory"]) if run.get("output_directory") else None
        if not sql:
            missing.append({"building_id": bid, "status": "no results", "error": "no SQLite output found"})
            continue
        try:
            res = building_result(sql)
        except sqlite3.Error as e:
            missing.append({"building_id": bid, "status": "no results", "error": f"unreadable SQLite output: {e}"})
            continue
        hourly = res.pop("hourly_kw")
        lengths = {len(s) for s in hourly.values()}
        if hours is None and lengths:
            hours = lengths.pop() if len(lengths) == 1 else None
        if any(len(s) != hours for s in hourly.values()):
            missing.append({"building_id": bid, "status": "no results",
                            "error": f"hourly series lengths {sorted({len(s) for s in hourly.values()})}, "
                                     f"expected {hours}"})
            continue
        for fuel, series in hourly.items():
            acc = district.setdefault(fuel, [0.0] * hours)
            for i, x in enumerate(series):
                acc[i] += x
        row = {"building_id": bid, "archetype": m["archetype"], "vintage": m["vintage"],
               "num_floors": m["num_floors"], "hvac_system": m["hvac_system"],
               "inferred_fields": m.get("inferred_fields", []), **res}
        if emission_factors:
            row["emissions_kg_co2e"] = sum(kwh * emission_factors.get(f, 0.0) for f, kwh in res["by_fuel_kwh"].items())
        rows.append(row)
    failures = {f["building_id"]: f for f in manifest.get("failures", [])}
    for bid, f in failures.items():
        missing.append({"building_id": bid, "status": "model generation failed", "error": f.get("error")})

    fuels = sorted({f for r in rows for f in r["by_fuel_kwh"]})
    end_uses = sorted({u for r in rows for u in r["by_end_use_kwh"]})
    _write_buildings_csv(out / "buildings.csv", rows, fuels, end_uses, bool(emission_factors))
    _write_results_geojson(out / "results.geojson", stock, rows, missing, fuels)
    _write_hourly_csv(out / "district_hourly.csv", district)

    total_area = sum(r["floor_area_m2"] for r in rows)
    total_kwh = sum(r["total_kwh"] for r in rows)
    peaks = {fuel: _peak(series) for fuel, series in district.items()}
    summary: Dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stock_id": manifest.get("stock_id"),
        "model_set_id": manifest.get("model_set_id"),
        "climate": manifest.get("climate") or {"climate_zone": manifest.get("climate_zone")},
        "coverage": {
            "buildings_in_stock": len(stock),
            "buildings_in_model_set": len(models) + len(failures),
            "buildings_with_results": len(rows),
            "buildings_without_results": len(missing),
            "floor_area_with_results_m2": total_area,
            "note": "district totals and hourly profiles include only buildings with results",
        },
        "district": {
            "total_kwh": total_kwh,
            "by_fuel_kwh": {f: sum(r["by_fuel_kwh"].get(f, 0.0) for r in rows) for f in fuels},
            "by_end_use_kwh": {u: sum(r["by_end_use_kwh"].get(u, 0.0) for r in rows) for u in end_uses},
            "site_eui_kwh_m2": total_kwh / total_area if total_area else None,
            "eui_distribution": _quantiles([r["site_eui_kwh_m2"] for r in rows]),
            "peak_kw": peaks,
            "load_factor": {f: (sum(s) / len(s)) / max(s) for f, s in district.items() if s and max(s) > 0},
        },
        "by_archetype": _group(rows, "archetype"),
        "by_vintage": _group(rows, "vintage"),
        "highest_eui": [{"building_id": r["building_id"], "archetype": r["archetype"],
                         "site_eui_kwh_m2": r["site_eui_kwh_m2"]}
                        for r in sorted(rows, key=lambda r: -(r["site_eui_kwh_m2"] or 0))[:10]],
        "without_results": missing,
        "files": {"buildings_csv": str(out / "buildings.csv"), "results_geojson": str(out / "results.geojson"),
                  "district_hourly_csv": str(out / "district_hourly.csv")},
    }
    if emission_factors:
        summary["district"]["emissions_kg_co2e"] = sum(r["emissions_kg_co2e"] for r in rows)
        summary["emission_factors_kg_per_kwh"] = emission_factors
    if make_map and rows:
        try:
            summary["files"]["map_html"] = write_map(out / "map.html", out / "results.geojson",
                                                     title=f"Site EUI, {manifest.get('stock_id')} / "
                                                           f"{manifest.get('model_set_id')}")
        except ImportError:
            summary["files"]["map_html"] = None
    with open(out / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, default=str)
    summary["files"]["summary_json"] = str(out / "summary.json")
    return summary


def _peak(series: List[float]) -> Dict[str, Any]:
    i = max(range(len(series)), key=series.__getitem__)
    month, day, hour = _hour_to_date(i)
    return {"kw": series[i], "hour_of_year": i + 1, "month": month, "day": day, "hour": hour}


_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _hour_to_date(i: int) -> Tuple[int, int, int]:
    """0-based hour of a 365-day year -> (month, day, hour ending 1-24)."""
    day_of_year, hour = divmod(i, 24)
    month = 0
    while month < 11 and day_of_year >= _DAYS[month]:
        day_of_year -= _DAYS[month]
        month += 1
    return month + 1, day_of_year + 1, hour + 1


def _write_buildings_csv(path: Path, rows, fuels, end_uses, emissions: bool) -> None:
    cols = (["building_id", "archetype", "vintage", "num_floors", "hvac_system", "floor_area_m2",
             "conditioned_area_m2", "total_kwh", "site_eui_kwh_m2"]
            + [f"{f}_kwh" for f in fuels] + [f"peak_{f}_kw" for f in fuels]
            + [f"{u}_kwh" for u in end_uses] + (["emissions_kg_co2e"] if emissions else []) + ["inferred_fields"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            vals = {**{k: r.get(k) for k in cols}, "inferred_fields": ";".join(r["inferred_fields"])}
            vals.update({f"{fu}_kwh": r["by_fuel_kwh"].get(fu, 0.0) for fu in fuels})
            vals.update({f"peak_{fu}_kw": r["peak_kw"].get(fu) for fu in fuels})
            vals.update({f"{u}_kwh": r["by_end_use_kwh"].get(u, 0.0) for u in end_uses})
            w.writerow([_round(vals[c]) for c in cols])


def _round(v):
    return round(v, 3) if isinstance(v, float) else v


def _write_results_geojson(path: Path, stock: BuildingStock, rows, missing, fuels) -> None:
    by_id = {r["building_id"]: r for r in rows}
    status = {m["building_id"]: m["status"] for m in missing}
    fc = stock.to_geojson()
    features = []
    for feat in fc["features"]:
        bid = feat["properties"].get("building_id")
        if bid not in by_id and bid not in status:
            continue
        r = by_id.get(bid)
        props = dict(feat["properties"])
        props["result_status"] = "success" if r else status[bid]
        if r:
            props.update({"site_eui_kwh_m2": _round(r["site_eui_kwh_m2"]), "total_kwh": _round(r["total_kwh"]),
                          "floor_area_m2": _round(r["floor_area_m2"])})
            props.update({f"{f}_kwh": _round(r["by_fuel_kwh"].get(f, 0.0)) for f in fuels})
            if "emissions_kg_co2e" in r:
                props["emissions_kg_co2e"] = _round(r["emissions_kg_co2e"])
        features.append({**feat, "properties": props})
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


def _write_hourly_csv(path: Path, district: Dict[str, List[float]]) -> None:
    fuels = sorted(district)
    n = max((len(s) for s in district.values()), default=0)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hour_of_year", "month", "day", "hour"] + [f"{fu}_kw" for fu in fuels])
        for i in range(n):
            w.writerow([i + 1, *_hour_to_date(i)] + [round(district[fu][i], 3) for fu in fuels])


def write_map(path: Path, geojson_path: Path, title: str = "Site EUI") -> str:
    """Interactive HTML map of site EUI (plotly; base-map tiles load when viewed online)."""
    import plotly.graph_objects as go

    with open(geojson_path, encoding="utf-8") as f:
        fc = json.load(f)
    feats = [ft for ft in fc["features"] if ft["properties"].get("site_eui_kwh_m2") is not None]
    if not feats:
        raise ValueError("no buildings with results to map")
    ids = [ft["properties"]["building_id"] for ft in feats]
    eui = [ft["properties"]["site_eui_kwh_m2"] for ft in feats]
    text = [f"{p['building_id']}<br>{p.get('archetype')} ({p.get('vintage')})<br>"
            f"{p['site_eui_kwh_m2']:.0f} kWh/m²·yr" for p in (ft["properties"] for ft in feats)]
    lons, lats = [], []
    for ft in feats:
        ring = ft["geometry"]["coordinates"][0] if ft["geometry"]["type"] == "Polygon" \
            else ft["geometry"]["coordinates"][0][0]
        lons += [c[0] for c in ring]
        lats += [c[1] for c in ring]
    fig = go.Figure(go.Choroplethmap(
        geojson={"type": "FeatureCollection", "features": feats}, locations=ids,
        featureidkey="properties.building_id", z=eui, text=text, hoverinfo="text",
        colorscale="YlOrRd", marker_line_width=0.5, colorbar_title="kWh/m²·yr"))
    span = max(max(lons) - min(lons), max(lats) - min(lats), 1e-4)
    fig.update_layout(title=title, margin={"l": 0, "r": 0, "t": 40, "b": 0},
                      map={"style": "carto-positron", "zoom": max(1.0, min(18.0, math.log2(360 / span) - 0.5)),
                           "center": {"lon": (max(lons) + min(lons)) / 2, "lat": (max(lats) + min(lats)) / 2}})
    fig.write_html(str(path), include_plotlyjs=True, full_html=True)
    return str(path)
