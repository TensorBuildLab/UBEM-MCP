"""
Data provenance for building stocks: where each dataset came from, under
which licence, the attribution it requires, and removal of personal fields.

New in UBEM-MCP (not part of EnergyPlus-MCP). See NOTICE.md.

Footprint and parcel layers often carry columns about people, such as the
owner's name and mailing address in assessor data. UBEM needs none of them,
so they are dropped when a stock is loaded unless the user asks to keep them.
"""

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Column names that identify or contact a person. Matched case-insensitively
# against the whole column name. Building addresses (OSM addr:*, a parcel's
# situs address) describe the building, not a person, and are kept.
PERSONAL_FIELD_PATTERN = re.compile(
    r"owner|taxpayer|tax_?payer|grantee|grantor|buyer|seller|(?:tenant|resident|occupant)_?name"
    r"|mail(?:ing)?_?(?:addr|address|street|city|state|zip|name|line)|^mail_|^mail$|^mail_?\d+$|e-?mail"
    r"|phone|telephone|^fax$|contact|first_?name|last_?name|full_?name|\bssn\b|birth_?date",
    re.IGNORECASE,
)


def is_personal_field(name: str) -> bool:
    return bool(PERSONAL_FIELD_PATTERN.search(str(name)))


def scrub_personal_fields(props: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return a copy of `props` without personal columns, and the names removed.
    A nested `source_properties` dict (a saved stock) is scrubbed the same way."""
    clean: Dict[str, Any] = {}
    removed: List[str] = []
    for key, value in props.items():
        if key == "source_properties" and isinstance(value, dict):
            inner, gone = scrub_personal_fields(value)
            clean[key] = inner
            removed.extend(gone)
        elif is_personal_field(key):
            removed.append(key)
        else:
            clean[key] = value
    return clean, removed


# --------------------------------------------------------------------------- #
# Source records
# --------------------------------------------------------------------------- #

# Licences that require derived databases to stay under the same licence.
SHARE_ALIKE = {"ODbL-1.0"}
# Licences known to be compatible with combining into an ODbL database.
ODBL_COMPATIBLE = {"ODbL-1.0", "CC0-1.0", "PDDL-1.0", "public domain", "CC-BY-4.0", "CDLA-Permissive-2.0"}

UNKNOWN = "unknown"


def source_record(source_id: str, name: str, role: str, license: Optional[str] = None,
                  attribution: Optional[str] = None, url: Optional[str] = None,
                  notes: Optional[str] = None, retrieved_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": source_id,
        "name": name,
        "role": role,
        "license": license or UNKNOWN,
        "attribution": attribution,
        "url": url,
        "notes": notes,
        "retrieved_at": retrieved_at or datetime.now().isoformat(timespec="seconds"),
    }


def osm_source(retrieved_at: Optional[str] = None, osm_timestamp: Optional[str] = None) -> Dict[str, Any]:
    note = ("Produced works must credit OpenStreetMap contributors visibly; a database derived from "
            "this data must be shared under the ODbL.")
    if osm_timestamp:
        note += f" OSM data as of {osm_timestamp}."
    return source_record("openstreetmap", "OpenStreetMap building footprints", "footprints",
                         license="ODbL-1.0", attribution="© OpenStreetMap contributors",
                         url="https://www.openstreetmap.org/copyright", notes=note, retrieved_at=retrieved_at)


def weather_source(url: Optional[str], dataset: Optional[str]) -> Dict[str, Any]:
    return source_record(
        "climate.onebuilding.org", f"Climate.OneBuilding.Org {dataset or 'weather file'}", "weather",
        license="Climate.OneBuilding.Org terms",
        attribution="Lawrie LK, Crawley DB. Development of Global Typical Meteorological Years (TMYx). "
                    "https://climate.onebuilding.org",
        url=url, notes="Downloaded at run time and not redistributed with UBEM-MCP.")


ARCHETYPE_SOURCES = [
    source_record("doe-commercial-reference-buildings", "DOE Commercial Reference Buildings v1.3_5.0",
                  "archetype parameters", license="CC-BY-4.0",
                  attribution="U.S. Department of Energy Commercial Reference Buildings (Deru et al., 2011), "
                              "OpenEI, CC BY 4.0",
                  url="https://data.openei.org/submissions/153", retrieved_at="bundled"),
    source_record("doe-pnnl-residential-prototype", "DOE/PNNL IECC single-family prototype building model",
                  "archetype parameters", license="public domain",
                  attribution="U.S. DOE / Pacific Northwest National Laboratory residential prototype building models",
                  url="https://www.energycodes.gov/prototype-building-models", retrieved_at="bundled"),
]


def merge_sources(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Concatenate source lists, keeping the first record for each id."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for s in group or []:
            if s["id"] not in seen:
                seen.add(s["id"])
                out.append(s)
    return out


def attribution_text(sources: Iterable[Dict[str, Any]]) -> str:
    parts = [s["attribution"] for s in sources if s.get("attribution")]
    return "; ".join(dict.fromkeys(parts))


def licence_warnings(sources: Iterable[Dict[str, Any]]) -> List[str]:
    """Warnings a user should see before sharing results built from these sources."""
    sources = list(sources)
    warnings = []
    for s in sources:
        if s["license"] == UNKNOWN:
            warnings.append(f"the licence of {s['name']} is unknown; record it (license=...) before sharing "
                            f"data or results derived from it")
    if any(s["license"] in SHARE_ALIKE for s in sources):
        others = [s["name"] for s in sources
                  if s["license"] not in ODBL_COMPATIBLE and s["role"] in ("footprints", "attributes")]
        if others:
            warnings.append("ODbL-licensed footprints are combined with " + ", ".join(others)
                            + "; a shared derived database must be released under the ODbL, so check that "
                              "those terms allow it")
    return warnings
