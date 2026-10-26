"""
Fetch building footprints from OpenStreetMap via the Overpass API and convert
them to GeoJSON that `stock.stock_from_geojson` can load.

The raw Overpass response is cached next to the GeoJSON so a stock can be
rebuilt exactly (OSM data changes over time; the cache is the record of what
a given study used).
"""

import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "UBEM-MCP (https://github.com/TensorBuildLab/UBEM-MCP)"
MAX_BBOX_AREA_KM2 = 25.0


def bbox_from_point(lat: float, lon: float, radius_m: float) -> Tuple[float, float, float, float]:
    """(south, west, north, east) of a square around a point."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def bbox_area_km2(bbox: Sequence[float]) -> float:
    south, west, north, east = bbox
    mid = math.radians((south + north) / 2)
    return (north - south) * 111.32 * (east - west) * 111.32 * math.cos(mid)


def validate_bbox(bbox: Sequence[float], max_area_km2: float = MAX_BBOX_AREA_KM2) -> Tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must be [south, west, north, east] in decimal degrees")
    south, west, north, east = (float(v) for v in bbox)
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise ValueError(f"Invalid bbox {bbox}: expected south < north and west < east (decimal degrees)")
    area = bbox_area_km2((south, west, north, east))
    if area > max_area_km2:
        raise ValueError(
            f"bbox covers {area:.1f} km2, above the {max_area_km2} km2 limit; split the area into smaller tiles"
        )
    return south, west, north, east


def build_query(bbox: Sequence[float], timeout_s: int = 180) -> str:
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    return (
        f"[out:json][timeout:{timeout_s}];"
        f"(way[\"building\"]({b});relation[\"building\"][\"type\"=\"multipolygon\"]({b}););"
        "out geom;"
    )


RETRY_HTTP_CODES = (429, 502, 503, 504)
RETRY_WAITS_S = (10, 30)


def fetch_overpass(query: str, url: str = DEFAULT_OVERPASS_URL, timeout_s: int = 200,
                   retry_waits_s: Sequence[float] = RETRY_WAITS_S) -> Dict[str, Any]:
    """POST an Overpass query. A busy server (HTTP 429/5xx) or a timeout is
    retried after each wait in retry_waits_s; then ConnectionError says so."""
    body = urllib.parse.urlencode({"data": query}).encode()
    problem = ""
    for attempt in range(len(retry_waits_s) + 1):
        if attempt:
            time.sleep(retry_waits_s[attempt - 1])
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - configured endpoint
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_HTTP_CODES:
                raise
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            problem = str(getattr(e, "reason", e)) or type(e).__name__
    raise ConnectionError(
        f"The Overpass server {url} did not answer ({problem}) after {len(retry_waits_s) + 1} tries. "
        "It is often busy; try again in a few minutes, use a smaller area, or pass another Overpass "
        "endpoint as overpass_url (e.g. https://overpass.kumi.systems/api/interpreter).")


def overpass_to_geojson(osm: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an Overpass `out geom` JSON response to a GeoJSON FeatureCollection."""
    features: List[Dict[str, Any]] = []
    for el in osm.get("elements", []):
        tags = el.get("tags") or {}
        if "building" not in tags:
            continue
        geom = None
        if el.get("type") == "way":
            geom = _way_polygon(el.get("geometry") or [])
        elif el.get("type") == "relation":
            geom = _relation_polygon(el.get("members") or [])
        if geom is None or geom.is_empty:
            continue
        osm_id = f"{el['type']}/{el['id']}"
        features.append({
            "type": "Feature",
            "id": osm_id,
            "geometry": mapping(geom),
            "properties": {"osm_id": osm_id, **tags},
        })
    return {"type": "FeatureCollection", "features": features}


def _coords(nodes: List[Dict[str, float]]) -> List[Tuple[float, float]]:
    return [(n["lon"], n["lat"]) for n in nodes if n and "lon" in n and "lat" in n]


def _way_polygon(nodes) -> Optional[Polygon]:
    pts = _coords(nodes)
    if len(pts) < 4 or pts[0] != pts[-1]:
        return None
    return Polygon(pts)


def _relation_polygon(members) -> Optional[Polygon]:
    outers, inners = [], []
    for m in members:
        if m.get("type") != "way":
            continue
        pts = _coords(m.get("geometry") or [])
        if len(pts) < 2:
            continue
        (inners if m.get("role") == "inner" else outers).append(LineString(pts))
    if not outers:
        return None
    outer_polys = list(polygonize(linemerge(outers)))
    if not outer_polys:
        return None
    geom = unary_union(outer_polys)
    if inners:
        holes = list(polygonize(linemerge(inners)))
        if holes:
            geom = geom.difference(unary_union(holes))
    if isinstance(geom, MultiPolygon):
        return geom  # stock loader keeps the largest part and records it
    return geom if isinstance(geom, Polygon) else None
