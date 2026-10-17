"""
Generate archetype EnergyPlus models for (part of) a building stock.

Each building becomes one IDF; every other building in the stock is a
potential neighbor for party walls and shading. A manifest records, per
building, the model path, the archetype parameters' provenance and every
modeling assumption, so results can be traced back.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from shapely import STRtree

from .idd import load_idd
from .library import data_sources, normalize_climate_zone, resolve
from .modelgen import SHADING_RADIUS_M, ModelBuilder, Neighbor, epw_location
from .stock import Building, BuildingStock


def generate_models(
    stock: BuildingStock,
    buildings: Sequence[Building],
    climate_zone: str,
    output_dir: str,
    idd_path: str,
    weather_file: Optional[str] = None,
    hvac: str = "template",
    param_hook: Optional[Callable] = None,
) -> Dict[str, Any]:
    """`param_hook(building, params) -> (params, applied)` may modify a
    building's archetype parameters (e.g. retrofit measures); `applied` is
    recorded in the manifest as the building's "retrofit"."""
    cz = normalize_climate_zone(climate_zone)
    idd = load_idd(idd_path)
    location = epw_location(weather_file) if weather_file else None
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    all_buildings = stock.buildings
    tree = STRtree([b.footprint for b in all_buildings])
    entries: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for b in buildings:
        try:
            near_idx = tree.query(b.footprint.buffer(SHADING_RADIUS_M))
            neighbors = [Neighbor(all_buildings[int(i)].footprint, all_buildings[int(i)].height_m)
                         for i in near_idx if all_buildings[int(i)].building_id != b.building_id]
            params = resolve(b.archetype, b.vintage, cz)
            applied: List[Dict[str, str]] = []
            if param_hook is not None:
                params, applied = param_hook(b, params)
            builder = ModelBuilder(idd, params, hvac=hvac)
            text = builder.build(b, neighbors, location)
            path = out / f"{b.building_id}.idf"
            path.write_text(text, encoding="utf-8")
            info = builder.info
            entries.append({
                "building_id": b.building_id,
                "idf_path": str(path),
                "archetype": b.archetype,
                "vintage": b.vintage,
                "num_floors": b.num_floors,
                "gross_floor_area_m2": round(b.gross_floor_area_m2, 1),
                "hvac_system": (params.hvac_system + ("_heat_pump" if params.heat_pump_heating_cop else "")
                                if hvac == "template" else "ideal_loads"),
                "zones": info.num_zones,
                "zoning": info.zoning,
                "floors_modeled": info.floors_modeled,
                "party_walls": info.party_walls,
                "shading_surfaces": info.shading_surfaces,
                "windows": info.windows,
                "inferred_fields": list(b.inferred_fields),
                "notes": list(b.notes) + info.notes,
                "assumptions": params.assumptions,
                **({"retrofit": applied} if param_hook is not None else {}),
            })
        except Exception as e:  # noqa: BLE001 - one bad building must not stop the set
            failures.append({"building_id": b.building_id, "error": f"{type(e).__name__}: {e}"})

    manifest = {
        "created_at": datetime.now().isoformat(),
        "climate_zone": cz,
        "weather_file": weather_file,
        "hvac": hvac,
        "energyplus_idd_version": idd.version,
        "data_sources": data_sources(),
        "num_models": len(entries),
        "num_failed": len(failures),
        "models": entries,
        "failures": failures,
    }
    with open(out / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, default=str)
    return manifest


def summarize_manifest(manifest: Dict[str, Any], output_dir: str) -> Dict[str, Any]:
    from collections import Counter

    models = manifest["models"]
    return {
        "model_directory": output_dir,
        "manifest": os.path.join(output_dir, "manifest.json"),
        "climate_zone": manifest["climate_zone"],
        "hvac": manifest["hvac"],
        "num_models": manifest["num_models"],
        "num_failed": manifest["num_failed"],
        "failures": manifest["failures"][:10],
        "by_hvac_system": dict(Counter(m["hvac_system"] for m in models)),
        "by_zoning": dict(Counter(m["zoning"] for m in models)),
        "total_zones": sum(m["zones"] for m in models),
        "buildings_with_party_walls": sum(1 for m in models if m["party_walls"]),
    }
