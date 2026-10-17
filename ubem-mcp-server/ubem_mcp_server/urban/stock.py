"""
Building stock: a set of building footprints with normalized attributes,
ready for archetype model generation.

Footprints come in as GeoJSON (any CRS pyproj understands; RFC 7946 WGS84 by
default) and are projected to a local azimuthal-equidistant frame centered on
the stock, so geometry is in metres with the origin near the site. Every
attribute that had to be guessed rather than read from the source is listed
in the building's `inferred_fields`, so data quality stays visible downstream.
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.polygon import orient
import shapely
from shapely.validation import make_valid

from .archetypes import ARCHETYPES, classify_use, parse_year, resolve_archetype_key, vintage_for_year

# Candidate source property names, checked case-insensitively in order.
HEIGHT_FIELDS = ("height_m", "height", "building_height", "bldg_height", "measuredheight", "roof_height")
FLOORS_FIELDS = ("num_floors", "building:levels", "levels", "floors", "stories", "storeys", "num_stories")
USE_FIELDS = ("archetype", "use_type", "building_type", "usage", "use", "landuse", "land_use")
OSM_USE_FIELDS = ("amenity", "shop", "office", "healthcare", "tourism", "building")
YEAR_FIELDS = ("year_built", "yearbuilt", "construction_year", "start_date", "year_of_construction", "year")
ID_FIELDS = ("building_id", "osm_id", "@id", "id", "objectid", "fid")

DEFAULT_MIN_FOOTPRINT_M2 = 20.0
_FT_TO_M = 0.3048


@dataclass
class Building:
    building_id: str
    footprint: Polygon  # local metric frame, counter-clockwise exterior, no holes
    archetype: str
    year_built: Optional[int]
    vintage: str
    num_floors: int
    floor_to_floor_m: float
    height_m: float
    inferred_fields: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    source_properties: Dict[str, Any] = field(default_factory=dict)

    @property
    def footprint_area_m2(self) -> float:
        return self.footprint.area

    @property
    def gross_floor_area_m2(self) -> float:
        return self.footprint.area * self.num_floors

    def to_properties(self) -> Dict[str, Any]:
        return {
            "building_id": self.building_id,
            "archetype": self.archetype,
            "year_built": self.year_built,
            "vintage": self.vintage,
            "num_floors": self.num_floors,
            "floor_to_floor_m": round(self.floor_to_floor_m, 3),
            "height_m": round(self.height_m, 2),
            "footprint_area_m2": round(self.footprint_area_m2, 1),
            "gross_floor_area_m2": round(self.gross_floor_area_m2, 1),
            "inferred_fields": list(self.inferred_fields),
            "notes": list(self.notes),
            "source_properties": self.source_properties,
        }


@dataclass
class ExcludedFeature:
    feature_id: str
    reason: str


def _reproject(fn, geom):
    """Apply a pyproj ``transform(x, y)`` callable to every coordinate of `geom`."""
    return shapely.transform(geom, lambda x, y: fn(x, y), interleaved=False)


class LocalFrame:
    """Azimuthal-equidistant projection centered on (lon0, lat0), in metres."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        local = CRS.from_proj4(f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
        self._fwd = Transformer.from_crs("EPSG:4326", local, always_xy=True)
        self._inv = Transformer.from_crs(local, "EPSG:4326", always_xy=True)

    def to_local(self, geom):
        return _reproject(self._fwd.transform, geom)

    def to_wgs84(self, geom):
        return _reproject(self._inv.transform, geom)


class BuildingStock:
    def __init__(self, buildings: List[Building], frame: LocalFrame,
                 excluded: Optional[List[ExcludedFeature]] = None, source: str = ""):
        self.buildings = buildings
        self.frame = frame
        self.excluded = excluded or []
        self.source = source

    def __len__(self) -> int:
        return len(self.buildings)

    def get(self, building_id: str) -> Building:
        for b in self.buildings:
            if b.building_id == building_id:
                return b
        raise KeyError(f"Unknown building_id: {building_id}")

    def filter(self, archetypes: Optional[Sequence[str]] = None, vintages: Optional[Sequence[str]] = None,
               min_gfa_m2: Optional[float] = None, max_gfa_m2: Optional[float] = None,
               building_ids: Optional[Sequence[str]] = None) -> List[Building]:
        out = []
        ids = set(building_ids) if building_ids else None
        for b in self.buildings:
            if archetypes and b.archetype not in archetypes:
                continue
            if vintages and b.vintage not in vintages:
                continue
            if min_gfa_m2 is not None and b.gross_floor_area_m2 < min_gfa_m2:
                continue
            if max_gfa_m2 is not None and b.gross_floor_area_m2 > max_gfa_m2:
                continue
            if ids is not None and b.building_id not in ids:
                continue
            out.append(b)
        return out

    def summary(self) -> Dict[str, Any]:
        n = len(self.buildings)
        inferred = Counter(f for b in self.buildings for f in b.inferred_fields)
        bounds = None
        if self.buildings:
            minx = min(b.footprint.bounds[0] for b in self.buildings)
            miny = min(b.footprint.bounds[1] for b in self.buildings)
            maxx = max(b.footprint.bounds[2] for b in self.buildings)
            maxy = max(b.footprint.bounds[3] for b in self.buildings)
            bounds = {"width_m": round(maxx - minx, 1), "depth_m": round(maxy - miny, 1)}
        return {
            "source": self.source,
            "origin_lon_lat": [round(self.frame.lon0, 6), round(self.frame.lat0, 6)],
            "num_buildings": n,
            "num_excluded": len(self.excluded),
            "excluded_by_reason": dict(Counter(_reason_class(e.reason) for e in self.excluded)),
            "by_archetype": dict(Counter(b.archetype for b in self.buildings).most_common()),
            "by_vintage": dict(Counter(b.vintage for b in self.buildings).most_common()),
            "total_footprint_area_m2": round(sum(b.footprint_area_m2 for b in self.buildings), 1),
            "total_gross_floor_area_m2": round(sum(b.gross_floor_area_m2 for b in self.buildings), 1),
            "inferred_share": {k: round(v / n, 3) for k, v in sorted(inferred.items())} if n else {},
            "notes_by_type": dict(Counter(_note_class(note) for b in self.buildings for note in b.notes).most_common()),
            "extent": bounds,
        }

    def to_geojson(self) -> Dict[str, Any]:
        features = []
        for b in self.buildings:
            features.append({
                "type": "Feature",
                "id": b.building_id,
                "geometry": mapping(self.frame.to_wgs84(b.footprint)),
                "properties": b.to_properties(),
            })
        return {
            "type": "FeatureCollection",
            "ubem_mcp": {"origin_lon_lat": [self.frame.lon0, self.frame.lat0], "source": self.source},
            "features": features,
        }


def _reason_class(reason: str) -> str:
    return reason.split(":", 1)[0]


def _note_class(note: str) -> str:
    """Group notes by kind, dropping building-specific details."""
    note = re.sub(r"'[^']*'", "'…'", note)
    return re.sub(r"\d+(\.\d+)?", "N", note.split(" (")[0])


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_geojson(path: str, **kwargs) -> BuildingStock:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return stock_from_geojson(data, source=path, **kwargs)


def stock_from_geojson(
    data: Dict[str, Any],
    source: str = "",
    source_crs: Optional[str] = None,
    height_field: Optional[str] = None,
    floors_field: Optional[str] = None,
    use_field: Optional[str] = None,
    year_field: Optional[str] = None,
    id_field: Optional[str] = None,
    height_unit: str = "m",
    default_archetype: str = "single_family",
    default_year: int = 1980,
    min_footprint_m2: float = DEFAULT_MIN_FOOTPRINT_M2,
) -> BuildingStock:
    """Build a BuildingStock from a GeoJSON FeatureCollection.

    Field arguments override auto-detection of the attribute columns.
    `default_archetype` and `default_year` fill buildings whose use or age
    can't be determined (and are recorded in `inferred_fields`).
    """
    if resolve_archetype_key(default_archetype) is None:
        raise ValueError(f"Unknown default_archetype {default_archetype!r}; valid: {sorted(ARCHETYPES)}")
    default_archetype = resolve_archetype_key(default_archetype)
    if height_unit not in ("m", "ft"):
        raise ValueError("height_unit must be 'm' or 'ft'")

    features = data.get("features") if data.get("type") == "FeatureCollection" else None
    if features is None:
        raise ValueError("Expected a GeoJSON FeatureCollection")

    crs_name = source_crs or _legacy_crs_name(data) or "EPSG:4326"
    to_wgs84 = None
    if not CRS.from_user_input(crs_name).equals(CRS.from_epsg(4326)):
        to_wgs84 = Transformer.from_crs(crs_name, "EPSG:4326", always_xy=True).transform

    excluded: List[ExcludedFeature] = []
    parsed: List[Tuple[str, Polygon, Dict[str, Any], List[str]]] = []
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        fid = _feature_id(feat, props, id_field, i)
        geom_json = feat.get("geometry")
        if not geom_json:
            excluded.append(ExcludedFeature(fid, "no_geometry"))
            continue
        try:
            geom = shape(geom_json)
        except Exception as e:  # noqa: BLE001 - malformed input geometry
            excluded.append(ExcludedFeature(fid, f"bad_geometry: {e}"))
            continue
        if to_wgs84 is not None:
            geom = _reproject(to_wgs84, geom)
        poly, notes = _single_polygon(geom)
        if poly is None:
            excluded.append(ExcludedFeature(fid, f"not_a_polygon: {geom.geom_type}"))
            continue
        parsed.append((fid, poly, props, notes))

    if not parsed:
        raise ValueError(f"No usable building polygons in {source or 'input'} ({len(excluded)} excluded)")

    # A stock saved by to_geojson() records its origin; reuse it so local
    # coordinates are identical after a round trip.
    saved_origin = (data.get("ubem_mcp") or {}).get("origin_lon_lat")
    lon0, lat0 = saved_origin if saved_origin else _centroid_lon_lat(p for _, p, _, _ in parsed)
    frame = LocalFrame(lon0, lat0)

    buildings: List[Building] = []
    seen_ids: Counter = Counter()
    for fid, poly_ll, props, notes in parsed:
        poly = _clean_local(frame.to_local(poly_ll))
        if poly is None or poly.area < min_footprint_m2:
            area = 0.0 if poly is None else poly.area
            excluded.append(ExcludedFeature(fid, f"too_small: {area:.1f} m2 < {min_footprint_m2} m2"))
            continue
        b = _normalize_attributes(
            fid, poly, props, notes,
            height_field=height_field, floors_field=floors_field, use_field=use_field,
            year_field=year_field, height_unit=height_unit,
            default_archetype=default_archetype, default_year=default_year,
        )
        if isinstance(b, ExcludedFeature):
            excluded.append(b)
            continue
        seen_ids[b.building_id] += 1
        if seen_ids[b.building_id] > 1:
            b.building_id = f"{b.building_id}_{seen_ids[b.building_id]}"
        buildings.append(b)

    return BuildingStock(buildings, frame, excluded, source=source)


def _legacy_crs_name(data: Dict[str, Any]) -> Optional[str]:
    crs = data.get("crs")
    if isinstance(crs, dict):
        name = (crs.get("properties") or {}).get("name")
        if name:
            m = re.search(r"EPSG:+(\d+)", name)
            return f"EPSG:{m.group(1)}" if m else name
    return None


def _feature_id(feat, props, id_field, index) -> str:
    if id_field and props.get(id_field) not in (None, ""):
        return str(props[id_field])
    lower = {k.lower(): v for k, v in props.items()}
    for k in ID_FIELDS:
        if lower.get(k) not in (None, ""):
            return str(lower[k])
    if feat.get("id") not in (None, ""):
        return str(feat["id"])
    return f"bldg_{index:05d}"


def _single_polygon(geom) -> Tuple[Optional[Polygon], List[str]]:
    notes: List[str] = []
    if not geom.is_valid:
        geom = make_valid(geom)
        notes.append("geometry repaired")
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return None, notes
        geom = max(polys, key=lambda g: g.area)
    if isinstance(geom, MultiPolygon):
        parts = sorted(geom.geoms, key=lambda g: g.area, reverse=True)
        if len(parts) > 1:
            notes.append(f"multipolygon: kept largest of {len(parts)} parts")
        geom = parts[0]
    if not isinstance(geom, Polygon) or geom.is_empty:
        return None, notes
    if geom.interiors:
        notes.append(f"dropped {len(geom.interiors)} interior ring(s) (courtyards)")
        geom = Polygon(geom.exterior)
    return geom, notes


def _clean_local(poly: Polygon) -> Optional[Polygon]:
    # Remove near-duplicate and collinear vertices (sub-10 cm detail), which
    # would otherwise become sliver wall surfaces in EnergyPlus.
    simplified = poly.simplify(0.1, preserve_topology=True)
    if simplified.is_empty or not isinstance(simplified, Polygon):
        return None
    return orient(simplified, sign=1.0)


def _centroid_lon_lat(polys: Iterable[Polygon]) -> Tuple[float, float]:
    xs, ys = [], []
    for p in polys:
        c = p.centroid
        xs.append(c.x)
        ys.append(c.y)
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _lookup(props: Dict[str, Any], explicit: Optional[str], candidates: Sequence[str]) -> Tuple[Optional[str], Any]:
    if explicit:
        if explicit in props:
            return explicit, props[explicit]
        lower = {k.lower(): k for k in props}
        if explicit.lower() in lower:
            k = lower[explicit.lower()]
            return k, props[k]
        return None, None
    lower = {k.lower(): k for k in props}
    for c in candidates:
        if c in lower and props[lower[c]] not in (None, ""):
            return lower[c], props[lower[c]]
    return None, None


def _parse_length(value, unit: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        x = float(value)
    else:
        s = str(value).strip().lower()
        m = re.match(r"^([0-9]*\.?[0-9]+)\s*(m|meters?|metres?|ft|feet|')?$", s)
        if not m:
            return None
        x = float(m.group(1))
        if m.group(2) in ("ft", "feet", "'"):
            return x * _FT_TO_M if x > 0 else None
    if not math.isfinite(x) or x <= 0:
        return None
    return x * _FT_TO_M if unit == "ft" else x


def _parse_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        n = int(round(float(str(value).strip())))
    except ValueError:
        return None
    return n if n > 0 else None


def _normalize_attributes(fid, poly, props, notes, *, height_field, floors_field, use_field,
                          year_field, height_unit, default_archetype, default_year):
    # Carry provenance through a save/reload round trip.
    inferred: List[str] = list(props["inferred_fields"]) if isinstance(props.get("inferred_fields"), list) else []
    if isinstance(props.get("notes"), list):
        notes = [n for n in props["notes"] if n not in notes] + notes

    _, raw_height = _lookup(props, height_field, HEIGHT_FIELDS)
    height = _parse_length(raw_height, height_unit)
    _, raw_floors = _lookup(props, floors_field, FLOORS_FIELDS)
    floors = _parse_int(raw_floors)

    # Provisional floor count for size/height-dependent use rules.
    provisional_floors = floors or (max(1, round(height / 3.5)) if height else None)

    use_candidates = []
    _, explicit_use = _lookup(props, use_field, USE_FIELDS)
    use_candidates.append(explicit_use)
    if use_field is None:
        lower = {k.lower(): v for k, v in props.items()}
        use_candidates.extend(lower.get(k) for k in OSM_USE_FIELDS)
    archetype, use_reason = classify_use(use_candidates, poly.area, provisional_floors)
    if archetype == "_unconditioned":
        return ExcludedFeature(fid, f"unconditioned: {use_reason}")
    if "approximated" in use_reason:
        notes = notes + [use_reason]
    if archetype is None:
        archetype = default_archetype
        _add(inferred, "archetype")
        notes = notes + [f"archetype defaulted to {default_archetype} ({use_reason})"]
    arch = ARCHETYPES[archetype]
    f2f = arch.floor_to_floor_m

    if floors is None and height is not None:
        floors = max(1, round(height / f2f))
        _add(inferred, "num_floors")
    elif floors is not None and height is None:
        height = floors * f2f
        _add(inferred, "height_m")
    elif floors is None and height is None:
        floors = arch.default_floors
        height = floors * f2f
        _add(inferred, "num_floors")
        _add(inferred, "height_m")
    else:
        f2f = height / floors
        if not 2.2 <= f2f <= 10.0:
            notes = notes + [f"height {height:.1f} m / {floors} floors is implausible; using {arch.floor_to_floor_m} m floors"]
            f2f = arch.floor_to_floor_m
            height = floors * f2f

    _, raw_year = _lookup(props, year_field, YEAR_FIELDS)
    year = parse_year(raw_year)
    if year is None:
        year = default_year
        _add(inferred, "year_built")

    source_props = props.get("source_properties") if isinstance(props.get("source_properties"), dict) else props
    return Building(
        building_id=_safe_id(fid),
        footprint=poly,
        archetype=archetype,
        year_built=year,
        vintage=vintage_for_year(year),
        num_floors=floors,
        floor_to_floor_m=f2f,
        height_m=height,
        inferred_fields=inferred,
        notes=notes,
        source_properties=source_props,
    )


def _add(lst: List[str], item: str) -> None:
    if item not in lst:
        lst.append(item)


def _safe_id(fid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", fid).strip("_") or "bldg"
