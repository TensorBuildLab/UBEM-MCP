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
from ..utils.path_utils import resolve_path
from . import osm
from .enrich import enrich_stock
from .stock import BuildingStock, load_geojson, stock_from_geojson

logger = logging.getLogger(__name__)

_STOCK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class StockManager:
    def __init__(self, config: Config):
        self.config = config
        self._stocks: Dict[str, BuildingStock] = {}

    @property
    def root(self) -> Path:
        return Path(self.config.paths.output_dir) / "stocks"

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
