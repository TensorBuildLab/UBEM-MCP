"""Tests for weather-station lookup, weather download and climate zones.

Network access is replaced by an in-memory fake, so these run offline.
"""
import io
import json
import zipfile
from pathlib import Path

import openpyxl
import pytest

from ubem_mcp_server.config import Config
from ubem_mcp_server.urban import weather
from ubem_mcp_server.urban.stock_manager import StockManager

SAMPLE_DIR = Path(__file__).parent.parent / "sample_files"
SF_EPW = SAMPLE_DIR / "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw"
BASE = "https://climate.onebuilding.org/WMO_Region_4_North_and_Central_America/USA_United_States_of_America"
HEADER = ("Country", "State", "City/Station", "WMO", "Source Data", "Latitude (N+/S-)",
          "Longitude (E+/W-)", "Time Zone (GMT +/-)", "Elevation (m)", "URL")
ROWS = [
    ("USA", "AZ", "Tucson", 722740, "Custom-722740", 32.131, -110.955, -7, 776.9,
     f"{BASE}/AZ_Arizona/USA_AZ_Tucson.722740_TMYx.2004-2018.zip"),
    ("USA", "AZ", "Tucson", 722740, "SRC-TMYx", 32.228, -110.956, -7, 776.9,
     f"{BASE}/AZ_Arizona/USA_AZ_Tucson.722740_TMYx.2009-2023.zip"),
    ("USA", "AZ", "Tucson", 722740, "SRC-TMYx", 32.228, -110.956, -7, 776.9,
     f"{BASE}/AZ_Arizona/USA_AZ_Tucson.722740_TMYx.zip"),
    ("USA", "AZ", "Tucson-Ryan.Field.AP", 720339, "SRC-TMYx", 32.142, -111.175, -7, 736.7,
     f"{BASE}/AZ_Arizona/USA_AZ_Tucson-Ryan.Field.AP.720339_TMYx.zip"),
    ("USA", "AZ", "Phoenix-Sky.Harbor.Intl.AP", 722780, "SRC-TMYx", 33.428, -112.004, -7, 337.0,
     f"{BASE}/AZ_Arizona/USA_AZ_Phoenix-Sky.Harbor.Intl.AP.722780_TMYx.2009-2023.zip"),
    ("USA", "CA", "bad row", None, "SRC-TMYx", "n/a", None, None, None, "not a url"),
]
STAT_2B = ' - Climate Zone "2B" (ASHRAE Standard 169-2021)\n - Climate type "BSh" (K\xf6ppen classification)**\n'


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _weather_zip(epw_bytes, stat_text=STAT_2B, extra=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("USA_AZ_Tucson.722740_TMYx.2009-2023.epw", epw_bytes)
        if stat_text is not None:
            zf.writestr("USA_AZ_Tucson.722740_TMYx.2009-2023.stat", stat_text.encode("latin-1"))
        for name, data in extra:
            zf.writestr(name, data)
    return buf.getvalue()


class FakeWeb:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for key, data in self.routes.items():
            if key in url:
                return data
        raise AssertionError(f"unexpected download {url}")


def _index():
    return weather.StationIndex(weather.stations_from_rows(ROWS))


# --------------------------------------------------------------------------- #
# Stations
# --------------------------------------------------------------------------- #

def test_rows_group_into_stations_with_datasets():
    stations = {s.file_stem: s for s in weather.stations_from_rows(ROWS)}
    assert set(stations) == {"USA_AZ_Tucson.722740", "USA_AZ_Tucson-Ryan.Field.AP.720339",
                             "USA_AZ_Phoenix-Sky.Harbor.Intl.AP.722780"}
    tucson = stations["USA_AZ_Tucson.722740"]
    assert tucson.datasets == ["TMYx.2004-2018", "TMYx.2009-2023", "TMYx"]
    assert tucson.latitude == pytest.approx(32.228)  # source-data rows win over "Custom" coordinates
    assert tucson.default_dataset() == "TMYx.2009-2023"
    assert stations["USA_AZ_Tucson-Ryan.Field.AP.720339"].default_dataset() == "TMYx"
    assert tucson.url("TMYx").endswith("/USA_AZ_Tucson.722740_TMYx.zip")
    with pytest.raises(ValueError):
        tucson.url("TMY3")


def test_nearest_station_and_distance():
    (st, d), (st2, _) = _index().nearest(32.2319, -110.9501, n=2)  # University of Arizona
    assert st.name == "Tucson" and d < 2
    assert st2.name == "Tucson-Ryan.Field.AP"
    assert weather.haversine_km(32.2, -110.9, 33.4, -112.0) == pytest.approx(166, abs=3)
    with pytest.raises(ValueError):
        _index().nearest(95, 0)


def test_index_downloads_once_then_uses_cache(tmp_path):
    web = FakeWeb({"Region4_USA": _xlsx(ROWS), "_TMYx_EPW_Processing_locations.xlsx": _xlsx([])})
    index = weather.StationIndex.load(str(tmp_path), fetch=web)
    assert len(index) == 3 and len(web.calls) == len(weather.INDEX_REGIONS)
    assert json.loads((tmp_path / "stations.json").read_text())["source"] == "https://climate.onebuilding.org"

    def offline(url):
        raise AssertionError("should use the cache")

    assert len(weather.StationIndex.load(str(tmp_path), fetch=offline)) == 3


def test_download_extracts_only_weather_files_without_paths(tmp_path):
    st = next(s for s in _index().stations if s.name == "Tucson")
    data = _weather_zip(SF_EPW.read_bytes(), extra=[("../../evil.epw.txt", b"x"), ("sub/readme.txt", b"x"),
                                                    ("sub/USA_AZ_Tucson.722740_TMYx.2009-2023.ddy", b"ddy")])
    web = FakeWeb({"USA_AZ_Tucson.722740_TMYx.2009-2023.zip": data})
    files = weather.download_weather(st, None, str(tmp_path), fetch=web)
    dest = tmp_path / "USA_AZ_Tucson.722740_TMYx.2009-2023"
    assert files["directory"] == str(dest) and files["dataset"] == "TMYx.2009-2023"
    assert sorted(p.name for p in dest.iterdir()) == [
        "USA_AZ_Tucson.722740_TMYx.2009-2023.ddy", "USA_AZ_Tucson.722740_TMYx.2009-2023.epw",
        "USA_AZ_Tucson.722740_TMYx.2009-2023.stat"]
    assert not list(tmp_path.parent.glob("evil*"))
    weather.download_weather(st, None, str(tmp_path), fetch=web)  # reused, not downloaded again
    assert len(web.calls) == 1


# --------------------------------------------------------------------------- #
# Climate zones
# --------------------------------------------------------------------------- #

def test_climate_zone_from_stat(tmp_path):
    new = tmp_path / "new.stat"
    new.write_text(STAT_2B, encoding="latin-1")
    old = tmp_path / "old.stat"
    old.write_text(' - Climate type "3C" (ASHRAE Standard 196-2006 Climate Zone)**\n', encoding="latin-1")
    none = tmp_path / "none.stat"
    none.write_text("no climate line\n", encoding="latin-1")
    assert weather.climate_zone_from_stat(str(new)) == "2B"
    assert weather.koppen_from_stat(str(new)) == "BSh"
    assert weather.climate_zone_from_stat(str(old)) == "3C"
    assert weather.climate_zone_from_stat(str(none)) is None


def test_climate_zone_estimated_from_epw():
    zone, notes = weather.climate_zone_from_epw(str(SF_EPW))
    assert zone == "3C"  # San Francisco: marine, HDD18 ~1560
    assert any("one weather year" in n for n in notes)


@pytest.mark.parametrize("hdd,cdd,number", [
    (0, 6500, 0), (100, 5500, 1), (885, 3902, 2), (1500, 3000, 3), (1557, 1436, 3), (2500, 2000, 4),
    (3500, 1000, 5), (4500, 800, 6), (6000, 500, 7), (8000, 100, 8)])
def test_thermal_zone_thresholds(hdd, cdd, number):
    assert weather.thermal_zone(hdd, cdd) == number


def test_library_climate_zone_mapping():
    assert weather.library_climate_zone("2B") == ("2B", None)
    assert weather.library_climate_zone("7") == ("7", None)
    assert weather.library_climate_zone("8A") == ("8", None)
    zone, note = weather.library_climate_zone("0B")
    assert zone == "2B" and "0B" in note
    assert weather.library_climate_zone("1B")[0] == "2B"  # Phoenix under ASHRAE 169-2021
    assert weather.library_climate_zone("0A")[0] == "1A"
    assert weather.library_climate_zone("5C")[0] == "4C"
    with pytest.raises(ValueError):
        weather.library_climate_zone("9Q")


# --------------------------------------------------------------------------- #
# Stock integration
# --------------------------------------------------------------------------- #

def _stock_manager(tmp_path):
    cfg = Config()
    cfg.paths.output_dir = str(tmp_path / "outputs")
    cfg.paths.sample_files_path = str(SAMPLE_DIR)
    sm = StockManager(cfg)
    sm.fetch = FakeWeb({"Region4_USA": _xlsx(ROWS), "_TMYx_EPW_Processing_locations.xlsx": _xlsx([]),
                        "USA_AZ_Tucson.722740_TMYx.2009-2023.zip": _weather_zip(SF_EPW.read_bytes())})
    lon, lat, d = -110.9501, 32.2319, 0.0002
    src = tmp_path / "stock.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"id": "a", "building": "office"},
        "geometry": {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d],
                                                          [lon, lat + d], [lon, lat]]]}}]}))
    sm.load_from_file(str(src), stock_id="uofa")
    return sm


def test_stock_weather_is_resolved_and_saved(tmp_path):
    sm = _stock_manager(tmp_path)
    nearest = sm.find_weather_stations(stock_id="uofa", n=2)
    assert nearest["stations"][0]["station"] == "USA_AZ_Tucson.722740"

    info = sm.resolve_weather("uofa")
    assert info["station"]["name"] == "Tucson" and info["distance_km"] < 2
    assert info["ashrae_climate_zone"] == "2B" and info["library_climate_zone"] == "2B"
    assert info["climate_zone_source"].startswith("stat file")
    assert Path(info["epw"]).exists() and not info["warnings"]
    assert sm.get_meta("uofa")["weather"]["epw"] == info["epw"]
    wsrc = [s for s in sm.get_meta("uofa")["sources"] if s["role"] == "weather"]
    assert len(wsrc) == 1 and wsrc[0]["id"] == "climate.onebuilding.org" and "Lawrie" in wsrc[0]["attribution"]

    # Model generation picks up the saved weather; an explicit zone still wins.
    epw, zone, climate = sm._weather_for_models("uofa", None, None)
    assert (epw, zone, climate["source"]) == (info["epw"], "2B", info["climate_zone_source"])
    assert sm._weather_for_models("uofa", "3C", None)[1:] == ("3C", {"climate_zone": "3C", "source": "given",
                                                                     "weather_file": None})


def test_local_weather_file_far_from_site_warns(tmp_path):
    sm = _stock_manager(tmp_path)
    info = sm.resolve_weather("uofa", weather_file=str(SF_EPW))
    assert info["source"] == "local file"
    assert info["ashrae_climate_zone"] == "3C" and info["climate_zone_source"] == "estimated from EPW"
    assert info["distance_km"] > 1000 and "km from the site" in info["warnings"][0]
    assert not any("climate.onebuilding.org" in c for c in sm.fetch.calls)


def test_http_get_reports_missing_and_unreachable(monkeypatch):
    import io
    import urllib.error

    from ubem_mcp_server.urban import weather as w

    def raise_(exc):
        def f(*a, **k):
            raise exc
        return f

    monkeypatch.setattr(w.urllib.request, "urlopen",
                        raise_(urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO())))
    with pytest.raises(FileNotFoundError, match="404"):
        w.http_get("https://x/y")
    monkeypatch.setattr(w.urllib.request, "urlopen",
                        raise_(urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO())))
    with pytest.raises(ConnectionError, match="503"):
        w.http_get("https://x/y")
    monkeypatch.setattr(w.urllib.request, "urlopen", raise_(urllib.error.URLError("no route")))
    with pytest.raises(ConnectionError, match="no route"):
        w.http_get("https://x/y")
