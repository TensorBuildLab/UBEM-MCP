"""Code UBEM abstracts into use-case categories with keyword rules (multi-label) and report shares."""
import collections
import csv
import json
import re
import sys

W = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]

# Each category: regexes over lower-cased title + abstract. A paper can fall in several.
CATS = {
    "model_generation": r"archetyp|model generation|generate(?:d|s)? (?:\w+ )?(?:building )?(?:energy )?models|automat\w* (?:generation|workflow|model)|gis[- ]based|footprint|citygml|lod ?[0-4]|level of detail|3d city model|building geometr|geometry generation|workflow|bottom-up (?:physic|engineer)|co-simulation",
    "data_stock": r"building stock (?:data|characteri|database|information)|data (?:acquisition|collection|enrichment|sources?|availability|quality|fusion|integration)|missing (?:data|attribute|information)|tax assessor|cadastr|census|open data|openstreetmap|\bosm\b|lidar|remote sensing|satellite|street view|machine learning|deep learning|data-driven|neural network|random forest|xgboost|surrogate",
    "retrofit_scenario": r"retrofit|renovat|scenario|what-if|decarboni[sz]|electrif|heat pump|energy (?:efficiency )?(?:policy|policies|measure)|policy|carbon (?:emission|reduction|neutral)|net[- ]zero|emission reduction|mitigation strateg|building performance standard",
    "demand_mapping": r"energy (?:demand|consumption|use) (?:estimation|prediction|mapping|map|assessment)|(?:estimat|predict|assess|quantif)\w* (?:the )?(?:\w+ ){0,3}(?:energy demand|energy consumption|energy use|heating demand|cooling demand)|spatial (?:distribution|mapping|analysis)|heat(?:ing)? demand map|energy map|eui",
    "supply_renewables": r"district heating|district cooling|district energy system|energy supply|photovoltaic|\bpv\b|solar (?:potential|energy|panel)|renewable|energy hub|microgrid|storage|thermal network|heat network|combined heat",
    "grid_load": r"load profile|hourly (?:load|demand|profile)|peak (?:load|demand)|grid|demand response|flexibilit|electricity (?:demand|load) (?:profile|curve)|time series|smart meter|ami\b",
    "climate_microclimate": r"climate change|future (?:climate|weather)|heat ?wave|urban heat island|\buhi\b|microclimate|resilien|extreme (?:weather|heat)|outdoor (?:thermal|temperature)|urban canopy|local climate",
    "calibration_validation": r"calibrat|validat|uncertaint|sensitivity analysis|bayesian|accuracy|measured (?:data|energy|consumption)|benchmark",
    "planning_design": r"urban (?:planning|design|form|morpholog)|neighbo(?:u)?rhood (?:design|planning)|master ?plan|planner|land use|density|block typolog|street (?:layout|canyon)|urban layout",
    "tool_platform": r"\btool\b|platform|software|framework|open[- ]source|web[- ]based|plug-?in|python|toolkit|interface|api\b",
    "occupant": r"occupan|behavio(?:u)?r|schedule",
}
COMPILED = {k: re.compile(v) for k, v in CATS.items()}


def keep(w):
    t = (w["title"] + " " + w["abstract"]).lower()
    if not w["year"] or w["year"] < 2010:
        return False
    if w["type"] in ("dataset", "conference-abstract", "erratum", "paratext"):
        return False
    return bool(re.search(r"building", t) and re.search(r"energy|heating|cooling|thermal", t)) or "ubem" in t


corpus = [w for w in W if keep(w)]
withabs = [w for w in corpus if len(w["abstract"]) > 200]
print(f"kept {len(corpus)} of {len(W)}; with abstract {len(withabs)}", file=sys.stderr)

rows = []
for w in withabs:
    t = (w["title"] + " " + w["abstract"]).lower()
    labels = [k for k, rx in COMPILED.items() if rx.search(t)]
    rows.append({**w, "labels": labels})

n = len(rows)
count = collections.Counter(lab for r in rows for lab in r["labels"])
cites = collections.Counter()
for r in rows:
    for lab in r["labels"]:
        cites[lab] += r["cites"]
total_cites = sum(r["cites"] for r in rows)
recent = [r for r in rows if r["year"] >= 2023]
count_recent = collections.Counter(lab for r in recent for lab in r["labels"])

print(f"papers coded: {n} (2023+: {len(recent)}); none matched: {sum(1 for r in rows if not r['labels'])}")
print(f"{'category':24} {'papers':>6} {'%':>6} {'% 2023+':>8} {'% cites':>8}")
for k, c in count.most_common():
    print(f"{k:24} {c:6d} {100*c/n:6.1f} {100*count_recent[k]/len(recent):8.1f} {100*cites[k]/total_cites:8.1f}")

with open("coded.csv", "w", newline="", encoding="utf-8") as f:
    wr = csv.writer(f)
    wr.writerow(["id", "year", "cites", "title"] + list(CATS))
    for r in rows:
        wr.writerow([r["id"], r["year"], r["cites"], r["title"]] + [int(k in r["labels"]) for k in CATS])
json.dump(rows, open("coded.json", "w", encoding="utf-8"))
