"""Build the small agent-test fixtures from the campus case-study footprints.

Footprints come from OpenStreetMap (ODbL, (c) OpenStreetMap contributors).
The parcel numbers, years, land-use codes, owner names and meter readings
added here are synthetic, made up for the tests; they describe no real
parcel, owner or meter.
"""
import csv
import json
import sys
from pathlib import Path

from shapely.geometry import shape
from pyproj import Geod

src = Path(sys.argv[1] if len(sys.argv) > 1 else "outputs/case_study/campus_source.geojson")
out = Path(__file__).parent / "fixtures"
data = json.loads(src.read_text(encoding="utf-8"))
geod = Geod(ellps="WGS84")


def area(f):
    return abs(geod.geometry_area_perimeter(shape(f["geometry"]))[0])


feats = data["features"]
pick = []
for kind, n in (("house", 4), ("university", 2), ("retail", 1), ("office", 1)):
    cands = sorted((f for f in feats if f["properties"].get("building") == kind and area(f) > 60),
                   key=lambda f: str(f["properties"].get("osm_id")))
    pick += cands[:n]
small = [f for f in feats if f["properties"].get("building") in ("garage", "shed")][:2]
KEEP = ("building", "building:levels", "osm_id", "name", "start_date")


def clean(f):
    return {"type": "Feature", "geometry": f["geometry"],
            "properties": {k: f["properties"][k] for k in KEEP if k in f["properties"]}}


attribution = "Footprints (c) OpenStreetMap contributors, ODbL 1.0"
(out / "campus_small.geojson").write_text(json.dumps(
    {"type": "FeatureCollection", "attribution": attribution, "features": [clean(f) for f in pick]}, indent=1))

inv = []
years = [1948, 1955, 1962, None, 1971, 2005, 1987, 1999, None, None]
uses = {"house": "0131", "university": "0710", "retail": "0310", "office": "0320", "garage": "0131", "shed": "0131"}
for i, f in enumerate(pick + small):
    g = clean(f)
    p = g["properties"]
    p["PARCEL"] = f"P-{1001 + i // 2 if p['building'] in ('garage', 'shed') else 1001 + i}"
    if years[i]:
        p["YEAR_BUILT"] = years[i]
    p["LAND_USE"] = uses[p["building"]]
    p["OWNER_NAME"] = f"Synthetic Owner {i + 1}"
    p["MAIL_ADDR"] = f"{100 + i} Example Street"
    inv.append(g)
(out / "parcels_inventory.geojson").write_text(json.dumps(
    {"type": "FeatureCollection", "attribution": attribution + "; parcel attributes synthetic", "features": inv},
    indent=1))

eui = {"house": 110, "university": 260, "retail": 330, "office": 210}
with open(out / "bills_2023.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["OSM Id", "Account Holder", "Electricity Use (kWh)", "Natural Gas Use (therms)"])
    for i, f in enumerate(pick):
        p = f["properties"]
        floors = float(p.get("building:levels") or (1 if p["building"] == "house" else 3))
        kwh = eui[p["building"]] * area(f) * floors * (0.9 + 0.05 * (i % 5))
        w.writerow([p["osm_id"], f"Synthetic Holder {i + 1}", round(kwh * 0.75), round(kwh * 0.25 / 29.307)])
print("fixtures:", [p.name for p in out.iterdir()])
