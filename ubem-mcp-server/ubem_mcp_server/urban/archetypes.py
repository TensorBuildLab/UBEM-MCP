"""
Building use-type taxonomy for UBEM-MCP, and the rules that map raw GIS
attributes (OpenStreetMap tags, assessor land-use codes, free text) onto it.

The taxonomy follows the U.S. DOE commercial prototype buildings plus the
DOE/IECC single-family residential prototype, so every archetype key maps to
a published reference model whose constructions, loads and schedules the
model generator can draw on. Vintage bins follow the DOE commercial reference
buildings (pre-1980, 1980-2004, new construction).
"""

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple


@dataclass(frozen=True)
class Archetype:
    key: str
    label: str
    sector: str  # "residential" | "commercial"
    default_floors: int
    floor_to_floor_m: float


ARCHETYPES: Dict[str, Archetype] = {a.key: a for a in [
    Archetype("single_family", "Single-family house", "residential", 1, 3.0),
    Archetype("midrise_apartment", "Mid-rise apartment", "residential", 4, 3.05),
    Archetype("highrise_apartment", "High-rise apartment", "residential", 10, 3.05),
    Archetype("small_office", "Small office", "commercial", 1, 3.05),
    Archetype("medium_office", "Medium office", "commercial", 3, 3.96),
    Archetype("large_office", "Large office", "commercial", 12, 3.96),
    Archetype("standalone_retail", "Stand-alone retail", "commercial", 1, 6.1),
    Archetype("strip_mall", "Strip mall", "commercial", 1, 5.18),
    Archetype("primary_school", "Primary school", "commercial", 1, 4.0),
    Archetype("secondary_school", "Secondary school", "commercial", 2, 4.0),
    Archetype("outpatient", "Outpatient healthcare", "commercial", 3, 4.0),
    Archetype("hospital", "Hospital", "commercial", 5, 4.27),
    Archetype("small_hotel", "Small hotel", "commercial", 4, 3.05),
    Archetype("large_hotel", "Large hotel", "commercial", 6, 3.05),
    Archetype("warehouse", "Warehouse", "commercial", 1, 8.53),
    Archetype("quick_service_restaurant", "Quick-service restaurant", "commercial", 1, 3.05),
    Archetype("full_service_restaurant", "Full-service restaurant", "commercial", 1, 3.05),
]}

VINTAGES = ("pre1980", "1980_2004", "post2004")

# Tag values for structures that are not conditioned buildings and should be
# excluded from an energy model rather than forced into an archetype.
UNCONDITIONED_USES = frozenset({
    "garage", "garages", "carport", "shed", "roof", "parking", "hut", "cabin_shelter",
    "greenhouse", "ruins", "construction", "container", "tank", "silo", "bunker",
    "transformer_tower", "service", "toilets", "kiosk", "boathouse", "bridge",
    "stadium_stand", "grandstand", "canopy",
})

# Raw use value -> archetype key (or a sentinel resolved by size/height).
_USE_MAP: Dict[str, str] = {
    # residential
    "house": "single_family", "detached": "single_family", "semidetached_house": "single_family",
    "terrace": "single_family", "bungalow": "single_family", "farm": "single_family",
    "cabin": "single_family", "single_family": "single_family", "sfh": "single_family",
    "residential": "_residential", "apartments": "_apartment", "apartment": "_apartment",
    "dormitory": "_apartment", "condominium": "_apartment", "multifamily": "_apartment",
    "multi_family": "_apartment",
    # office / civic
    "office": "_office", "commercial": "_office", "government": "_office", "civic": "_office",
    "public": "_office", "bank": "_office", "townhall": "_office", "courthouse": "_office",
    "police": "_office", "fire_station": "_office", "library": "_office",
    # retail
    "retail": "standalone_retail", "shop": "standalone_retail", "supermarket": "standalone_retail",
    "mall": "strip_mall", "marketplace": "strip_mall", "department_store": "standalone_retail",
    # education
    "school": "primary_school", "kindergarten": "primary_school", "childcare": "primary_school",
    # healthcare
    "hospital": "hospital", "clinic": "outpatient", "doctors": "outpatient",
    "dentist": "outpatient", "healthcare": "outpatient",
    # lodging
    "hotel": "_hotel", "motel": "small_hotel", "hostel": "small_hotel", "guest_house": "small_hotel",
    # warehouse / industrial
    "warehouse": "warehouse", "industrial": "warehouse", "storage": "warehouse",
    "manufacture": "warehouse", "hangar": "warehouse", "depot": "warehouse",
    # food service
    "restaurant": "full_service_restaurant", "cafe": "quick_service_restaurant",
    "fast_food": "quick_service_restaurant",
}

# Uses with no DOE prototype, mapped to the closest one. The building gets a
# note so the approximation stays visible in results.
APPROXIMATE_USES: Dict[str, str] = {
    "university": "secondary_school", "college": "secondary_school",
    # assembly: large open spaces with intermittent occupancy, like a school's gym/auditorium
    "church": "secondary_school", "chapel": "secondary_school", "cathedral": "secondary_school",
    "mosque": "secondary_school", "synagogue": "secondary_school", "temple": "secondary_school",
    "religious": "secondary_school", "place_of_worship": "secondary_school",
    "community_centre": "secondary_school", "sports_centre": "secondary_school",
    "sports_hall": "secondary_school", "theatre": "secondary_school", "cinema": "secondary_school",
    "museum": "secondary_school", "arts_centre": "secondary_school", "events_venue": "secondary_school",
    # transport / service retail
    "train_station": "standalone_retail", "transportation": "standalone_retail",
    "fuel": "standalone_retail", "pharmacy": "standalone_retail",
    # residential care
    "nursing_home": "small_hotel", "social_facility": "small_hotel",
}

# Office size thresholds by gross floor area (m2), between the DOE small
# (511 m2), medium (4,982 m2) and large (46,320 m2) office prototypes.
_SMALL_OFFICE_MAX_GFA = 2_000.0
_MEDIUM_OFFICE_MAX_GFA = 20_000.0


# Values that say only "this is a building" (OSM building=yes), not its use.
GENERIC_USES = frozenset({"yes", "true", "1", "building"})


def is_generic_use(value) -> bool:
    return value is None or str(value).strip() == "" or _norm(value) in GENERIC_USES


def _norm(value) -> str:
    return re.sub(r"[\s\-/]+", "_", str(value).strip().lower())


def resolve_archetype_key(value) -> Optional[str]:
    """Return the archetype key if `value` names one directly (key or label)."""
    if value is None:
        return None
    v = _norm(value)
    if v in ARCHETYPES:
        return v
    for a in ARCHETYPES.values():
        if _norm(a.label) == v:
            return a.key
    return None


def classify_use(
    use_values: Iterable[Optional[str]],
    footprint_area_m2: float,
    num_floors: Optional[int],
) -> Tuple[Optional[str], str]:
    """Map raw use values to an archetype key.

    `use_values` are candidate raw values in priority order (e.g. an explicit
    use column, then OSM `amenity`/`shop`/`office`, then `building`). Returns
    (archetype_key, reason). archetype_key is None when nothing matched
    (caller applies its default) or "_unconditioned" for structures that
    should be excluded.
    """
    for raw in use_values:
        if is_generic_use(raw):
            continue
        direct = resolve_archetype_key(raw)
        if direct:
            return direct, f"explicit archetype {raw!r}"
        v = _norm(raw)
        if v in UNCONDITIONED_USES:
            return "_unconditioned", f"unconditioned structure {raw!r}"
        mapped = _USE_MAP.get(v)
        if mapped is not None:
            return _resolve_sentinel(mapped, footprint_area_m2, num_floors), f"use {raw!r}"
        if v in APPROXIMATE_USES:
            key = APPROXIMATE_USES[v]
            return key, f"use {raw!r} approximated as {key} (no DOE prototype)"
    return None, "no recognized use"


def _resolve_sentinel(mapped: str, footprint_area_m2: float, num_floors: Optional[int]) -> str:
    floors = num_floors or 1
    if mapped == "_residential":
        return "single_family" if floors <= 3 else _resolve_sentinel("_apartment", footprint_area_m2, floors)
    if mapped == "_apartment":
        return "highrise_apartment" if floors >= 10 else "midrise_apartment"
    if mapped == "_office":
        gfa = footprint_area_m2 * floors
        if gfa < _SMALL_OFFICE_MAX_GFA:
            return "small_office"
        return "medium_office" if gfa < _MEDIUM_OFFICE_MAX_GFA else "large_office"
    if mapped == "_hotel":
        return "small_hotel" if floors <= 4 else "large_hotel"
    return mapped


def vintage_for_year(year: Optional[int]) -> Optional[str]:
    if year is None:
        return None
    if year < 1980:
        return "pre1980"
    if year < 2004:
        return "1980_2004"
    return "post2004"


def parse_year(value) -> Optional[int]:
    """Parse construction years like 1975, "1975-06-01", "~1975", "1970s", "C1950"."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        year = int(value)
        return year if 1600 <= year <= 2100 else None
    m = re.search(r"(1[6-9]\d\d|20\d\d)", str(value))
    return int(m.group(1)) if m else None
