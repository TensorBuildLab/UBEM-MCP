"""
Attribute enrichment: fill guessed building attributes from another GIS layer
(e.g. assessor parcels with year built and land use) by spatial join.

Footprint data such as OpenStreetMap usually has good geometry but sparse
attributes, while parcel/assessor data has attributes but no building
geometry. Joining the two is how most UBEM workflows get construction year
and use type.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from pyproj import CRS, Transformer
from shapely import STRtree
from shapely.geometry import Polygon, shape

from .archetypes import classify_use
from .provenance import is_personal_field
from .stock import BuildingStock, _legacy_crs_name, _reproject, stock_from_geojson

ENRICHABLE = ("year_built", "use", "num_floors", "height_m")


def enrich_stock(
    stock: BuildingStock,
    layer: Dict[str, Any],
    field_map: Dict[str, str],
    use_value_map: Optional[Dict[str, str]] = None,
    layer_crs: Optional[str] = None,
    overwrite: bool = False,
    height_unit: str = "m",
    min_overlap: float = 0.5,
    loader_options: Optional[Dict[str, Any]] = None,
    keep_columns: Optional[List[str]] = None,
) -> Tuple[BuildingStock, Dict[str, Any]]:
    """Return a new stock with attributes joined from `layer`, plus a report.

    field_map maps target attributes ("year_built", "use", "num_floors",
    "height_m") to column names in the layer. use_value_map translates the
    layer's use values (e.g. assessor land-use codes) to archetype keys or
    recognized use words. Each building matches the layer feature containing
    its footprint centroid; if none does, the polygon covering the largest
    share of the footprint is used when that share is at least `min_overlap`.
    Point features match the footprint that contains them.

    A layer's use describes the whole parcel, so it is not given to structures
    whose own data mark them unconditioned (garages, sheds, carports) unless
    overwrite=True. keep_columns copies those layer columns (e.g. a parcel id
    or the raw land-use code) onto each matched building, where query_stock
    and export_stock can use them.

    By default only attributes that were inferred are filled; with
    overwrite=True, layer values replace source values too.
    """
    unknown = set(field_map) - set(ENRICHABLE)
    if unknown:
        raise ValueError(f"Unknown target attribute(s) {sorted(unknown)}; valid: {list(ENRICHABLE)}")
    if not field_map:
        raise ValueError("field_map is empty; map at least one of " + ", ".join(ENRICHABLE))
    if height_unit not in ("m", "ft"):
        raise ValueError("height_unit must be 'm' or 'ft'")
    use_value_map = {str(k).strip().lower(): v for k, v in (use_value_map or {}).items()}
    keep_columns = list(keep_columns or [])
    personal = [c for c in keep_columns if is_personal_field(c)]
    if personal and not (loader_options or {}).get("keep_personal_fields"):
        raise ValueError(f"keep_columns {personal} look like personal data (owner, address, contact) and are "
                         "not copied; load the stock with keep_personal_fields=true only if you need them "
                         "and may hold them")

    geoms, props = _layer_in_local_frame(stock, layer, layer_crs)
    tree = STRtree(geoms) if geoms else None

    data = stock.to_geojson()
    report = Counter()
    filled = Counter()
    unrecognized = Counter()
    for feat, building in zip(data["features"], stock.buildings, strict=True):
        match = _match(building.footprint, geoms, tree, min_overlap) if tree is not None else None
        if match is None:
            report["unmatched"] += 1
            continue
        report["matched"] += 1
        layer_props = props[match]
        changed = _apply(feat["properties"], building, layer_props, field_map, use_value_map,
                         overwrite, height_unit, unrecognized, report)
        filled.update(changed)
        if keep_columns:
            _keep(feat["properties"], layer_props, keep_columns, report)

    # Only the defaults, the mode and the privacy choice carry over: column
    # names and units described the original source, not the already-normalized
    # properties being reloaded.
    keep = ("default_archetype", "default_year", "mode", "keep_personal_fields")
    opts = {k: v for k, v in (loader_options or {}).items() if k in keep}
    new_stock = stock_from_geojson(data, source=f"{stock.source} + enrichment", min_footprint_m2=0.0, **opts)
    summary = {
        "buildings": len(stock.buildings),
        "layer_features": len(geoms),
        "matched": report["matched"],
        "unmatched": report["unmatched"],
        "filled": {("use" if k == "archetype" else k): v for k, v in filled.items()},
        "use_not_given_to_unconditioned": report["kept_unconditioned"],
        **({"kept_columns": keep_columns, "kept_columns_missing": report["missing_column"]}
           if keep_columns else {}),
        "unrecognized_use_values": dict(unrecognized.most_common(20)),
        "inferred_share_before": stock.summary()["inferred_share"],
        "inferred_share_after": new_stock.summary()["inferred_share"],
    }
    return new_stock, summary


def _layer_in_local_frame(stock: BuildingStock, layer: Dict[str, Any], layer_crs: Optional[str]):
    if layer.get("type") != "FeatureCollection":
        raise ValueError("Enrichment layer must be a GeoJSON FeatureCollection")
    crs_name = layer_crs or _legacy_crs_name(layer) or "EPSG:4326"
    to_wgs84 = None
    if not CRS.from_user_input(crs_name).equals(CRS.from_epsg(4326)):
        to_wgs84 = Transformer.from_crs(crs_name, "EPSG:4326", always_xy=True).transform
    geoms, props = [], []
    for feat in layer.get("features") or []:
        if not feat.get("geometry"):
            continue
        try:
            g = shape(feat["geometry"])
        except Exception:  # noqa: BLE001 - skip malformed features
            continue
        if to_wgs84 is not None:
            g = _reproject(to_wgs84, g)
        g = stock.frame.to_local(g)
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        geoms.append(g)
        props.append(feat.get("properties") or {})
    return geoms, props


def _match(footprint: Polygon, geoms: List[Any], tree: STRtree, min_overlap: float) -> Optional[int]:
    candidates = tree.query(footprint)
    if len(candidates) == 0:
        return None
    centroid = footprint.representative_point()
    best, best_share = None, 0.0
    for i in candidates:
        g = geoms[int(i)]
        if g.geom_type in ("Point", "MultiPoint"):
            if footprint.contains(g):
                return int(i)
            continue
        if g.contains(centroid):
            return int(i)
        share = footprint.intersection(g).area / footprint.area if footprint.area else 0.0
        if share > best_share:
            best, best_share = int(i), share
    return best if best_share >= min_overlap else None


def _keep(props: Dict[str, Any], layer_props: Dict[str, Any], columns: List[str], report: Counter) -> None:
    """Copy layer columns into the building's source columns; a name the
    building already has gets the prefix "layer_"."""
    lower = {k.lower(): k for k in layer_props}
    src = props.setdefault("source_properties", {})
    for col in columns:
        key = col if col in layer_props else lower.get(col.lower())
        if key is None:
            report["missing_column"] += 1
            continue
        name = col if col not in src or src.get(col) == layer_props[key] else f"layer_{col}"
        src[name] = layer_props[key]


def _apply(props: Dict[str, Any], building, layer_props: Dict[str, Any], field_map: Dict[str, str],
           use_value_map: Dict[str, str], overwrite: bool, height_unit: str,
           unrecognized: Counter, report: Counter) -> List[str]:
    """Write joined values into a saved-stock feature's properties so that
    stock_from_geojson re-derives everything that depends on them."""
    inferred = list(props.get("inferred_fields") or [])
    lower = {k.lower(): k for k in layer_props}
    changed: List[str] = []

    def layer_value(target):
        col = field_map.get(target)
        if not col:
            return None
        key = col if col in layer_props else lower.get(col.lower())
        value = layer_props.get(key) if key else None
        return None if value in (None, "") else value

    targets = {"use": "archetype", "year_built": "year_built", "num_floors": "num_floors", "height_m": "height_m"}
    for target, attr in targets.items():
        value = layer_value(target)
        # Fill values that were guessed (model mode) or are unknown (data mode).
        if value is None or not (overwrite or attr in inferred or props.get(attr) in (None, "")):
            continue
        if target == "use":
            if not overwrite and building.use is not None and                     classify_use([building.use], building.footprint_area_m2, building.num_floors)[0] == "_unconditioned":
                report["kept_unconditioned"] += 1
                continue
            mapped = use_value_map.get(str(value).strip().lower(), value)
            key, _ = classify_use([mapped], building.footprint_area_m2, building.num_floors)
            if key is None:
                unrecognized[str(value)] += 1
                continue
            props["archetype"] = mapped
        elif target == "height_m" and height_unit == "ft":
            props["height_m"] = f"{value} ft"
        else:
            props[target] = value
        if attr in inferred:
            inferred.remove(attr)
        changed.append(attr)
        enrichment = props.setdefault("source_properties", {}).setdefault("enrichment", {})
        enrichment[attr] = value

    if not changed:
        return changed

    # Height/floors that were guessed must be recomputed from the new data
    # (a new archetype changes floor-to-floor height; a known height changes
    # the floor count). Drop them so the loader derives them again.
    for attr in ("num_floors", "height_m"):
        if attr in inferred and attr not in changed:
            props.pop(attr, None)
            inferred.remove(attr)
    props["inferred_fields"] = inferred
    props.pop("vintage", None)
    props["notes"] = [n for n in props.get("notes") or [] if not n.startswith("archetype defaulted")] \
        if "archetype" in changed else props.get("notes")
    return changed
