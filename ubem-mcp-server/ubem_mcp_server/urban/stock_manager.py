"""
Registry of loaded building stocks, persisted under <output_dir>/stocks/<stock_id>/
so a stock survives server restarts and later steps (model generation,
simulation, aggregation) can refer to it by id.
"""

import json
import logging
import os
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..utils.path_utils import resolve_path, resolve_weather_file_path
from . import osm, weather
from .enrich import enrich_stock
from .generate import generate_models, summarize_manifest
from .stock import BuildingStock, load_geojson, stock_from_geojson

logger = logging.getLogger(__name__)

_STOCK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StockManager:
    def __init__(self, config: Config):
        self.config = config
        self._stocks: Dict[str, BuildingStock] = {}
        self._station_index: Optional[weather.StationIndex] = None
        self.fetch = weather.http_get  # replaced in tests

    @property
    def root(self) -> Path:
        return Path(self.config.paths.output_dir) / "stocks"

    @property
    def weather_dir(self) -> Path:
        return Path(self.config.paths.output_dir) / "weather"

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #

    def load_from_file(self, path: str, stock_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        stock_id = self._new_id(stock_id)
        resolved = self._resolve_input(path)
        stock = load_geojson(resolved, **kwargs)
        return self._register(stock, stock_id, extra_meta={"source_file": resolved, "loader_options": kwargs})

    def fetch_osm(
        self,
        bbox: Optional[Sequence[float]] = None,
        center_lat: Optional[float] = None,
        center_lon: Optional[float] = None,
        radius_m: Optional[float] = None,
        stock_id: Optional[str] = None,
        overpass_url: str = osm.DEFAULT_OVERPASS_URL,
        **kwargs,
    ) -> Dict[str, Any]:
        if bbox is None:
            if center_lat is None or center_lon is None or not radius_m:
                raise ValueError("Provide either bbox=[south, west, north, east] or center_lat, center_lon and radius_m")
            bbox = osm.bbox_from_point(center_lat, center_lon, radius_m)
        bbox = osm.validate_bbox(bbox)

        stock_id = self._new_id(stock_id)
        stock_dir = self.root / stock_id
        stock_dir.mkdir(parents=True, exist_ok=True)

        query = osm.build_query(bbox)
        logger.info("Fetching OSM buildings for bbox=%s", bbox)
        raw = osm.fetch_overpass(query, url=overpass_url)
        retrieved_at = datetime.now().isoformat()
        with open(stock_dir / "osm_raw.json", "w", encoding="utf-8") as f:
            json.dump(raw, f)
        geojson = osm.overpass_to_geojson(raw)
        with open(stock_dir / "source.geojson", "w", encoding="utf-8") as f:
            json.dump(geojson, f)

        stock = stock_from_geojson(geojson, source=f"OpenStreetMap bbox={list(bbox)}", **kwargs)
        meta = {
            "osm": {
                "bbox": list(bbox),
                "overpass_url": overpass_url,
                "query": query,
                "retrieved_at": retrieved_at,
                "osm_base_timestamp": (raw.get("osm3s") or {}).get("timestamp_osm_base"),
                "num_features": len(geojson["features"]),
            },
            "loader_options": kwargs,
        }
        return self._register(stock, stock_id, extra_meta=meta)

    def enrich(
        self,
        stock_id: str,
        layer_path: str,
        field_map: Dict[str, str],
        new_stock_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Join attributes from a GeoJSON layer onto a stock, saved as a new stock."""
        base = self.get(stock_id)
        new_id = self._new_id(new_stock_id)
        resolved = self._resolve_input(layer_path)
        with open(resolved, encoding="utf-8") as f:
            layer = json.load(f)
        loader_options = self.get_meta(stock_id).get("loader_options") or {}
        enriched, report = enrich_stock(base, layer, field_map, loader_options=loader_options, **kwargs)
        meta = {
            "derived_from": stock_id,
            "enrichment": {"layer": resolved, "field_map": field_map, "options": kwargs, "report": report},
            "loader_options": loader_options,
        }
        result = self._register(enriched, new_id, extra_meta=meta)
        return {"enrichment": report, **result}

    # ------------------------------------------------------------------ #
    # Weather and climate
    # ------------------------------------------------------------------ #

    def station_index(self, refresh: bool = False) -> weather.StationIndex:
        if self._station_index is None or refresh:
            self._station_index = weather.StationIndex.load(str(self.weather_dir), refresh=refresh, fetch=self.fetch)
        return self._station_index

    def site_location(self, stock_id: str) -> Dict[str, float]:
        frame = self.get(stock_id).frame
        return {"latitude": frame.lat0, "longitude": frame.lon0}

    def find_weather_stations(self, latitude: Optional[float] = None, longitude: Optional[float] = None,
                              stock_id: Optional[str] = None, n: int = 5) -> Dict[str, Any]:
        if stock_id:
            loc = self.site_location(stock_id)
            latitude, longitude = loc["latitude"], loc["longitude"]
        if latitude is None or longitude is None:
            raise ValueError("Provide stock_id, or latitude and longitude")
        index = self.station_index()
        return {
            "site": {"latitude": latitude, "longitude": longitude},
            "index_retrieved_at": index.retrieved_at,
            "stations": [_station_summary(st, d) for st, d in index.nearest(latitude, longitude, n)],
        }

    def resolve_weather(self, stock_id: str, station: Optional[str] = None, dataset: Optional[str] = None,
                        weather_file: Optional[str] = None) -> Dict[str, Any]:
        """Pick weather for a stock (the nearest station, a named `station`, or a
        local `weather_file`), download it if needed, determine the climate
        zone, and record both in the stock's meta.json for model generation."""
        site = self.site_location(stock_id)
        warnings: List[str] = []
        if weather_file:
            epw = resolve_weather_file_path(self.config, weather_file)
            stat = str(Path(epw).with_suffix(".stat"))
            files = {"epw": epw, "stat": stat if os.path.exists(stat) else None}
            lat, lon = _epw_header_location(epw)
            distance = weather.haversine_km(site["latitude"], site["longitude"], lat, lon)
            info: Dict[str, Any] = {"source": "local file", "epw": epw, "stat": files["stat"]}
        else:
            index = self.station_index()
            if station:
                matches = [s for s in index.stations if station in (s.file_stem, s.base_url)]
                if not matches:
                    raise KeyError(f"Unknown weather station {station!r}; use find_weather_stations")
                st = matches[0]
                distance = weather.haversine_km(site["latitude"], site["longitude"], st.latitude, st.longitude)
            else:
                st, distance = index.nearest(site["latitude"], site["longitude"], 1)[0]
            files = weather.download_weather(st, dataset, str(self.weather_dir / "files"), fetch=self.fetch)
            info = {"source": "climate.onebuilding.org", "station": _station_summary(st, distance),
                    "dataset": files["dataset"], "url": files["url"], "epw": files["epw"],
                    "stat": files.get("stat"), "ddy": files.get("ddy")}
        info["distance_km"] = round(distance, 1)
        if distance > 50:
            warnings.append(f"the weather station is {distance:.0f} km from the site")
        info.update(weather.site_climate(files))
        info["warnings"] = warnings
        info["resolved_at"] = datetime.now().isoformat(timespec="seconds")
        self._update_meta(stock_id, {"weather": info})
        return {"stock_id": stock_id, "site": site, **info}

    def generate_models(
        self,
        stock_id: str,
        climate_zone: Optional[str] = None,
        weather_file: Optional[str] = None,
        hvac: str = "template",
        archetypes: Optional[Sequence[str]] = None,
        vintages: Optional[Sequence[str]] = None,
        building_ids: Optional[Sequence[str]] = None,
        max_buildings: Optional[int] = None,
        model_set_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write one archetype IDF per selected building under
        <stock_directory>/models/<model_set_id>/."""
        stock = self.get(stock_id)
        selected = stock.filter(archetypes=archetypes, vintages=vintages, building_ids=building_ids)
        if building_ids:
            missing = set(building_ids) - {b.building_id for b in selected}
            if missing:
                raise KeyError(f"Unknown building_id(s): {sorted(missing)[:10]}")
        if max_buildings is not None:
            selected = selected[:max(0, max_buildings)]
        if not selected:
            raise ValueError("No buildings match the selection")
        epw, climate_zone, climate = self._weather_for_models(stock_id, climate_zone, weather_file)

        set_id = model_set_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._check_id(set_id)
        out_dir = self.stock_dir(stock_id) / "models" / set_id
        if (out_dir / "manifest.json").exists():
            raise ValueError(f"Model set {set_id!r} already exists for stock {stock_id!r}")
        manifest = generate_models(stock, selected, climate_zone, str(out_dir),
                                   self.config.energyplus.idd_path, weather_file=epw, hvac=hvac)
        manifest["stock_id"] = stock_id
        manifest["model_set_id"] = set_id
        manifest["climate"] = climate
        with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, default=str)
        return {"stock_id": stock_id, "model_set_id": set_id, "weather_file": epw,
                "climate": climate, **summarize_manifest(manifest, str(out_dir))}

    def _weather_for_models(self, stock_id: str, climate_zone: Optional[str],
                            weather_file: Optional[str]):
        """(EPW path or None, climate zone, provenance) for model generation.

        An explicit climate_zone always wins. Otherwise the zone comes from the
        given weather file, or from the stock's resolved weather (resolved
        now, from the nearest station, if it has not been yet)."""
        if weather_file:
            epw = resolve_weather_file_path(self.config, weather_file)
            if climate_zone:
                return epw, climate_zone, {"climate_zone": climate_zone, "source": "given", "weather_file": epw}
            info = self.resolve_weather(stock_id, weather_file=epw)
        elif climate_zone:
            return None, climate_zone, {"climate_zone": climate_zone, "source": "given", "weather_file": None}
        else:
            info = self.get_meta(stock_id).get("weather") or self.resolve_weather(stock_id)
        climate = {"climate_zone": info["library_climate_zone"], "ashrae_climate_zone": info["ashrae_climate_zone"],
                   "source": info["climate_zone_source"], "weather_file": info["epw"],
                   "notes": info.get("notes", []) + info.get("warnings", [])}
        return info["epw"], info["library_climate_zone"], climate

    def model_manifest(self, stock_id: str, model_set_id: str) -> Dict[str, Any]:
        self._check_id(model_set_id)
        path = self.stock_dir(stock_id) / "models" / model_set_id / "manifest.json"
        if not path.exists():
            raise KeyError(f"Unknown model set {model_set_id!r} for stock {stock_id!r}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #

    def get(self, stock_id: str) -> BuildingStock:
        if stock_id in self._stocks:
            return self._stocks[stock_id]
        self._check_id(stock_id)
        path = self.root / stock_id / "buildings.geojson"
        if not path.exists():
            raise KeyError(f"Unknown stock_id: {stock_id}")
        stock = load_geojson(str(path), min_footprint_m2=0.0)
        meta = self.get_meta(stock_id)
        stock.source = meta.get("source", stock.source)
        self._stocks[stock_id] = stock
        return stock

    def get_meta(self, stock_id: str) -> Dict[str, Any]:
        self._check_id(stock_id)
        path = self.root / stock_id / "meta.json"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def list_stocks(self) -> List[Dict[str, Any]]:
        out = []
        if self.root.exists():
            for d in sorted(self.root.iterdir()):
                if (d / "meta.json").exists():
                    meta = self.get_meta(d.name)
                    out.append({
                        "stock_id": d.name,
                        "created_at": meta.get("created_at"),
                        "source": meta.get("source"),
                        "num_buildings": (meta.get("summary") or {}).get("num_buildings"),
                    })
        return out

    def stock_dir(self, stock_id: str) -> Path:
        self._check_id(stock_id)
        return self.root / stock_id

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _register(self, stock: BuildingStock, stock_id: str, extra_meta: Dict[str, Any]) -> Dict[str, Any]:
        stock_dir = self.root / stock_id
        stock_dir.mkdir(parents=True, exist_ok=True)

        with open(stock_dir / "buildings.geojson", "w", encoding="utf-8") as f:
            json.dump(stock.to_geojson(), f)
        with open(stock_dir / "excluded.json", "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in stock.excluded], f, indent=1)
        summary = stock.summary()
        meta = {
            "stock_id": stock_id,
            "created_at": datetime.now().isoformat(),
            "source": stock.source,
            "summary": summary,
            **extra_meta,
        }
        with open(stock_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

        self._stocks[stock_id] = stock
        logger.info("Registered stock %s: %d buildings", stock_id, len(stock))
        return {"stock_id": stock_id, "stock_directory": str(stock_dir), **summary}

    def _update_meta(self, stock_id: str, updates: Dict[str, Any]) -> None:
        meta = self.get_meta(stock_id)
        meta.update(updates)
        with open(self.stock_dir(stock_id) / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    def _new_id(self, requested: Optional[str]) -> str:
        if requested:
            self._check_id(requested)
            if (self.root / requested / "meta.json").exists() or requested in self._stocks:
                raise ValueError(f"stock_id {requested!r} already exists")
            stock_id = requested
        else:
            stock_id = uuid.uuid4().hex[:12]
        return stock_id

    @staticmethod
    def _check_id(stock_id: str) -> None:
        if not _STOCK_ID_RE.match(stock_id or ""):
            raise ValueError(f"Invalid stock_id {stock_id!r}: use 1-64 letters, digits, '_' or '-'")

    def _resolve_input(self, path: str) -> str:
        if os.path.isabs(path) and os.path.exists(path):
            return path
        return resolve_path(self.config, path, file_types=[".geojson", ".json"], description="GeoJSON file")


def _station_summary(st: "weather.Station", distance_km: float) -> Dict[str, Any]:
    return {"station": st.file_stem, "name": st.name, "country": st.country, "state": st.state, "wmo": st.wmo,
            "latitude": st.latitude, "longitude": st.longitude, "elevation_m": st.elevation_m,
            "distance_km": round(distance_km, 1), "datasets": st.datasets,
            "default_dataset": st.default_dataset()}


def _epw_header_location(epw_path: str):
    with open(epw_path, encoding="latin-1") as f:
        header = f.readline().split(",")
    return float(header[6]), float(header[7])
