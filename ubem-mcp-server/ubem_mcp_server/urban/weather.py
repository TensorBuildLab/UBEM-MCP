"""
Weather files and climate zones for a site.

Weather stations come from the Climate.OneBuilding.Org TMYx station indexes
(Lawrie & Crawley, https://climate.onebuilding.org), which are downloaded on
first use and cached; they are not redistributed with UBEM-MCP. A station's
EPW/STAT/DDY files are downloaded from the same site.

The ASHRAE 169 climate zone of a weather file is read from its .stat file,
where Climate.OneBuilding.Org records the zone computed from the ASHRAE
Handbook design conditions. Without a .stat file the zone is estimated from
the EPW itself with the ASHRAE 169 thermal and moisture criteria; that
estimate uses one typical year rather than a long-term record and is labelled
as such.
"""

import io
import json
import logging
import math
import re
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .library import CLIMATE_ZONES

logger = logging.getLogger(__name__)

ONEBUILDING_HOST = "climate.onebuilding.org"
INDEX_URL = "https://climate.onebuilding.org/sources/{region}_TMYx_EPW_Processing_locations.xlsx"
INDEX_REGIONS = (
    "Region1_Africa", "Region2_Asia", "Region2_Region6_Russia", "Region3_South_America",
    "Region4_USA", "Region4_Canada", "Region4_NA_CA_Caribbean", "Region5_Southwest_Pacific",
    "Region6_Europe", "Region7_Antarctica",
)
USER_AGENT = "UBEM-MCP (+https://github.com/TensorBuildLab/UBEM-MCP)"
WEATHER_EXTENSIONS = (".epw", ".stat", ".ddy")

Fetch = Callable[[str], bytes]


def http_get(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https URLs built here)
        return resp.read()


# --------------------------------------------------------------------------- #
# Stations
# --------------------------------------------------------------------------- #

@dataclass
class Station:
    name: str
    country: str
    state: str
    wmo: str
    latitude: float
    longitude: float
    time_zone: float
    elevation_m: float
    base_url: str  # file URL without "_<dataset>.zip"
    datasets: List[str] = field(default_factory=list)

    def url(self, dataset: str) -> str:
        if dataset not in self.datasets:
            raise ValueError(f"Station {self.name!r} has no dataset {dataset!r}; available: {self.datasets}")
        return f"{self.base_url}_{dataset}.zip"

    @property
    def file_stem(self) -> str:
        return self.base_url.rsplit("/", 1)[-1]

    def default_dataset(self) -> str:
        """The most recent 15-year TMYx period, else the full-record TMYx."""
        periods = []
        for d in self.datasets:
            m = re.fullmatch(r"TMYx\.(\d{4})-(\d{4})", d)
            if m:
                periods.append((int(m.group(2)), int(m.group(1)), d))
        if periods:
            return max(periods)[2]
        return "TMYx" if "TMYx" in self.datasets else sorted(self.datasets)[0]


_FILE_RE = re.compile(r"^(https://climate\.onebuilding\.org/[^\s]+)_([^_/]+)\.zip$")


def stations_from_rows(rows: Iterable[Sequence[Any]]) -> List[Station]:
    """Group index rows (Country, State, City/Station, WMO, Source Data, Latitude,
    Longitude, Time Zone, Elevation, URL) into one Station per file stem."""
    by_base: Dict[str, Station] = {}
    for row in rows:
        if not row or len(row) < 10 or not row[9]:
            continue
        m = _FILE_RE.match(str(row[9]).strip())
        if not m:
            continue
        base, dataset = m.groups()
        try:
            lat, lon = float(row[5]), float(row[6])
        except (TypeError, ValueError):
            continue
        st = by_base.get(base)
        if st is None:
            st = by_base[base] = Station(
                name=str(row[2] or "").strip(), country=str(row[0] or "").strip(),
                state=str(row[1] or "").strip(), wmo=str(row[3] or "").strip(),
                latitude=lat, longitude=lon, time_zone=_num(row[7]), elevation_m=_num(row[8]),
                base_url=base,
            )
        elif str(row[4] or "").startswith("SRC"):
            # "Custom" rows can carry older coordinates; prefer the source-data rows.
            st.latitude, st.longitude = lat, lon
        if dataset not in st.datasets:
            st.datasets.append(dataset)
    return list(by_base.values())


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


class StationIndex:
    def __init__(self, stations: List[Station], retrieved_at: Optional[str] = None):
        self.stations = stations
        self.retrieved_at = retrieved_at

    def __len__(self) -> int:
        return len(self.stations)

    def nearest(self, latitude: float, longitude: float, n: int = 5) -> List[Tuple[Station, float]]:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError(f"Invalid coordinates ({latitude}, {longitude})")
        scored = [(st, haversine_km(latitude, longitude, st.latitude, st.longitude)) for st in self.stations]
        scored.sort(key=lambda x: x[1])
        return scored[:max(1, n)]

    @classmethod
    def load(cls, cache_dir: str, refresh: bool = False, fetch: Fetch = http_get,
             regions: Sequence[str] = INDEX_REGIONS) -> "StationIndex":
        """Load the cached index, downloading the regional indexes if needed."""
        cache = Path(cache_dir) / "stations.json"
        if cache.exists() and not refresh:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
            return cls([Station(**s) for s in data["stations"]], data.get("retrieved_at"))

        import openpyxl

        rows: List[Sequence[Any]] = []
        for region in regions:
            url = INDEX_URL.format(region=region)
            logger.info("Downloading weather station index %s", url)
            wb = openpyxl.load_workbook(io.BytesIO(fetch(url)), read_only=True, data_only=True)
            for ws in wb.worksheets:
                rows.extend(ws.iter_rows(min_row=2, values_only=True))
            wb.close()
        stations = stations_from_rows(rows)
        if not stations:
            raise RuntimeError("The weather station index downloaded but contained no stations")
        retrieved_at = datetime.now().isoformat(timespec="seconds")
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"source": "https://climate.onebuilding.org", "retrieved_at": retrieved_at,
                       "regions": list(regions), "stations": [asdict(s) for s in stations]}, f)
        return cls(stations, retrieved_at)


def download_weather(station: Station, dataset: Optional[str], dest_root: str,
                     fetch: Fetch = http_get) -> Dict[str, Any]:
    """Download and unpack a station's weather files into
    <dest_root>/<station file stem>_<dataset>/ (reused if already there)."""
    dataset = dataset or station.default_dataset()
    url = station.url(dataset)
    if not url.startswith(f"https://{ONEBUILDING_HOST}/"):
        raise ValueError(f"Refusing to download weather from {url!r}")
    dest = Path(dest_root) / f"{station.file_stem}_{dataset}"
    files = _weather_files(dest)
    if "epw" not in files:
        logger.info("Downloading weather file %s", url)
        data = fetch(url)
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.infolist():
                name = Path(member.filename).name  # never trust paths inside the archive
                if member.is_dir() or not name.lower().endswith(WEATHER_EXTENSIONS):
                    continue
                (dest / name).write_bytes(zf.read(member))
        files = _weather_files(dest)
        if "epw" not in files:
            raise RuntimeError(f"{url} did not contain an EPW file")
    return {"directory": str(dest), "url": url, "dataset": dataset, **files}


def _weather_files(directory: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if directory.is_dir():
        for p in sorted(directory.iterdir()):
            ext = p.suffix.lower().lstrip(".")
            if "." + ext in WEATHER_EXTENSIONS and ext not in out:
                out[ext] = str(p)
    return out


# --------------------------------------------------------------------------- #
# Climate zones
# --------------------------------------------------------------------------- #

_STAT_ZONE_RES = (
    re.compile(r'Climate Zone "([0-8][ABC]?)" \(ASHRAE Standard 169'),
    re.compile(r'Climate type "([0-8][ABC]?)" \(ASHRAE Standard 196'),  # older .stat files
)
_STAT_KOPPEN_RE = re.compile(r'Climate type "([A-Za-z]{1,3})" \(K.ppen')


def climate_zone_from_stat(stat_path: str) -> Optional[str]:
    text = Path(stat_path).read_text(encoding="latin-1")
    for rx in _STAT_ZONE_RES:
        m = rx.search(text)
        if m:
            return m.group(1).upper()
    return None


def koppen_from_stat(stat_path: str) -> Optional[str]:
    m = _STAT_KOPPEN_RE.search(Path(stat_path).read_text(encoding="latin-1"))
    return m.group(1) if m else None


def epw_climate_statistics(epw_path: str) -> Dict[str, Any]:
    """Degree days, monthly mean temperatures and precipitation of an EPW."""
    daily: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    monthly: Dict[int, List[float]] = defaultdict(list)
    monthly_precip: Dict[int, float] = defaultdict(float)
    precip_hours = 0
    with open(epw_path, encoding="latin-1") as f:
        for i, line in enumerate(f):
            if i < 8:
                continue
            r = line.rstrip("\n").split(",")
            if len(r) < 34:
                continue
            month, day, t = int(r[1]), int(r[2]), float(r[6])
            daily[(month, day)].append(t)
            monthly[month].append(t)
            try:
                p = float(r[33])
            except ValueError:
                p = 999.0
            if 0 <= p < 999:
                monthly_precip[month] += p
                precip_hours += 1
    day_means = [sum(v) / len(v) for v in daily.values()]
    hours = sum(len(v) for v in monthly.values())
    return {
        "hdd18": sum(max(0.0, 18.0 - t) for t in day_means),
        "cdd10": sum(max(0.0, t - 10.0) for t in day_means),
        "annual_mean_c": sum(sum(v) for v in monthly.values()) / max(1, hours),
        "monthly_mean_c": {m: sum(v) / len(v) for m, v in sorted(monthly.items())},
        "monthly_precip_mm": dict(sorted(monthly_precip.items())),
        "precip_hours": precip_hours,
        "hours": hours,
    }


def thermal_zone(hdd18: float, cdd10: float) -> int:
    """ASHRAE 169 thermal climate zone number from degree days (SI)."""
    if cdd10 > 6000:
        return 0
    if cdd10 > 5000:
        return 1
    if cdd10 > 3500:
        return 2
    if cdd10 > 2500 or hdd18 <= 2000:
        return 3
    if hdd18 <= 3000:
        return 4
    if hdd18 <= 4000:
        return 5
    if hdd18 <= 5000:
        return 6
    if hdd18 <= 7000:
        return 7
    return 8


def climate_zone_from_epw(epw_path: str, koppen: Optional[str] = None) -> Tuple[str, List[str]]:
    """Estimate the ASHRAE 169 climate zone from one weather year.

    Returns (zone, notes). Precipitation in typical-year files is often
    missing or unrepresentative, so it is used only when every hour has a
    value; otherwise a Koppen class (from the .stat file) decides dry vs.
    humid, and failing that the zone is assumed humid (A).
    """
    s = epw_climate_statistics(epw_path)
    notes = [f"estimated from one weather year (HDD18={s['hdd18']:.0f}, CDD10={s['cdd10']:.0f}); "
             "ASHRAE 169 uses long-term records"]
    temps = s["monthly_mean_c"]
    precip_ok = s["precip_hours"] >= s["hours"] > 0 and sum(s["monthly_precip_mm"].values()) > 0
    marine = (-3 <= min(temps.values()) <= 18 and max(temps.values()) < 22
              and sum(1 for t in temps.values() if t > 10) >= 4)
    if marine and precip_ok:
        p = s["monthly_precip_mm"]
        # Northern-hemisphere seasons; mirrored for the southern hemisphere by month index.
        winter, summer = (12, 1, 2), (6, 7, 8)
        if _latitude(epw_path) < 0:
            winter, summer = summer, winter
        marine = max(p.get(m, 0) for m in winter) >= 3 * min(p.get(m, 0) for m in summer)
    elif marine:
        notes.append("marine (C) decided from temperatures only; dry-summer criterion not checked")
    if marine and s["hdd18"] <= 4000 and s["cdd10"] <= 2500:
        number = 3 if s["hdd18"] <= 2000 else 4 if s["hdd18"] <= 3000 else 5
        return f"{number}C", notes

    number = thermal_zone(s["hdd18"], s["cdd10"])
    if number >= 7:
        return str(number), notes
    if precip_ok:
        p_cm = sum(s["monthly_precip_mm"].values()) / 10.0
        moisture = "B" if p_cm < 2.0 * (s["annual_mean_c"] + 7.0) else "A"
    elif koppen:
        moisture = "B" if koppen.upper().startswith("B") else "A"
        notes.append(f"dry/humid decided from Koppen class {koppen}")
    else:
        moisture = "A"
        notes.append("precipitation unavailable; assumed humid (A)")
    return f"{number}{moisture}", notes


def _latitude(epw_path: str) -> float:
    with open(epw_path, encoding="latin-1") as f:
        header = f.readline().split(",")
    try:
        return float(header[6])
    except (IndexError, ValueError):
        return 0.0


# ASHRAE zones without a DOE reference-building location use the closest one.
_LIBRARY_PROXY = {
    "0A": ("1A", "zone 0A (extremely hot) uses the 1A reference buildings"),
    "0B": ("1A", "zone 0B (extremely hot, dry) uses the 1A reference buildings"),
    "1B": ("1A", "zone 1B (very hot, dry) uses the 1A reference buildings"),
    "5C": ("4C", "zone 5C (cool marine) uses the 4C reference buildings"),
}


def library_climate_zone(ashrae_zone: str) -> Tuple[str, Optional[str]]:
    """Map an ASHRAE 169 zone onto the archetype library's climate zones."""
    z = str(ashrae_zone).strip().upper()
    if z in _LIBRARY_PROXY:
        return _LIBRARY_PROXY[z]
    if z[:1] in ("7", "8"):
        return z[:1], None
    if z in CLIMATE_ZONES:
        return z, None
    raise ValueError(f"Unrecognized ASHRAE climate zone {ashrae_zone!r}")


def site_climate(weather: Dict[str, Any]) -> Dict[str, Any]:
    """Climate zone of downloaded/local weather files ({'epw': ..., 'stat': ...})."""
    notes: List[str] = []
    zone, source = None, None
    stat = weather.get("stat")
    if stat and Path(stat).exists():
        zone = climate_zone_from_stat(stat)
        source = "stat file (ASHRAE Handbook design conditions)"
    if not zone:
        koppen = koppen_from_stat(stat) if stat and Path(stat).exists() else None
        zone, notes = climate_zone_from_epw(weather["epw"], koppen)
        source = "estimated from EPW"
    lib_zone, note = library_climate_zone(zone)
    if note:
        notes.append(note)
    return {"ashrae_climate_zone": zone, "climate_zone_source": source,
            "library_climate_zone": lib_zone, "notes": notes}
