"""
Archetype EnergyPlus model generation for one building of a building stock.

Geometry:
  - The footprint is extruded floor by floor. Buildings with more than three
    floors are modeled as a ground, a middle and a top floor, with the middle
    floor's zones multiplied to stand for the intermediate floors.
  - Commercial floors large enough get core/perimeter zoning (4.57 m deep
    perimeter zones, as in the DOE prototypes); other floors are one zone.
  - Walls shared with an adjacent building (party walls) are adiabatic and
    windowless; nearby buildings are added as shading surfaces.
  - Floors between stories are adiabatic; the ground floor uses the F-factor
    slab method, so no separate ground-temperature preprocessing is needed.

Everything else (constructions, loads, schedules, HVAC) comes from
library.resolve(archetype, vintage, climate_zone).
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient

from .idd import IDD, IDFWriter
from .library import ArchetypeParams, DAY_TYPES, expand_rules

PERIMETER_DEPTH_M = 4.57
MIN_CORE_SHARE = 0.15
MAX_PERIMETER_EDGES = 12
PARTY_WALL_TOLERANCE_M = 0.3
PARTY_WALL_MIN_SHARE = 0.5
MIN_WINDOW_WALL_WIDTH_M = 1.0
SHADING_RADIUS_M = 60.0
MAX_SHADING_NEIGHBORS = 20
MAX_SHADING_SURFACES = 60
MIN_SHADING_EDGE_M = 2.0
MAX_FOOTPRINT_VERTICES = 30
# Simplification tolerances (m) tried for detailed footprints, and the largest
# area change accepted.
SIMPLIFY_TOLERANCES = (0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
MAX_SIMPLIFY_AREA_CHANGE = 0.05
SLAB_F_FACTOR = 1.264  # W/m-K, uninsulated slab-on-grade (0.73 Btu/h-ft-F)

HVAC_SYSTEMS = ("template", "ideal")

# ASHRAE Fundamentals effective-leakage-area model coefficients for 1/2/3+
# story buildings, wind coefficient for shelter class 3 (typical suburban).
_STACK_COEF = {1: 0.000145, 2: 0.000290, 3: 0.000435}
_WIND_COEF = {1: 0.000174, 2: 0.000231, 3: 0.000271}


@dataclass
class Neighbor:
    footprint: Polygon
    height_m: float


@dataclass
class ZoneGeom:
    name: str
    polygon: Polygon  # CCW, local metres
    z0: float
    z1: float
    multiplier: int
    is_ground: bool
    is_top: bool


@dataclass
class ModelInfo:
    num_zones: int = 0
    floors_modeled: List[Tuple[int, int]] = field(default_factory=list)  # (story index, multiplier)
    zoning: str = ""
    party_walls: int = 0
    shading_surfaces: int = 0
    windows: int = 0
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def representative_floors(num_floors: int) -> List[Tuple[int, int, bool, bool]]:
    """[(story_index, multiplier, is_ground, is_top)]."""
    if num_floors <= 3:
        return [(i, 1, i == 0, i == num_floors - 1) for i in range(num_floors)]
    return [(0, 1, True, False), (1, num_floors - 2, False, False), (num_floors - 1, 1, False, True)]


def core_perimeter(footprint: Polygon, depth: float = PERIMETER_DEPTH_M) -> Optional[List[Polygon]]:
    """[perimeter_0..perimeter_n-1, core] or None when the footprint can't be split cleanly."""
    outer = list(footprint.exterior.coords)[:-1]
    n = len(outer)
    if n > MAX_PERIMETER_EDGES:
        return None
    inner = footprint.buffer(-depth, join_style="mitre", mitre_limit=10.0)
    if inner.is_empty or inner.geom_type != "Polygon" or inner.area < MIN_CORE_SHARE * footprint.area:
        return None
    inner = orient(inner, 1.0)
    ring = list(inner.exterior.coords)[:-1]
    if len(ring) != n:
        return None
    # Rotate the inner ring so vertex i is the offset of outer vertex i.
    start = min(range(n), key=lambda k: (ring[k][0] - outer[0][0]) ** 2 + (ring[k][1] - outer[0][1]) ** 2)
    ring = ring[start:] + ring[:start]
    limit = depth * 10.0
    if any(math.dist(outer[i], ring[i]) > limit for i in range(n)):
        return None
    zones = []
    for i in range(n):
        j = (i + 1) % n
        quad = Polygon([outer[i], outer[j], ring[j], ring[i]])
        if not quad.is_valid or quad.area <= 0.5 or not quad.exterior.is_ccw:
            return None
        zones.append(quad)
    core = Polygon(ring)
    if not core.is_valid:
        return None
    return zones + [core]


def _simplified(poly: Polygon, tolerance: float) -> Optional[Polygon]:
    s = orient(poly.simplify(tolerance, preserve_topology=True), 1.0)
    if s.is_empty or s.geom_type != "Polygon" or not s.is_valid:
        return None
    if abs(s.area - poly.area) > MAX_SIMPLIFY_AREA_CHANGE * poly.area:
        return None
    return s


def equal_area_rectangle(poly: Polygon) -> Polygon:
    """The polygon's minimum rotated rectangle, scaled about its centroid to the polygon's area."""
    mrr = poly.minimum_rotated_rectangle
    k = math.sqrt(poly.area / mrr.area)
    return orient(affinity.scale(mrr, k, k, origin=mrr.centroid), 1.0)


def model_footprint(footprint: Polygon, commercial: bool):
    """(footprint to model, core/perimeter zones or None, note or None).

    Detailed outlines (e.g. from OpenStreetMap) are simplified until they have
    at most MAX_FOOTPRINT_VERTICES vertices; commercial footprints are further
    simplified, if needed, until core/perimeter zoning succeeds. No accepted
    simplification changes the area by more than 5%. An outline that cannot
    be simplified that far is replaced by an oriented rectangle of the same
    area, so every building still gets a model.
    """
    footprint = orient(footprint, 1.0)
    n0 = len(footprint.exterior.coords) - 1
    candidates = [(0.0, footprint)] + [(t, _simplified(footprint, t)) for t in SIMPLIFY_TOLERANCES]
    candidates = [(t, p) for t, p in candidates if p is not None]
    fitting = [(t, p) for t, p in candidates if len(p.exterior.coords) - 1 <= MAX_FOOTPRINT_VERTICES]
    if not fitting:
        rect = equal_area_rectangle(footprint)
        note = (f"footprint with {n0} vertices could not be simplified to {MAX_FOOTPRINT_VERTICES} vertices "
                f"within {MAX_SIMPLIFY_AREA_CHANGE:.0%} area change; modeled as an oriented rectangle of the same area")
        return rect, (core_perimeter(rect) if commercial else None), note
    chosen_t, chosen = fitting[0]
    zones = None
    if commercial:
        for t, p in fitting:
            zones = core_perimeter(p)
            if zones:
                chosen_t, chosen = t, p
                break
    note = None
    if chosen_t > 0:
        n1 = len(chosen.exterior.coords) - 1
        note = (f"footprint simplified from {n0} to {n1} vertices (tolerance {chosen_t} m, "
                f"area change {100 * (chosen.area / footprint.area - 1):+.1f}%)")
    return chosen, zones, note


def _edges(poly: Polygon):
    pts = list(poly.exterior.coords)[:-1]
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _key(p1, p2):
    return (round(p1[0], 3), round(p1[1], 3), round(p2[0], 3), round(p2[1], 3))


def party_wall_heights(footprint: Polygon, neighbors: Sequence[Neighbor]) -> Dict[tuple, float]:
    """Footprint edge key -> height up to which the edge is a party wall."""
    out: Dict[tuple, float] = {}
    for p1, p2 in _edges(footprint):
        edge = LineString([p1, p2])
        if edge.length < 1e-6:
            continue
        for nb in neighbors:
            if nb.footprint.distance(edge) > PARTY_WALL_TOLERANCE_M:
                continue
            band = nb.footprint.exterior.buffer(PARTY_WALL_TOLERANCE_M)
            if edge.intersection(band).length / edge.length >= PARTY_WALL_MIN_SHARE:
                out[_key(p1, p2)] = max(out.get(_key(p1, p2), 0.0), nb.height_m)
    return out


def _wall_vertices(p1, p2, z0, z1):
    # Counter-clockwise seen from outside, starting upper-left.
    return [(p1[0], p1[1], z1), (p1[0], p1[1], z0), (p2[0], p2[1], z0), (p2[0], p2[1], z1)]


def _scaled(vertices, factor):
    cx = sum(v[0] for v in vertices) / len(vertices)
    cy = sum(v[1] for v in vertices) / len(vertices)
    cz = sum(v[2] for v in vertices) / len(vertices)
    return [(cx + factor * (x - cx), cy + factor * (y - cy), cz + factor * (z - cz)) for x, y, z in vertices]


def _flat(vertices):
    return [round(c, 4) for v in vertices for c in v]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

class ModelBuilder:
    def __init__(self, idd: IDD, params: ArchetypeParams, hvac: str = "template"):
        if hvac not in HVAC_SYSTEMS:
            raise ValueError(f"hvac must be one of {HVAC_SYSTEMS}")
        self.idd = idd
        self.p = params
        self.hvac = hvac
        self.w = IDFWriter(idd)
        self.info = ModelInfo()

    # -- entry point --------------------------------------------------------

    def build(self, building, neighbors: Sequence[Neighbor] = (), location: Optional[dict] = None) -> str:
        p, w = self.p, self.w
        footprint, split, note = model_footprint(building.footprint, p.sector == "commercial")
        if note:
            self.info.notes.append(note)
        f2f = building.floor_to_floor_m
        floors = representative_floors(building.num_floors)
        party = party_wall_heights(footprint, neighbors)
        self.info.party_walls = len(party)
        self.info.zoning = "core_perimeter" if split else "single_zone_per_floor"
        if p.sector == "commercial" and not split:
            self.info.notes.append("footprint too small or irregular for core/perimeter zoning; one zone per floor")

        self._header(building, location)
        self._constructions()
        self._schedules()

        zones: List[ZoneGeom] = []
        for story, mult, is_ground, is_top in floors:
            z0, z1 = story * f2f, (story + 1) * f2f
            polys = split if split else [footprint]
            for k, poly in enumerate(polys):
                if split:
                    label = "Core" if k == len(polys) - 1 else f"Perim{k + 1}"
                else:
                    label = "Zone"
                zones.append(ZoneGeom(f"F{story + 1}_{label}", poly, z0, z1, mult, is_ground, is_top))
            self.info.floors_modeled.append((story + 1, mult))
        if building.num_floors > 3:
            self.info.notes.append(
                f"{building.num_floors} floors modeled as ground, middle (x{building.num_floors - 2}) and top floors")
        self.info.num_zones = len(zones)

        ext_wall_area = self._geometry(zones, footprint, party)
        self._loads(zones, building, ext_wall_area)
        self._hot_water(building)
        self._hvac(zones, floors)
        self._shading(footprint, neighbors)
        self._outputs()
        return w.text()

    # -- header -------------------------------------------------------------

    def _header(self, building, location):
        w = self.w
        w.add("Version", {"Version Identifier": ".".join(self.idd.version.split(".")[:2])})
        has_plant = self.hvac == "template" and (self.p.hvac_system == "vav" or self.p.reheat_coil == "HotWater")
        w.add("SimulationControl", {
            "Do Zone Sizing Calculation": True, "Do System Sizing Calculation": True,
            "Do Plant Sizing Calculation": has_plant, "Run Simulation for Sizing Periods": False,
            "Run Simulation for Weather File Run Periods": True,
        })
        w.add("Building", {
            "Name": f"Building {building.building_id}", "North Axis": 0, "Terrain": "Suburbs",
            "Loads Convergence Tolerance Value": 0.04, "Temperature Convergence Tolerance Value": 0.4,
            "Solar Distribution": "FullExterior", "Maximum Number of Warmup Days": 50,
            "Minimum Number of Warmup Days": 6,
        })
        w.add("Timestep", {"Number of Timesteps per Hour": 4})
        w.add("GlobalGeometryRules", {"Starting Vertex Position": "UpperLeftCorner",
                                      "Vertex Entry Direction": "Counterclockwise", "Coordinate System": "World"})
        if location:
            w.add("Site:Location", {"Name": location["name"], "Latitude": location["latitude"],
                                    "Longitude": location["longitude"], "Time Zone": location["time_zone"],
                                    "Elevation": location["elevation"]})
        w.add("RunPeriod", {"Name": "Annual", "Begin Month": 1, "Begin Day of Month": 1, "End Month": 12,
                            "End Day of Month": 31, "Use Weather File Holidays and Special Days": True,
                            "Use Weather File Daylight Saving Period": True, "Apply Weekend Holiday Rule": False,
                            "Use Weather File Rain Indicators": True, "Use Weather File Snow Indicators": True})
        for period in ("SummerExtreme", "WinterExtreme"):
            w.add("SizingPeriod:WeatherFileConditionType", {"Name": f"Sizing {period}", "Period Selection": period})
        w.add("Site:WaterMainsTemperature", {"Calculation Method": "CorrelationFromWeatherFile"})

    # -- constructions --------------------------------------------------------

    def _constructions(self):
        p, w = self.p, self.w
        mat = lambda name, t, k, rho, cp, sa=0.7: w.add("Material", {  # noqa: E731
            "Name": name, "Roughness": "MediumRough", "Thickness": t, "Conductivity": k, "Density": rho,
            "Specific Heat": cp, "Thermal Absorptance": 0.9, "Solar Absorptance": sa, "Visible Absorptance": sa})
        mat("Stucco 25mm", 0.0254, 0.6918, 1858, 837)
        mat("Gypsum 13mm", 0.0127, 0.16, 784.9, 830, 0.4)
        mat("CMU 200mm", 0.2032, 0.5104, 800, 920)
        mat("Roof membrane", 0.0095, 0.16, 1121.3, 1460, p.roof_solar_absorptance)
        mat("Metal deck", 0.0015, 45.006, 7680, 418.4)
        mat("Concrete 100mm", 0.1016, 1.311, 2240, 836.8)
        mat("Interior floor 100mm", 0.1016, 1.311, 2240, 836.8)

        # Insulation is sized so the assembly (without air films) hits the target R.
        mass = "mass" in p.wall_construction.lower()
        wall_r_other = 0.0254 / 0.6918 + 0.0127 / 0.16 + (0.2032 / 0.5104 if mass else 0.0)
        w.add("Material:NoMass", {"Name": "Wall insulation", "Roughness": "MediumRough",
                                  "Thermal Resistance": max(p.wall_r - wall_r_other, 0.01),
                                  "Thermal Absorptance": 0.9, "Solar Absorptance": 0.7, "Visible Absorptance": 0.7})
        w.add_raw("Construction", ["Exterior wall", "Stucco 25mm", *(["CMU 200mm"] if mass else []),
                                   "Wall insulation", "Gypsum 13mm"])

        roof_r_other = 0.0095 / 0.16 + 0.0015 / 45.006
        roof_ins = max(p.roof_r - roof_r_other, 0.01)
        w.add("Material:NoMass", {"Name": "Roof insulation", "Roughness": "MediumRough",
                                  "Thermal Resistance": roof_ins, "Thermal Absorptance": 0.9,
                                  "Solar Absorptance": 0.7, "Visible Absorptance": 0.7})
        w.add_raw("Construction", ["Roof", "Roof membrane", "Roof insulation", "Metal deck"])

        w.add("WindowMaterial:SimpleGlazingSystem", {"Name": "Glazing", "U-Factor": p.window_u,
                                                     "Solar Heat Gain Coefficient": p.window_shgc,
                                                     "Visible Transmittance": p.window_vt})
        w.add_raw("Construction", ["Window", "Glazing"])
        w.add_raw("Construction", ["Interior wall", "Gypsum 13mm", "Gypsum 13mm"])
        w.add_raw("Construction", ["Interior floor", "Interior floor 100mm"])
        w.add_raw("Construction", ["Interior ceiling", "Interior floor 100mm"])
        w.add_raw("Construction", ["Party wall", "CMU 200mm"])

    # -- schedules ------------------------------------------------------------

    def _schedules(self):
        w = self.w
        w.add_raw("ScheduleTypeLimits", ["Fraction", 0, 1, "Continuous"])
        w.add_raw("ScheduleTypeLimits", ["Any Number"])
        w.add_raw("Schedule:Constant", ["ALWAYS_ON", "Fraction", 1])
        w.add_raw("Schedule:Constant", ["UBEM_ACTIVITY", "Any Number", 120])
        w.add_raw("Schedule:Constant", ["UBEM_WATER_HEATER_SETPOINT", "Any Number", self.p.water_heater_setpoint_c])
        w.add_raw("Schedule:Constant", ["UBEM_WATER_HEATER_AMBIENT", "Any Number", 22])
        used = {n for n in self.p.schedule_roles.values() if n}
        used |= {e["schedule"] for e in self.p.electric_equipment + self.p.gas_equipment}
        if self.p.hot_water_schedule:
            used.add(self.p.hot_water_schedule)
        if self.p.exterior_lighting_schedule:
            used.add(self.p.exterior_lighting_schedule)
        self._schedule_names = {}
        for name in sorted(used - {"ALWAYS_ON"}):
            self._schedule_names[name] = self._compact(name, self.p.schedules[name])
        self._schedule_names["ALWAYS_ON"] = "ALWAYS_ON"

    def _compact(self, name: str, sched: Dict) -> str:
        limits = "Fraction" if str(sched.get("type", "")).lower() in ("fraction", "on/off") else "Any Number"
        safe = "UBEM_" + "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        fields: List[object] = [safe, limits]
        for thru, days in expand_rules(sched["rules"]):
            fields.append(f"Through: {thru}")
            # Group day types with identical profiles.
            groups: Dict[tuple, List[str]] = {}
            for d in DAY_TYPES:
                groups.setdefault(tuple(days[d]), []).append(d)
            items = list(groups.items())
            for i, (values, day_list) in enumerate(items):
                if len(items) == 1:
                    label = "AllDays"
                elif i == len(items) - 1:
                    label = "AllOtherDays"
                else:
                    label = " ".join("Holidays" if d == "Holiday" else d for d in day_list)
                fields.append(f"For: {label}")
                hour = 0
                while hour < 24:
                    end = hour
                    while end + 1 < 24 and values[end + 1] == values[hour]:
                        end += 1
                    fields += [f"Until: {end + 1:02d}:00", values[hour]]
                    hour = end + 1
        self.w.add_raw("Schedule:Compact", fields)
        return safe

    def sched(self, role_or_name: Optional[str]) -> str:
        name = self.p.schedule_roles.get(role_or_name, role_or_name) if role_or_name else None
        return self._schedule_names.get(name, "ALWAYS_ON") if name else "ALWAYS_ON"

    # -- geometry -------------------------------------------------------------

    def _geometry(self, zones: List[ZoneGeom], footprint: Polygon, party: Dict[tuple, float]) -> Dict[str, float]:
        w, p = self.w, self.p
        footprint_edges = {_key(a, b) for a, b in _edges(footprint)}
        wwr_factor = math.sqrt(max(min(p.window_to_wall_ratio, 0.9), 0.0))
        ext_wall_area: Dict[str, float] = {}

        # Index zone edges so interzone walls can be paired: (z0, edge) -> wall name.
        wall_by_edge: Dict[tuple, str] = {}
        for z in zones:
            for k, (a, b) in enumerate(_edges(z.polygon)):
                wall_by_edge[(z.z0,) + _key(a, b)] = f"{z.name}_Wall{k + 1}"

        for z in zones:
            w.add("Zone", {"Name": z.name, "Multiplier": z.multiplier, "Ceiling Height": z.z1 - z.z0})
            ext_wall_area[z.name] = 0.0
            # floor
            floor_vertices = [(x, y, z.z0) for x, y in reversed(list(z.polygon.exterior.coords)[:-1])]
            if z.is_ground:
                exposed = sum(math.dist(a, b) for a, b in _edges(z.polygon) if _key(a, b) in footprint_edges)
                cname = f"Slab {z.name}"
                w.add("Construction:FfactorGroundFloor", {"Name": cname, "F-Factor": SLAB_F_FACTOR,
                                                          "Area": z.polygon.area, "PerimeterExposed": exposed})
                self._surface(f"{z.name}_Floor", "Floor", cname, z.name, "GroundFCfactorMethod", "", floor_vertices,
                              sun=False)
            else:
                self._surface(f"{z.name}_Floor", "Floor", "Interior floor", z.name, "Adiabatic", "", floor_vertices,
                              sun=False)
            # roof / ceiling
            top_vertices = [(x, y, z.z1) for x, y in list(z.polygon.exterior.coords)[:-1]]
            if z.is_top:
                self._surface(f"{z.name}_Roof", "Roof", "Roof", z.name, "Outdoors", "", top_vertices, sun=True)
            else:
                self._surface(f"{z.name}_Ceiling", "Ceiling", "Interior ceiling", z.name, "Adiabatic", "",
                              top_vertices, sun=False)
            # walls
            for k, (a, b) in enumerate(_edges(z.polygon)):
                verts = _wall_vertices(a, b, z.z0, z.z1)
                sname = f"{z.name}_Wall{k + 1}"
                key = _key(a, b)
                if key in footprint_edges:
                    if party.get(key, 0.0) >= z.z1 - 0.01:
                        self._surface(sname, "Wall", "Party wall", z.name, "Adiabatic", "", verts, sun=False)
                        continue
                    self._surface(sname, "Wall", "Exterior wall", z.name, "Outdoors", "", verts, sun=True)
                    width = math.dist(a, b)
                    ext_wall_area[z.name] += width * (z.z1 - z.z0)
                    if wwr_factor > 0 and width >= MIN_WINDOW_WALL_WIDTH_M:
                        w.add("FenestrationSurface:Detailed", {
                            "Name": f"{sname}_Win", "Surface Type": "Window", "Construction Name": "Window",
                            "Building Surface Name": sname, "Number of Vertices": 4,
                        }, extensible=_flat(_scaled(verts, wwr_factor)))
                        self.info.windows += 1
                else:
                    other = wall_by_edge.get((z.z0,) + _key(b, a))
                    if other is None:
                        self._surface(sname, "Wall", "Interior wall", z.name, "Adiabatic", "", verts, sun=False)
                    else:
                        self._surface(sname, "Wall", "Interior wall", z.name, "Surface", other, verts, sun=False)
        return ext_wall_area

    def _surface(self, name, stype, construction, zone, boundary, boundary_obj, vertices, sun):
        self.w.add("BuildingSurface:Detailed", {
            "Name": name, "Surface Type": stype, "Construction Name": construction, "Zone Name": zone,
            "Outside Boundary Condition": boundary, "Outside Boundary Condition Object": boundary_obj,
            "Sun Exposure": "SunExposed" if sun else "NoSun", "Wind Exposure": "WindExposed" if sun else "NoWind",
            "View Factor to Ground": "autocalculate", "Number of Vertices": len(vertices),
        }, extensible=_flat(vertices))

    def _shading(self, footprint: Polygon, neighbors: Sequence[Neighbor]):
        near = sorted(
            (nb for nb in neighbors if 0 < nb.footprint.distance(footprint) <= SHADING_RADIUS_M),
            key=lambda nb: nb.footprint.distance(footprint))[:MAX_SHADING_NEIGHBORS]
        # Candidate shading walls from simplified neighbor outlines, nearest
        # first; walls touching this building (party walls) cast no shadow on it.
        band = footprint.exterior.buffer(PARTY_WALL_TOLERANCE_M)
        walls = []
        for i, nb in enumerate(near):
            poly = _simplified(nb.footprint, 1.0) or orient(nb.footprint, 1.0)
            for k, (a, b) in enumerate(_edges(poly)):
                edge = LineString([a, b])
                if edge.length < MIN_SHADING_EDGE_M or edge.within(band):
                    continue
                walls.append((edge.distance(footprint), f"Neighbor{i + 1}_Wall{k + 1}", a, b, nb.height_m))
        walls.sort(key=lambda x: x[0])
        if len(walls) > MAX_SHADING_SURFACES:
            self.info.notes.append(f"shading limited to the nearest {MAX_SHADING_SURFACES} of {len(walls)} neighbor walls")
        for _, name, a, b, h in walls[:MAX_SHADING_SURFACES]:
            self.w.add("Shading:Building:Detailed", {"Name": name, "Number of Vertices": 4},
                       extensible=_flat(_wall_vertices(a, b, 0.0, h)))
            self.info.shading_surfaces += 1

    # -- loads ----------------------------------------------------------------

    def _loads(self, zones: List[ZoneGeom], building, ext_wall_area: Dict[str, float]):
        w, p = self.w, self.p
        total_volume = sum(z.polygon.area * (z.z1 - z.z0) * z.multiplier for z in zones)
        total_ext = sum(ext_wall_area[z.name] * z.multiplier for z in zones)
        occupancy = self.sched("occupancy")
        activity = self.sched("activity") if p.schedule_roles.get("activity") else "UBEM_ACTIVITY"
        stories = min(max(building.num_floors, 1), 3)
        for z in zones:
            w.add("People", {"Name": f"{z.name} People", "Zone or ZoneList or Space or SpaceList Name": z.name,
                             "Number of People Schedule Name": occupancy,
                             "Number of People Calculation Method": "People/Area",
                             "People per Floor Area": p.people_per_m2, "Fraction Radiant": 0.3,
                             "Sensible Heat Fraction": "autocalculate", "Activity Level Schedule Name": activity})
            w.add("Lights", {"Name": f"{z.name} Lights", "Zone or ZoneList or Space or SpaceList Name": z.name,
                             "Schedule Name": self.sched("lighting"), "Design Level Calculation Method": "Watts/Area",
                             "Watts per Floor Area": p.lighting_w_m2, "Return Air Fraction": 0.0,
                             "Fraction Radiant": 0.7, "Fraction Visible": 0.2, "End-Use Subcategory": "General"})
            for e in p.electric_equipment:
                w.add("ElectricEquipment", {
                    "Name": f"{z.name} {e['name']}", "Zone or ZoneList or Space or SpaceList Name": z.name,
                    "Schedule Name": self.sched(e["schedule"]), "Design Level Calculation Method": "Watts/Area",
                    "Watts per Floor Area": e["w_per_m2"], "Fraction Latent": e.get("fraction_latent") or 0.0,
                    "Fraction Radiant": e.get("fraction_radiant") or 0.0, "Fraction Lost": e.get("fraction_lost") or 0.0,
                    "End-Use Subcategory": e["name"]})
            for e in p.gas_equipment:
                w.add("GasEquipment", {
                    "Name": f"{z.name} {e['name']} gas", "Zone or ZoneList or Space or SpaceList Name": z.name,
                    "Schedule Name": self.sched(e["schedule"]), "Design Level Calculation Method": "Watts/Area",
                    "Power per Floor Area": e["w_per_m2"], "Fraction Latent": e.get("fraction_latent") or 0.0,
                    "Fraction Radiant": e.get("fraction_radiant") or 0.0, "Fraction Lost": e.get("fraction_lost") or 0.0,
                    "End-Use Subcategory": e["name"]})
            if p.ela_cm2_per_m2_envelope is not None:
                envelope = ext_wall_area[z.name] + (z.polygon.area if z.is_top else 0.0)
                if envelope > 0:
                    w.add("ZoneInfiltration:EffectiveLeakageArea", {
                        "Name": f"{z.name} Infiltration", "Zone or Space Name": z.name, "Schedule Name": "ALWAYS_ON",
                        "Effective Air Leakage Area": p.ela_cm2_per_m2_envelope * envelope,
                        "Stack Coefficient": _STACK_COEF[stories], "Wind Coefficient": _WIND_COEF[stories]})
            elif p.infiltration_ach and total_ext > 0 and ext_wall_area[z.name] > 0:
                # Building infiltration (ACH x volume) distributed by exterior wall area.
                per_wall = p.infiltration_ach * total_volume / 3600.0 / total_ext
                w.add("ZoneInfiltration:DesignFlowRate", {
                    "Name": f"{z.name} Infiltration", "Zone or ZoneList or Space or SpaceList Name": z.name,
                    "Schedule Name": self.sched("infiltration"), "Design Flow Rate Calculation Method": "Flow/ExteriorWallArea",
                    "Flow Rate per Exterior Surface Area": per_wall, "Constant Term Coefficient": 1,
                    "Temperature Term Coefficient": 0, "Velocity Term Coefficient": 0,
                    "Velocity Squared Term Coefficient": 0})
        gfa = building.gross_floor_area_m2
        if p.exterior_lighting_w_per_m2 > 0:
            astronomical = p.exterior_lighting_schedule is None
            w.add("Exterior:Lights", {
                "Name": "Exterior lighting",
                "Schedule Name": "ALWAYS_ON" if astronomical else self.sched(p.exterior_lighting_schedule),
                "Design Level": p.exterior_lighting_w_per_m2 * gfa,
                "Control Option": "AstronomicalClock" if astronomical else "ScheduleNameOnly",
                "End-Use Subcategory": "Exterior lighting"})

    def _hot_water(self, building):
        p = self.p
        peak = p.hot_water_m3_s_per_m2 * building.gross_floor_area_m2
        if peak <= 0 or not p.hot_water_schedule:
            return
        delta_t = p.water_heater_setpoint_c - 10.0
        self.w.add("WaterHeater:Mixed", {
            "Name": "Water heater", "Tank Volume": max(0.15, peak * 1800.0),
            "Setpoint Temperature Schedule Name": "UBEM_WATER_HEATER_SETPOINT", "Deadband Temperature Difference": 2.0,
            "Maximum Temperature Limit": 82.0, "Heater Control Type": "Cycle",
            "Heater Maximum Capacity": max(11700.0, 0.5 * peak * 1000.0 * 4186.0 * delta_t),
            "Heater Fuel Type": p.water_heater_fuel, "Heater Thermal Efficiency": p.water_heater_efficiency,
            "Ambient Temperature Indicator": "Schedule", "Ambient Temperature Schedule Name": "UBEM_WATER_HEATER_AMBIENT",
            "Off Cycle Loss Coefficient to Ambient Temperature": 2.0, "On Cycle Loss Coefficient to Ambient Temperature": 2.0,
            "Peak Use Flow Rate": peak, "Use Flow Rate Fraction Schedule Name": self.sched(p.hot_water_schedule),
            "End-Use Subcategory": "Service hot water"})

    # -- HVAC -----------------------------------------------------------------

    def _hvac(self, zones: List[ZoneGeom], floors):
        w, p = self.w, self.p
        w.add("HVACTemplate:Thermostat", {"Name": "Thermostat",
                                           "Heating Setpoint Schedule Name": self.sched("heating_setpoint"),
                                           "Cooling Setpoint Schedule Name": self.sched("cooling_setpoint")})
        oa_area = (p.outdoor_air_l_s_m2 or 0.0) / 1000.0
        availability = self.sched("hvac_operation") if p.schedule_roles.get("hvac_operation") else "ALWAYS_ON"
        economizer = "DifferentialDryBulb" if p.economizer else "NoEconomizer"
        gas = p.heating_fuel == "NaturalGas"

        if self.hvac == "ideal":
            for z in zones:
                w.add("HVACTemplate:Zone:IdealLoadsAirSystem", {
                    "Zone Name": z.name, "Template Thermostat Name": "Thermostat",
                    "System Availability Schedule Name": availability,
                    "Outdoor Air Method": "Flow/Area", "Outdoor Air Flow Rate per Zone Floor Area": oa_area})
            return

        if p.hvac_system == "psz" and p.heat_pump_heating_cop:
            for z in zones:
                sys_name = f"PSZ-HP {z.name}"
                w.add("HVACTemplate:System:UnitaryHeatPump:AirToAir", {
                    "Name": sys_name, "System Availability Schedule Name": availability,
                    "Control Zone or Thermostat Location Name": z.name,
                    "Supply Fan Operating Mode Schedule Name": None if p.sector == "residential" else availability,
                    "Supply Fan Total Efficiency": 0.6, "Supply Fan Delta Pressure": 622.0,
                    "Supply Fan Motor Efficiency": 0.85, "Cooling Coil Type": "SingleSpeedDX",
                    "Cooling Coil Gross Rated COP": p.cooling_cop,
                    "Heat Pump Heating Coil Type": "SingleSpeedDXHeatPump",
                    "Heat Pump Heating Coil Rated COP": p.heat_pump_heating_cop,
                    "Heat Pump Heating Minimum Outdoor Dry-Bulb Temperature": -12.0,
                    "Supplemental Heating Coil Type": "Electric",
                    "Maximum Outdoor Air Flow Rate": "autosize", "Minimum Outdoor Air Flow Rate": "autosize",
                    "Economizer Type": economizer, "Night Cycle Control": "CycleOnAny"})
                w.add("HVACTemplate:Zone:Unitary", {
                    "Zone Name": z.name, "Template Unitary System Name": sys_name,
                    "Template Thermostat Name": "Thermostat", "Outdoor Air Method": "Flow/Area",
                    "Outdoor Air Flow Rate per Zone Floor Area": oa_area})
        elif p.hvac_system == "ptac" and p.heat_pump_heating_cop:
            for z in zones:
                w.add("HVACTemplate:Zone:PTHP", {
                    "Zone Name": z.name, "Template Thermostat Name": "Thermostat",
                    "Outdoor Air Method": "Flow/Area", "Outdoor Air Flow Rate per Zone Floor Area": oa_area,
                    "System Availability Schedule Name": availability, "Supply Fan Operating Mode Schedule Name": availability,
                    "Cooling Coil Type": "SingleSpeedDX", "Cooling Coil Gross Rated COP": p.cooling_cop,
                    "Heat Pump Heating Coil Type": "SingleSpeedDXHeatPump",
                    "Heat Pump Heating Coil Gross Rated COP": p.heat_pump_heating_cop,
                    "Heat Pump Heating Minimum Outdoor Dry-Bulb Temperature": -12.0,
                    "Supplemental Heating Coil Type": "Electric"})
        elif p.hvac_system == "psz":
            for z in zones:
                sys_name = f"PSZ {z.name}"
                w.add("HVACTemplate:System:Unitary", {
                    "Name": sys_name, "System Availability Schedule Name": availability,
                    "Control Zone or Thermostat Location Name": z.name, "Supply Fan Maximum Flow Rate": "autosize",
                    # Commercial reference systems run the fan whenever the system is on (to
                    # deliver ventilation air); residential furnaces cycle the fan.
                    "Supply Fan Operating Mode Schedule Name": None if p.sector == "residential" else availability,
                    "Supply Fan Total Efficiency": 0.6, "Supply Fan Delta Pressure": 622.0,
                    "Supply Fan Motor Efficiency": 0.85, "Cooling Coil Type": "SingleSpeedDX",
                    "Cooling Coil Gross Rated Total Capacity": "autosize",
                    "Cooling Coil Gross Rated Sensible Heat Ratio": "autosize",
                    "Cooling Coil Gross Rated COP": p.cooling_cop,
                    "Heating Coil Type": "Gas" if gas else "Electric", "Heating Coil Capacity": "autosize",
                    "Gas Heating Coil Efficiency": p.heating_efficiency if gas else None,
                    "Maximum Outdoor Air Flow Rate": "autosize", "Minimum Outdoor Air Flow Rate": "autosize",
                    "Economizer Type": economizer, "Night Cycle Control": "CycleOnAny"})
                w.add("HVACTemplate:Zone:Unitary", {
                    "Zone Name": z.name, "Template Unitary System Name": sys_name,
                    "Template Thermostat Name": "Thermostat", "Outdoor Air Method": "Flow/Area",
                    "Outdoor Air Flow Rate per Zone Floor Area": oa_area})
        elif p.hvac_system == "ptac":
            for z in zones:
                w.add("HVACTemplate:Zone:PTAC", {
                    "Zone Name": z.name, "Template Thermostat Name": "Thermostat",
                    "Outdoor Air Method": "Flow/Area", "Outdoor Air Flow Rate per Zone Floor Area": oa_area,
                    "System Availability Schedule Name": availability, "Supply Fan Operating Mode Schedule Name": availability,
                    "Cooling Coil Type": "SingleSpeedDX", "Cooling Coil Gross Rated Cooling COP": p.cooling_cop,
                    "Heating Coil Type": "Gas" if gas else "Electric",
                    "Gas Heating Coil Efficiency": p.heating_efficiency if gas else None})
        else:
            if p.hvac_system == "vav" or p.reheat_coil == "HotWater" or p.ahu_heating_coil == "HotWater":
                w.add("HVACTemplate:Plant:HotWaterLoop", {"Name": "Hot water loop", "Pump Control Type": "Intermittent"})
                w.add("HVACTemplate:Plant:Boiler", {"Name": "Boiler", "Boiler Type": "HotWaterBoiler",
                                                    "Capacity": "autosize", "Efficiency": p.heating_efficiency,
                                                    "Fuel Type": "NaturalGas" if gas else "Electricity",
                                                    "Template Plant Loop Type": "HotWater"})
            if p.hvac_system == "vav":
                w.add("HVACTemplate:Plant:ChilledWaterLoop", {"Name": "Chilled water loop",
                                                               "Pump Control Type": "Intermittent"})
                w.add("HVACTemplate:Plant:Chiller", {"Name": "Chiller", "Chiller Type": "ElectricCentrifugalChiller",
                                                     "Capacity": "autosize", "Nominal COP": p.cooling_cop,
                                                     "Condenser Type": "WaterCooled"})
                w.add("HVACTemplate:Plant:Tower", {"Name": "Cooling tower", "Tower Type": "SingleSpeed",
                                                   "High Speed Nominal Capacity": "autosize",
                                                   "High Speed Fan Power": "autosize"})
            for story, _mult, _, _ in floors:
                sys_name = f"VAV F{story + 1}"
                common = {"Name": sys_name, "System Availability Schedule Name": availability,
                          "Supply Fan Maximum Flow Rate": "autosize", "Supply Fan Minimum Flow Rate": "autosize",
                          "Supply Fan Total Efficiency": 0.6, "Supply Fan Delta Pressure": 1000.0,
                          "Supply Fan Motor Efficiency": 0.9, "Maximum Outdoor Air Flow Rate": "autosize",
                          "Minimum Outdoor Air Flow Rate": "autosize", "Economizer Type": economizer,
                          "Night Cycle Control": "CycleOnAny"}
                if p.hvac_system == "pvav":
                    w.add("HVACTemplate:System:PackagedVAV", {
                        **common, "Cooling Coil Type": "TwoSpeedDX", "Cooling Coil Gross Rated Total Capacity": "autosize",
                        "Cooling Coil Gross Rated Sensible Heat Ratio": "autosize",
                        "Cooling Coil Gross Rated COP": p.cooling_cop, "Heating Coil Type": p.ahu_heating_coil or "None",
                        "Gas Heating Coil Efficiency": p.heating_efficiency if p.ahu_heating_coil == "Gas" else None})
                else:
                    w.add("HVACTemplate:System:VAV", {**common, "Cooling Coil Type": "ChilledWater",
                                                     "Heating Coil Type": "HotWater"})
                for z in zones:
                    if z.name.startswith(f"F{story + 1}_"):
                        w.add("HVACTemplate:Zone:VAV", {
                            "Zone Name": z.name, "Template VAV System Name": sys_name,
                            "Template Thermostat Name": "Thermostat", "Zone Minimum Air Flow Input Method": "Constant",
                            "Constant Minimum Air Flow Fraction": 0.3, "Outdoor Air Method": "Flow/Area",
                            "Outdoor Air Flow Rate per Zone Floor Area": oa_area,
                            "Reheat Coil Type": p.reheat_coil or "HotWater"})

    # -- outputs ----------------------------------------------------------------

    def _outputs(self):
        w = self.w
        w.add("Output:SQLite", {"Option Type": "SimpleAndTabular"})
        w.add_raw("OutputControl:Table:Style", ["CommaAndHTML", "JtoKWH"])
        w.add_raw("Output:Table:SummaryReports", ["AllSummary"])
        for meter in ("Electricity:Facility", "NaturalGas:Facility"):
            w.add_raw("Output:Meter", [meter, "Hourly"])


# --------------------------------------------------------------------------- #
# EPW location
# --------------------------------------------------------------------------- #

def epw_location(epw_path: str) -> Dict[str, object]:
    with open(epw_path, encoding="latin-1") as f:
        header = f.readline().strip().split(",")
    if not header or header[0].upper() != "LOCATION" or len(header) < 10:
        raise ValueError(f"{epw_path} does not start with an EPW LOCATION record")
    return {
        "name": " ".join(x for x in header[1:4] if x).strip() or "Site",
        "latitude": float(header[6]), "longitude": float(header[7]),
        "time_zone": float(header[8]), "elevation": float(header[9]),
    }
