"""
Registry of loaded building stocks, persisted under <output_dir>/stocks/<stock_id>/
so a stock survives server restarts and later steps (model generation,
simulation, aggregation) can refer to it by id.
"""

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..utils.path_utils import resolve_path, resolve_weather_file_path
from . import osm, own_models, provenance, query, record, retrofit, weather
from .aggregate import aggregate, compare_results
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

    def load_from_file(self, path: str, stock_id: Optional[str] = None, license: Optional[str] = None,
                       attribution: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        stock_id = self._new_id(stock_id)
        resolved = self._resolve_input(path)
        stock = load_geojson(resolved, **kwargs)
        src = provenance.source_record(f"file:{Path(resolved).name}", Path(resolved).name, "footprints",
                                       license=license, attribution=attribution)
        rec = record.recipe("load", {"path": resolved, "license": license, "attribution": attribution,
                                     "loader_options": kwargs}, [record.input_entry("footprints", resolved)])
        return self._register(stock, stock_id, extra_meta={"source_file": resolved, "loader_options": kwargs,
                                                           "recipe": rec}, sources=[src])

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
        query = osm.build_query(bbox)
        logger.info("Fetching OSM buildings for bbox=%s", bbox)
        raw = osm.fetch_overpass(query, url=overpass_url)
        return self._from_osm_raw(raw, stock_id, list(bbox), overpass_url, query, datetime.now().isoformat(), kwargs)

    def _from_osm_raw(self, raw: Dict[str, Any], stock_id: str, bbox: List[float], overpass_url: str, query: str,
                      retrieved_at: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Register a stock from an Overpass response; the response is kept as
        osm_raw.json so the stock can be rebuilt from exactly this snapshot."""
        stock_dir = self.root / stock_id
        stock_dir.mkdir(parents=True, exist_ok=True)
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
            "recipe": record.recipe("fetch_osm", {"bbox": list(bbox), "overpass_url": overpass_url,
                                                  "loader_options": kwargs},
                                    [record.input_entry("OpenStreetMap snapshot", str(stock_dir / "osm_raw.json"))]),
        }
        src = provenance.osm_source(retrieved_at, meta["osm"]["osm_base_timestamp"])
        return self._register(stock, stock_id, extra_meta=meta, sources=[src])

    def enrich(
        self,
        stock_id: str,
        layer_path: str,
        field_map: Dict[str, str],
        new_stock_id: Optional[str] = None,
        layer_license: Optional[str] = None,
        layer_attribution: Optional[str] = None,
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
            "recipe": record.recipe("enrich", {"layer_path": resolved, "field_map": field_map, "options": kwargs,
                                               "layer_license": layer_license,
                                               "layer_attribution": layer_attribution},
                                    [record.input_entry("attribute layer", resolved)], parent=stock_id),
        }
        layer_src = provenance.source_record(f"file:{Path(resolved).name}", Path(resolved).name, "attributes",
                                             license=layer_license, attribution=layer_attribution)
        sources = provenance.merge_sources(self.get_meta(stock_id).get("sources") or [], [layer_src])
        result = self._register(enriched, new_id, extra_meta=meta, sources=sources)
        return {"enrichment": report, **result}

    def assign_archetypes(self, stock_id: str, new_stock_id: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Derive a model-mode stock from a data-mode one: classify every
        building into an archetype, fill unknown values with defaults (recorded
        as inferred) and exclude unconditioned or tiny structures. `kwargs` are
        the model-mode loader options (default_archetype, default_year, ...)."""
        base = self.get(stock_id)
        if base.mode != "data":
            raise ValueError(f"Stock {stock_id!r} is already a modeling stock")
        saved = json.loads((self.stock_dir(stock_id) / "buildings.geojson").read_text(encoding="utf-8"))
        feats = []
        for ft in saved["features"]:
            p = ft["properties"]
            # Re-read the original columns plus anything joined later (enrich).
            flat = dict(p.get("source_properties") or {})
            flat.update({k: p[k] for k in ("archetype", "year_built", "num_floors", "height_m")
                         if p.get(k) not in (None, "")})
            if p.get("use"):
                flat["use"] = p["use"]
            flat["building_id"] = p["building_id"]
            feats.append({"type": "Feature", "id": p["building_id"], "geometry": ft["geometry"],
                          "properties": flat})
        data = {"type": "FeatureCollection", "ubem_mcp": saved.get("ubem_mcp"), "features": feats}
        base_opts = self.get_meta(stock_id).get("loader_options") or {}
        opts = {k: v for k, v in base_opts.items()
                if k in ("keep_personal_fields", "height_unit")}
        opts.update({k: v for k, v in kwargs.items() if v is not None})
        opts["mode"] = "model"
        stock = stock_from_geojson(data, source=base.source, **opts)
        stock.personal_fields_removed = list(base.personal_fields_removed)
        new_id = self._new_id(new_stock_id)
        meta = {"derived_from": stock_id, "derivation": "assign_archetypes", "loader_options": opts,
                "recipe": record.recipe("assign_archetypes",
                                        {k: v for k, v in kwargs.items() if v is not None}, parent=stock_id)}
        return self._register(stock, new_id, extra_meta=meta,
                              sources=self.get_meta(stock_id).get("sources") or [])

    def query_stock(self, stock_id: str, **kwargs) -> Dict[str, Any]:
        """Filter, group and summarize a stock's buildings (see urban/query.py)."""
        return {"stock_id": stock_id, **query.query(self.get(stock_id), **kwargs)}

    def export_stock(self, stock_id: str, fmt: str = "csv", filters: Optional[Dict[str, Any]] = None,
                     columns: Optional[Sequence[str]] = None, name: Optional[str] = None) -> Dict[str, Any]:
        """Write a stock (or a filtered part of it) to <stock_directory>/exports/,
        with a metadata file recording its sources, licences and attribution."""
        if fmt not in ("csv", "geojson"):
            raise ValueError("format must be 'csv' or 'geojson'")
        stem = name or f"{stock_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._check_id(stem)
        out_dir = self.stock_dir(stock_id) / "exports"
        info = query.export(self.get(stock_id), out_dir / f"{stem}.{fmt}", fmt, filters=filters, columns=columns)
        sources = self.get_meta(stock_id).get("sources") or []
        meta = {"stock_id": stock_id, "exported_at": datetime.now().isoformat(timespec="seconds"),
                "filters": filters or {}, **info, "sources": sources,
                "attribution": provenance.attribution_text(sources),
                "warnings": provenance.licence_warnings(sources)}
        meta_path = out_dir / f"{stem}.metadata.json"
        meta_path.write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
        return {**info, "metadata_path": str(meta_path), "attribution": meta["attribution"],
                "warnings": meta["warnings"]}

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
        info["epw_sha256"] = record.sha256_file(info["epw"])
        if info["source"] == "climate.onebuilding.org":
            wsrc = provenance.weather_source(info.get("url"), info.get("dataset"))
        else:
            wsrc = provenance.source_record(f"file:{Path(info['epw']).name}", Path(info["epw"]).name, "weather")
        sources = [s for s in self.get_meta(stock_id).get("sources") or [] if s.get("role") != "weather"]
        self._update_meta(stock_id, {"weather": info, "sources": provenance.merge_sources(sources, [wsrc])})
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
        param_hook=None,
        extra_manifest: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Write one archetype IDF per selected building under
        <stock_directory>/models/<model_set_id>/."""
        stock = self.get(stock_id)
        if stock.mode != "model":
            raise ValueError(f"Stock {stock_id!r} holds data only (mode 'data'); derive a modeling stock "
                             f"with assign_archetypes first")
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
                                   self.config.energyplus.idd_path, weather_file=epw, hvac=hvac,
                                   param_hook=param_hook)
        manifest["stock_id"] = stock_id
        manifest["model_set_id"] = set_id
        manifest["climate"] = climate
        hashes = {}
        for m in manifest["models"]:
            m["idf_sha256"] = hashes[m["building_id"]] = record.sha256_file(m["idf_path"])
        manifest["models_sha256"] = record.combined_hash(hashes)
        manifest["software"] = self.software_versions()
        manifest["recipe"] = record.recipe(
            "generate_models",
            {"climate_zone": manifest["climate_zone"], "weather_file": epw, "hvac": hvac,
             "building_ids": [b.building_id for b in selected]},
            [record.input_entry("weather", epw)] if epw else [])
        sources = provenance.merge_sources(self.get_meta(stock_id).get("sources") or [],
                                           provenance.ARCHETYPE_SOURCES)
        self._update_meta(stock_id, {"sources": sources})
        manifest["sources"] = sources
        manifest["attribution"] = provenance.attribution_text(sources)
        if extra_manifest:
            manifest.update(extra_manifest(manifest) if callable(extra_manifest) else extra_manifest)
        with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, default=str)
        result = {"stock_id": stock_id, "model_set_id": set_id, "weather_file": epw,
                  "climate": climate, **summarize_manifest(manifest, str(out_dir))}
        if "scenario" in manifest:
            result["scenario"] = {"name": manifest["scenario"]["name"],
                                  "baseline_model_set_id": manifest["baseline_model_set_id"],
                                  **manifest["adoption"]}
        return result

    def generate_scenario(self, stock_id: str, baseline_model_set_id: str, scenario: Dict[str, Any],
                          model_set_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate a retrofit scenario's models: the baseline model set's
        buildings, climate, weather and HVAC option, with the scenario's
        measures applied to the buildings that adopt it."""
        spec = retrofit.validate_scenario(scenario)
        base = self.model_manifest(stock_id, baseline_model_set_id)
        if base.get("scenario"):
            raise ValueError(f"Model set {baseline_model_set_id!r} is itself a scenario; use its baseline")
        supplied = [m for m in base["models"] if m.get("model_source") == "supplied"]
        ids = [m["building_id"] for m in base["models"] if m.get("model_source") != "supplied"]
        if not ids:
            raise ValueError("Every model in this set was supplied by the user; scenario measures can only be "
                             "applied to archetype models")
        stock = self.get(stock_id)
        buildings = [b for b in stock.buildings if b.building_id in set(ids)]
        adopters, targeted = retrofit.adopting_buildings(spec, buildings)
        climate = base.get("climate") or {}
        set_id = model_set_id or f"{baseline_model_set_id}__{spec['name']}"

        def extra(manifest):
            rec = record.recipe("generate_scenario", {"baseline_model_set_id": baseline_model_set_id,
                                                      "scenario": spec}, manifest["recipe"]["inputs"])
            out = {"scenario": spec, "baseline_model_set_id": baseline_model_set_id,
                   "adoption": retrofit.summarize_adoption(manifest["models"], targeted), "recipe": rec}
            if supplied:
                out_dir = Path(manifest["models"][0]["idf_path"]).parent
                kept = []
                for m in supplied:
                    dst = out_dir / Path(m["idf_path"]).name
                    shutil.copyfile(m["idf_path"], dst)
                    kept.append({**m, "idf_path": str(dst), "retrofit": [],
                                 "notes": m.get("notes", []) + ["supplied model: scenario measures not applied"]})
                out["models"] = manifest["models"] + kept
                out["num_models"] = len(out["models"])
                out["adoption"]["supplied_models_unchanged"] = [m["building_id"] for m in supplied]
                hashes = {m["building_id"]: m.get("idf_sha256") for m in out["models"]}
                out["models_sha256"] = record.combined_hash(hashes)
            return out

        return self.generate_models(
            stock_id, climate_zone=base["climate_zone"], weather_file=base.get("weather_file"),
            hvac=base.get("hvac", "template"), building_ids=ids, model_set_id=set_id,
            param_hook=retrofit.scenario_hook(spec, adopters),
            extra_manifest=lambda m: {**extra(m), "climate": {**climate, "climate_zone": base["climate_zone"]}},
        )

    def set_building_models(self, stock_id: str, model_set_id: str, building_models: Dict[str, str],
                            new_model_set_id: Optional[str] = None, license: Optional[str] = None,
                            attribution: Optional[str] = None) -> Dict[str, Any]:
        """A new model set in which some buildings use the user's own
        EnergyPlus models instead of archetype models. `building_models` maps
        building_id -> IDF path. The other buildings' models are copied, so
        the original model set stays as it was."""
        if not building_models:
            raise ValueError("building_models is empty; give {building_id: path to an .idf file}")
        base = self.model_manifest(stock_id, model_set_id)
        if base.get("scenario"):
            raise ValueError(f"Model set {model_set_id!r} is a retrofit scenario; supply models to its baseline "
                             f"{base['baseline_model_set_id']!r} and generate the scenario again")
        stock = self.get(stock_id)
        unknown = [b for b in building_models if b not in {x.building_id for x in stock.buildings}]
        if unknown:
            raise KeyError(f"Unknown building_id(s) in stock {stock_id!r}: {unknown[:10]}")
        new_id = new_model_set_id or f"{model_set_id}__own"
        self._check_id(new_id)
        out_dir = self.stock_dir(stock_id) / "models" / new_id
        if (out_dir / "manifest.json").exists():
            raise ValueError(f"Model set {new_id!r} already exists for stock {stock_id!r}")
        resolved = {bid: self._resolve_model(path) for bid, path in building_models.items()}

        ep_version = record._idd_header(self.config.energyplus.idd_path)["version"]
        warnings: List[str] = []
        inputs = []
        supplied: Dict[str, Dict[str, Any]] = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        for bid, src in resolved.items():
            dst = out_dir / f"{bid}.idf"
            info = own_models.prepare_supplied_model(src, str(dst), ep_version)
            warnings += info["warnings"]
            entry = own_models.supplied_entry(stock.get(bid), str(dst), src, record.sha256_file(src), info, license)
            entry["idf_sha256"] = record.sha256_file(str(dst))
            supplied[bid] = entry
            inputs.append(record.input_entry(f"building model {bid}", src))

        models = []
        for m in base["models"]:
            if m["building_id"] in supplied:
                models.append(supplied.pop(m["building_id"]))
                continue
            dst = out_dir / Path(m["idf_path"]).name
            shutil.copyfile(m["idf_path"], dst)
            models.append({**m, "idf_path": str(dst)})
        added = list(supplied.values())  # buildings that were not in the base set
        models += added
        failures = [f for f in base.get("failures", []) if f["building_id"] not in resolved]

        src_records = [provenance.source_record(f"file:{Path(p).name}", Path(p).name, "building model",
                                                license=license, attribution=attribution)
                       for p in resolved.values()]
        sources = provenance.merge_sources(base.get("sources") or self.get_meta(stock_id).get("sources") or [],
                                           src_records)
        hashes = {m["building_id"]: m.get("idf_sha256") for m in models}
        manifest = {k: v for k, v in base.items() if k not in ("models", "failures", "recipe")}
        manifest.update({
            "created_at": datetime.now().isoformat(), "model_set_id": new_id, "based_on": model_set_id,
            "models": models, "failures": failures, "num_models": len(models), "num_failed": len(failures),
            "num_supplied": sum(1 for m in models if m.get("model_source") == "supplied"),
            "models_sha256": record.combined_hash(hashes), "software": self.software_versions(),
            "sources": sources, "attribution": provenance.attribution_text(sources),
            "recipe": record.recipe("set_building_models",
                                    {"base_model_set_id": model_set_id, "building_models": resolved,
                                     "license": license, "attribution": attribution}, inputs),
        })
        with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=1, default=str)
        warnings += provenance.licence_warnings(sources)
        return {"stock_id": stock_id, "model_set_id": new_id, "based_on": model_set_id,
                "model_directory": str(out_dir), "weather_file": manifest.get("weather_file"),
                "num_models": len(models), "num_supplied": manifest["num_supplied"],
                "added_buildings": [m["building_id"] for m in added],
                "supplied": [{"building_id": m["building_id"], "outputs_added": m["outputs_added"],
                              "energyplus_version": m["energyplus_version"]}
                             for m in models if m.get("model_source") == "supplied"],
                "attribution": manifest["attribution"], "warnings": warnings}

    def _resolve_model(self, path: str) -> str:
        if os.path.isabs(path) and os.path.exists(path):
            return path
        return resolve_path(self.config, path, file_types=[".idf"], description="EnergyPlus model")

    def compare_scenario(self, stock_id: str, scenario_model_set_id: str,
                         baseline_results_id: Optional[str] = None,
                         scenario_results_id: Optional[str] = None,
                         emission_factors: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Compare a scenario's aggregated results with its baseline's."""
        manifest = self.model_manifest(stock_id, scenario_model_set_id)
        if not manifest.get("scenario"):
            raise ValueError(f"Model set {scenario_model_set_id!r} is not a retrofit scenario")
        base_id = manifest["baseline_model_set_id"]
        base_dir = self._results_dir(stock_id, base_id, baseline_results_id)
        scen_dir = self._results_dir(stock_id, scenario_model_set_id, scenario_results_id)
        out_dir = scen_dir / "comparison"
        return compare_results(str(base_dir), str(scen_dir), str(out_dir), manifest,
                               emission_factors=emission_factors)

    def _results_dir(self, stock_id: str, model_set_id: str, results_id: Optional[str]) -> Path:
        root = self.stock_dir(stock_id) / "models" / model_set_id / "results"
        if results_id:
            self._check_id(results_id)
            d = root / results_id
        else:
            cands = [p.parent for p in root.glob("*/summary.json")] if root.is_dir() else []
            if not cands:
                raise KeyError(f"No aggregated results for model set {model_set_id!r}; run aggregate_stock_results")
            d = max(cands, key=lambda p: (p / "summary.json").stat().st_mtime)
        if not (d / "summary.json").exists():
            raise KeyError(f"No aggregated results {results_id!r} for model set {model_set_id!r}")
        return d

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

    def aggregate_results(
        self,
        stock_id: str,
        model_set_id: str,
        outputs: Optional[Dict[str, Dict[str, Any]]] = None,
        results_id: Optional[str] = None,
        emission_factors: Optional[Dict[str, float]] = None,
        make_map: bool = True,
    ) -> Dict[str, Any]:
        """Aggregate a model set's simulation results into
        <model set>/results/<results_id>/.

        `outputs` maps building_id -> {"status", "output_directory", "error"}
        (from a batch); without it, the model set's runs/ directory is scanned.
        """
        manifest = self.model_manifest(stock_id, model_set_id)
        model_dir = self.stock_dir(stock_id) / "models" / model_set_id
        if outputs is None:
            outputs = scan_runs(model_dir / "runs", [m["building_id"] for m in manifest["models"]])
        results_id = results_id or "latest"
        self._check_id(results_id)
        if emission_factors:
            bad = {k: v for k, v in emission_factors.items() if not isinstance(v, (int, float)) or v < 0}
            if bad:
                raise ValueError(f"Emission factors must be non-negative numbers (kg CO2e/kWh): {bad}")
        out_dir = model_dir / "results" / results_id
        summary = aggregate(self.get(stock_id), manifest, outputs, str(out_dir),
                            emission_factors=emission_factors, make_map=make_map)
        sources = manifest.get("sources") or self.get_meta(stock_id).get("sources") or []
        summary["attribution"] = provenance.attribution_text(sources)
        lines = ["Data sources and required attribution for these results:", ""]
        lines += [f"- {s['name']} ({s['license']}): {s.get('attribution') or 'no attribution recorded'}"
                  for s in sources]
        (out_dir / "ATTRIBUTION.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return summary

    def model_manifest(self, stock_id: str, model_set_id: str) -> Dict[str, Any]:
        self._check_id(model_set_id)
        path = self.stock_dir(stock_id) / "models" / model_set_id / "manifest.json"
        if not path.exists():
            raise KeyError(f"Unknown model set {model_set_id!r} for stock {stock_id!r}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ #
    # Project records (reproducibility)
    # ------------------------------------------------------------------ #

    def software_versions(self) -> Dict[str, Any]:
        return record.software_versions(self.config.energyplus.idd_path, self.config.energyplus.executable_path)

    def lineage(self, stock_id: str) -> List[str]:
        """Stock ids from the original stock to `stock_id`."""
        chain = [stock_id]
        while True:
            parent = self.get_meta(chain[-1]).get("derived_from")
            if not parent or parent in chain:
                break
            chain.append(parent)
        return chain[::-1]

    def model_set_ids(self, stock_id: str) -> List[str]:
        root = self.stock_dir(stock_id) / "models"
        return sorted(p.parent.name for p in root.glob("*/manifest.json")) if root.is_dir() else []

    def project_record(self, stock_id: str) -> Dict[str, Any]:
        """Write <stock_directory>/project_record.json: the steps, inputs (with
        hashes), weather, model sets, results and software behind a stock."""
        self.get(stock_id)
        steps = []
        for sid in self.lineage(stock_id):
            meta = self.get_meta(sid)
            weather_info = meta.get("weather")
            steps.append({
                "stock_id": sid, "created_at": meta.get("created_at"),
                "recipe": meta.get("recipe"),
                "num_buildings": (meta.get("summary") or {}).get("num_buildings"),
                "mode": (meta.get("summary") or {}).get("mode"),
                "weather": None if not weather_info else {
                    "epw": weather_info.get("epw"), "epw_sha256": weather_info.get("epw_sha256"),
                    "source": weather_info.get("source"), "dataset": weather_info.get("dataset"),
                    "url": weather_info.get("url")},
            })
        model_sets = []
        for set_id in self.model_set_ids(stock_id):
            m = self.model_manifest(stock_id, set_id)
            res_root = self.stock_dir(stock_id) / "models" / set_id / "results"
            results = [record.results_entry(p.parent) for p in sorted(res_root.glob("*/summary.json"))] \
                if res_root.is_dir() else []
            model_sets.append({
                "model_set_id": set_id, "created_at": m.get("created_at"), "recipe": m.get("recipe"),
                "num_models": m.get("num_models"), "num_failed": m.get("num_failed"),
                "models_sha256": m.get("models_sha256"),
                "idf_sha256": {e["building_id"]: e.get("idf_sha256") for e in m["models"]},
                "software": m.get("software"), "results": results,
            })
        sources = self.get_meta(stock_id).get("sources") or []
        rec = {
            "record_version": record.RECORD_VERSION, "created_at": record.now(), "stock_id": stock_id,
            "software": self.software_versions(), "steps": steps, "model_sets": model_sets,
            "sources": sources, "attribution": provenance.attribution_text(sources),
        }
        warnings = [f"stock {s['stock_id']!r} was created before project records; it cannot be rerun"
                    for s in steps if not s["recipe"]]
        warnings += [f"model set {m['model_set_id']!r} was created before project records; it cannot be rerun"
                     for m in model_sets if not m["recipe"]]
        rec["warnings"] = warnings
        path = self.stock_dir(stock_id) / "project_record.json"
        path.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        return {"stock_id": stock_id, "record_path": str(path), "steps": [s["stock_id"] for s in steps],
                "model_sets": [{"model_set_id": m["model_set_id"], "num_models": m["num_models"],
                                "results": [r["results_id"] for r in m["results"]]} for m in model_sets],
                "software": {k: rec["software"][k] for k in ("ubem_mcp", "python")} | {
                    "energyplus": rec["software"]["energyplus"]["idd_version"],
                    "archetype_library_sha256": rec["software"]["archetype_library"]["sha256"]},
                "attribution": rec["attribution"], "warnings": warnings}

    def rerun_record(self, record_path: str, id_suffix: str = "_rerun") -> Dict[str, Any]:
        """Repeat the steps in a project record under new ids (old id +
        id_suffix): rebuild every stock from the recorded input files, then
        regenerate every model set and compare each model with its recorded
        hash. Simulation is left to the caller."""
        path = Path(record_path)
        if path.is_dir():
            path = path / "project_record.json"
        if not path.is_file():
            raise FileNotFoundError(f"No project record at {record_path!r}")
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("record_version") != record.RECORD_VERSION:
            raise ValueError(f"Unsupported project record version {rec.get('record_version')!r}")
        warnings = record.compare_software(rec.get("software") or {}, self.software_versions())
        new_ids: Dict[str, str] = {}

        def check_inputs(rcp):
            for inp in rcp.get("inputs") or []:
                current = record.sha256_file(inp["path"]) if inp.get("path") else None
                if current is None:
                    raise FileNotFoundError(f"Recorded input {inp['role']} not found: {inp.get('path')}")
                if inp.get("sha256") and current != inp["sha256"]:
                    warnings.append(f"{inp['role']} {inp['path']} has changed since the record was made")

        for step in rec["steps"]:
            rcp = step.get("recipe")
            if not rcp:
                raise ValueError(f"Stock {step['stock_id']!r} has no recipe (made before project records)")
            new_id = step["stock_id"] + id_suffix
            check_inputs(rcp)
            a, op = rcp["args"], rcp["op"]
            if op == "load":
                self.load_from_file(a["path"], new_id, license=a.get("license"), attribution=a.get("attribution"),
                                    **(a.get("loader_options") or {}))
            elif op == "fetch_osm":
                raw = json.loads(Path(rcp["inputs"][0]["path"]).read_text(encoding="utf-8"))
                old_osm = self.get_meta(step["stock_id"]).get("osm") or {}
                self._new_id(new_id)
                self._from_osm_raw(raw, new_id, a["bbox"], a["overpass_url"], old_osm.get("query", ""),
                                   old_osm.get("retrieved_at") or record.now(), a.get("loader_options") or {})
            elif op == "enrich":
                self.enrich(new_ids[rcp["parent"]], a["layer_path"], a["field_map"], new_id,
                            layer_license=a.get("layer_license"), layer_attribution=a.get("layer_attribution"),
                            **(a.get("options") or {}))
            elif op == "assign_archetypes":
                self.assign_archetypes(new_ids[rcp["parent"]], new_id, **a)
            else:
                raise ValueError(f"Unknown recipe operation {op!r}")
            new_ids[step["stock_id"]] = new_id
            w = step.get("weather")
            if w and w.get("epw"):
                if w.get("epw_sha256") and record.sha256_file(w["epw"]) != w["epw_sha256"]:
                    warnings.append(f"weather file {w['epw']} has changed since the record was made")
                self.resolve_weather(new_id, weather_file=w["epw"])

        leaf = new_ids[rec["stock_id"]]
        order = {"generate_models": 0, "set_building_models": 1, "generate_scenario": 2}
        sets = sorted(rec["model_sets"], key=lambda m: order.get((m.get("recipe") or {}).get("op"), 0))
        regenerated = []
        for ms in sets:
            rcp = ms.get("recipe")
            if not rcp:
                warnings.append(f"model set {ms['model_set_id']!r} has no recipe and was skipped")
                continue
            check_inputs(rcp)
            a = rcp["args"]
            if rcp["op"] == "set_building_models":
                self.set_building_models(leaf, a["base_model_set_id"], a["building_models"],
                                         new_model_set_id=ms["model_set_id"], license=a.get("license"),
                                         attribution=a.get("attribution"))
            elif rcp["op"] == "generate_scenario":
                self.generate_scenario(leaf, a["baseline_model_set_id"], a["scenario"],
                                       model_set_id=ms["model_set_id"])
            else:
                self.generate_models(leaf, a["climate_zone"], weather_file=a.get("weather_file"), hvac=a["hvac"],
                                     building_ids=a["building_ids"], model_set_id=ms["model_set_id"])
            new = self.model_manifest(leaf, ms["model_set_id"])
            now_hashes = {e["building_id"]: e.get("idf_sha256") for e in new["models"]}
            old_hashes = ms.get("idf_sha256") or {}
            differ = sorted(b for b in old_hashes if now_hashes.get(b) != old_hashes[b])
            regenerated.append({
                "model_set_id": ms["model_set_id"], "num_models": new["num_models"],
                "identical": len(old_hashes) - len(differ), "different": len(differ),
                "different_buildings": differ[:20],
                "models_identical": new.get("models_sha256") == ms.get("models_sha256"),
                "recorded_results": [{"results_id": r["results_id"], "district": r.get("district")}
                                     for r in ms.get("results") or []],
            })
            if differ:
                warnings.append(f"model set {ms['model_set_id']!r}: {len(differ)} model(s) differ from the record")
        return {"record_path": str(path), "stock_ids": new_ids, "stock_id": leaf, "model_sets": regenerated,
                "reproduced": not warnings and all(m["models_identical"] for m in regenerated),
                "warnings": warnings}

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
        meta = self.get_meta(stock_id)
        opts = meta.get("loader_options") or {}
        stock = load_geojson(str(path), min_footprint_m2=0.0, mode=opts.get("mode", "model"),
                             keep_personal_fields=bool(opts.get("keep_personal_fields")))
        stock.source = meta.get("source", stock.source)
        stock.personal_fields_removed = list((meta.get("summary") or {}).get("personal_fields_removed") or [])
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

    def _register(self, stock: BuildingStock, stock_id: str, extra_meta: Dict[str, Any],
                  sources: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        stock_dir = self.root / stock_id
        stock_dir.mkdir(parents=True, exist_ok=True)
        sources = sources or []
        attribution = provenance.attribution_text(sources)

        geo = stock.to_geojson()
        geo.setdefault("ubem_mcp", {})["attribution"] = attribution
        with open(stock_dir / "buildings.geojson", "w", encoding="utf-8") as f:
            json.dump(geo, f)
        with open(stock_dir / "excluded.json", "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in stock.excluded], f, indent=1)
        summary = stock.summary()
        meta = {
            "stock_id": stock_id,
            "created_at": datetime.now().isoformat(),
            "source": stock.source,
            "summary": summary,
            "sources": sources,
            **extra_meta,
        }
        with open(stock_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

        self._stocks[stock_id] = stock
        logger.info("Registered stock %s: %d buildings", stock_id, len(stock))
        warnings = provenance.licence_warnings(sources)
        if summary["personal_fields_removed"]:
            warnings.append("personal columns were dropped on load: " + ", ".join(summary["personal_fields_removed"])
                            + " (load with keep_personal_fields=true only if you need them and may hold them)")
        return {"stock_id": stock_id, "stock_directory": str(stock_dir), **summary,
                "sources": [{"name": s["name"], "license": s["license"]} for s in sources],
                "attribution": attribution, "warnings": warnings}

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


def scan_runs(runs_dir: Path, building_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Find each building's most recent run under runs/<NNNN>_<building_id>/."""
    out: Dict[str, Dict[str, Any]] = {}
    if not runs_dir.is_dir():
        return out
    wanted = set(building_ids)
    for d in sorted(runs_dir.iterdir()):
        bid = d.name.split("_", 1)[1] if "_" in d.name else d.name
        if not d.is_dir() or bid not in wanted:
            continue
        errs = list(d.glob("*.err"))
        text = errs[0].read_text(errors="replace") if errs else ""
        ok = "EnergyPlus Completed Successfully" in text and any(d.glob("*.sql"))
        out[bid] = {"status": "success" if ok else "failed", "output_directory": str(d),
                    "error": None if ok else "simulation did not complete successfully"}
    return out
