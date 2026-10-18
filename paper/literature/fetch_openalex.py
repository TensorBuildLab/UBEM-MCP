"""Fetch UBEM papers from OpenAlex (title/abstract search) and save JSONL with reconstructed abstracts."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1])
QUERIES = [
    '"urban building energy model"', '"urban building energy models"', '"urban building energy modeling"',
    '"urban building energy modelling"', '"urban-scale building energy"', '"urban scale building energy"',
    '"building stock energy model"', '"building stock energy modelling"', '"building stock energy modeling"',
    '"district-scale building energy"', '"city-scale building energy"', '"urban energy modeling of buildings"',
    'UBEM',
]
MAILTO = os.environ.get("OPENALEX_MAILTO", "")  # optional: your e-mail for the OpenAlex polite pool


def get(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            print("retry", attempt, e, file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(url)


def abstract(inv):
    if not inv:
        return ""
    pos = {}
    for w, idx in inv.items():
        for i in idx:
            pos[i] = w
    return " ".join(pos[i] for i in sorted(pos))


works = {}
for q in QUERIES:
    page = 1
    n = 0
    while page:
        params = {"filter": f"title_and_abstract.search:{q}", "per-page": "200", "page": str(page),
                  "select": "id,doi,title,publication_year,type,cited_by_count,abstract_inverted_index,primary_location",
                  "mailto": MAILTO}
        data = get("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        for w in data["results"]:
            if w["id"] in works:
                continue
            src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
            works[w["id"]] = {"id": w["id"], "doi": w.get("doi"), "title": w.get("title") or "",
                              "year": w.get("publication_year"), "type": w.get("type"),
                              "cites": w.get("cited_by_count", 0), "venue": src,
                              "abstract": abstract(w.get("abstract_inverted_index"))}
            n += 1
        page = page + 1 if len(data["results"]) == 200 else 0
    print(q, "new:", n, "total:", len(works), file=sys.stderr)

with open(OUT, "w", encoding="utf-8") as f:
    for w in works.values():
        f.write(json.dumps(w) + "\n")
print(len(works))
