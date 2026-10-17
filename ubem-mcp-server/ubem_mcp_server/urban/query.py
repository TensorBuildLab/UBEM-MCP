"""
Query and export a building stock without running any model.

New in UBEM-MCP (not part of EnergyPlus-MCP). See NOTICE.md.

Answers are summaries sized for an AI assistant's context: counts, totals and
data completeness per group, plus a few sample rows. The full table goes to a
file through export_stock, never into the response.
"""

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from shapely.geometry import mapping

from .stock import Building, BuildingStock

# Attributes every building has, in the order export writes them.
STANDARD_FIELDS = ("building_id", "archetype", "use", "year_built", "vintage", "num_floors", "height_m",
                   "footprint_area_m2", "gross_floor_area_m2")
NUMERIC_FIELDS = {"year_built", "num_floors", "height_m", "footprint_area_m2", "gross_floor_area_m2"}
COMPLETENESS_FIELDS = ("archetype", "use", "year_built", "num_floors", "height_m")
AGGREGATES: Dict[str, Callable[[List[float]], float]] = {
    "sum": sum, "mean": statistics.fmean, "median": statistics.median, "min": min, "max": max,
}
DEFAULT_METRICS = ("sum:footprint_area_m2", "sum:gross_floor_area_m2", "median:year_built")
MAX_GROUPS = 200
MAX_SAMPLE = 50


def value_of(b: Building, name: str) -> Any:
    """A building attribute by name: a standard field, `decade` (of year built),
    or any column of the source data (case-insensitive)."""
    if name in STANDARD_FIELDS:
        v = getattr(b, name)
        return round(v, 2) if isinstance(v, float) else v
    if name == "decade":
        return None if b.year_built is None else b.year_built // 10 * 10
    props = b.source_properties
    if name in props:
        return props[name]
    lower = {k.lower(): k for k in props}
    key = lower.get(name.lower())
    return props[key] if key else None


def _matches(v: Any, cond: Any) -> bool:
    if isinstance(cond, dict):
        if "is_null" in cond:
            return (v is None) == bool(cond["is_null"])
        if v is None:
            return False
        try:
            num = float(v)
        except (TypeError, ValueError):
            return False
        if "min" in cond and num < float(cond["min"]):
            return False
        if "max" in cond and num > float(cond["max"]):
            return False
        return True
    if isinstance(cond, list):
        return any(_matches(v, c) for c in cond)
    if v is None or cond is None:
        return v is None and cond is None
    return str(v).strip().lower() == str(cond).strip().lower()


def select(stock: BuildingStock, filters: Optional[Dict[str, Any]] = None) -> List[Building]:
    """Buildings matching every filter. A filter value is a value, a list of
    values (any of), {"min": x, "max": y}, or {"is_null": true|false}."""
    filters = filters or {}
    return [b for b in stock.buildings if all(_matches(value_of(b, k), c) for k, c in filters.items())]


def _parse_metric(spec: str) -> Tuple[str, str]:
    agg, _, attr = spec.partition(":")
    if agg == "count" and not attr:
        return "count", ""
    if agg not in AGGREGATES or not attr:
        raise ValueError(f"Bad metric {spec!r}: use count or <{'|'.join(AGGREGATES)}>:<attribute>, "
                         f"e.g. sum:gross_floor_area_m2")
    return agg, attr


def _numbers(buildings: Sequence[Building], attr: str) -> List[float]:
    out = []
    for b in buildings:
        v = value_of(b, attr)
        try:
            if v is not None and v != "":
                out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _summarize(buildings: Sequence[Building], metrics: Sequence[Tuple[str, str]],
               model_mode: bool) -> Dict[str, Any]:
    n = len(buildings)
    out: Dict[str, Any] = {"count": n}
    for agg, attr in metrics:
        if agg == "count":
            continue
        values = _numbers(buildings, attr)
        out[f"{agg}:{attr}"] = round(AGGREGATES[agg](values), 2) if values else None
        if len(values) < n:
            out[f"{agg}:{attr}:known_for"] = len(values)
    out["known_share"] = {a: round(sum(1 for b in buildings if getattr(b, a) is not None) / n, 3)
                          for a in COMPLETENESS_FIELDS} if n else {}
    if model_mode and n:
        out["inferred_share"] = {a: round(sum(1 for b in buildings if a in b.inferred_fields) / n, 3)
                                 for a in ("archetype", "year_built", "num_floors", "height_m")}
    return out


def sample_row(b: Building, extra: Sequence[str] = ()) -> Dict[str, Any]:
    row = {k: value_of(b, k) for k in STANDARD_FIELDS}
    for k in extra:
        if k not in row:
            row[k] = value_of(b, k)
    if b.inferred_fields:
        row["inferred_fields"] = list(b.inferred_fields)
    return row


def query(stock: BuildingStock, filters: Optional[Dict[str, Any]] = None,
          group_by: Optional[Sequence[str]] = None, metrics: Optional[Sequence[str]] = None,
          max_groups: int = 20, sample: int = 5, offset: int = 0,
          columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    parsed = [_parse_metric(m) for m in (metrics or DEFAULT_METRICS)]
    model_mode = stock.mode == "model"
    matched = select(stock, filters)
    result: Dict[str, Any] = {
        "mode": stock.mode,
        "filters": filters or {},
        "num_buildings": len(stock.buildings),
        "num_matching": len(matched),
        "overall": _summarize(matched, parsed, model_mode),
    }
    if group_by:
        groups: Dict[Tuple, List[Building]] = defaultdict(list)
        for b in matched:
            key = tuple(value_of(b, g) for g in group_by)
            groups[tuple("unknown" if v is None else v for v in key)].append(b)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
        cap = max(1, min(max_groups, MAX_GROUPS))
        result["group_by"] = list(group_by)
        result["num_groups"] = len(ordered)
        result["groups"] = [{**dict(zip(group_by, key)), **_summarize(bs, parsed, model_mode)}
                            for key, bs in ordered[:cap]]
        if len(ordered) > cap:
            rest = [b for _, bs in ordered[cap:] for b in bs]
            result["other_groups"] = {"groups": len(ordered) - cap, **_summarize(rest, parsed, model_mode)}
    n_sample = max(0, min(sample, MAX_SAMPLE))
    page = matched[offset:offset + n_sample]
    result["sample"] = [sample_row(b, columns or ()) for b in page]
    if offset + n_sample < len(matched):
        result["next_offset"] = offset + n_sample
    return result


def export(stock: BuildingStock, path: Path, fmt: str = "csv", filters: Optional[Dict[str, Any]] = None,
           columns: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Write the (filtered) stock to CSV (one row per building, centroid
    lon/lat) or GeoJSON (full geometry). `columns` adds source-data columns."""
    rows = select(stock, filters)
    extra = [c for c in (columns or ()) if c not in STANDARD_FIELDS]
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(list(STANDARD_FIELDS) + ["longitude", "latitude", "inferred_fields"] + extra)
            for b in rows:
                c = stock.frame.to_wgs84(b.footprint.centroid)
                w.writerow([value_of(b, k) for k in STANDARD_FIELDS]
                           + [round(c.x, 6), round(c.y, 6), ";".join(b.inferred_fields)]
                           + [value_of(b, k) for k in extra])
    elif fmt == "geojson":
        feats = []
        for b in rows:
            geom = b.full_geometry if b.full_geometry is not None else b.footprint
            props = {k: value_of(b, k) for k in STANDARD_FIELDS}
            props["inferred_fields"] = list(b.inferred_fields)
            props.update({k: value_of(b, k) for k in extra})
            feats.append({"type": "Feature", "id": b.building_id,
                          "geometry": mapping(stock.frame.to_wgs84(geom)), "properties": props})
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f)
    else:
        raise ValueError("format must be 'csv' or 'geojson'")
    return {"path": str(path), "format": fmt, "rows": len(rows), "columns": list(STANDARD_FIELDS) + extra}
