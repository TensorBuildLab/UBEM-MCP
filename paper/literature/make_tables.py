"""Build the committed tables from a fetch (works.jsonl) and the coding files.

    python make_tables.py works.jsonl coded.json manual_codes.txt
    python make_tables.py --second-coder works.jsonl 30 sheet.csv

The committed tables carry identifiers, titles and codes but no abstract
text, which may be under publishers' copyright; fetch_openalex.py downloads
the abstracts again. --second-coder writes a sheet of randomly chosen sample
papers, with abstracts, for a second coder to fill in (keep it out of git).
"""

import csv
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).parent


def works(path):
    return {w["id"]: w for w in map(json.loads, open(path, encoding="utf-8"))}


def main(argv):
    if argv[0] == "--second-coder":
        w, k, out = works(argv[1]), int(argv[2]), argv[3]
        with open(HERE / "manual_codes.csv", encoding="utf-8") as f:
            sample = [r for r in csv.DictReader(f) if r["code"] != "off"]
        chosen = random.Random(2026).sample(sample, k)
        with open(out, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["id", "doi", "year", "title", "abstract", "code"])
            for r in chosen:
                x = w[r["id"]]
                wr.writerow([r["id"], x.get("doi"), x.get("year"), x.get("title"), x.get("abstract"), ""])
        print(f"wrote {k} papers to {out}; fill 'code' with one letter from codebook.md")
        return
    w = works(argv[0])
    with open(HERE / "records.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "doi", "year", "type", "cites", "venue", "title", "has_abstract"])
        for x in w.values():
            wr.writerow([x["id"], x.get("doi"), x.get("year"), x.get("type"), x.get("cites"), x.get("venue"),
                         x.get("title"), int(bool(x.get("abstract")))])
    coded = json.load(open(argv[1], encoding="utf-8"))
    cats = sorted({lab for r in coded for lab in r["labels"]})
    with open(HERE / "keyword_codes.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "year", "cites"] + cats)
        for r in coded:
            wr.writerow([r["id"], r["year"], r["cites"]] + [int(c in r["labels"]) for c in cats])
    codes = open(argv[2], encoding="utf-8").read().split()
    # manual_codes.txt holds "index code" pairs in sample order
    pairs = dict(zip(codes[0::2], codes[1::2]))
    ids = json.load(open(HERE / "sample_ids.json", encoding="utf-8"))
    with open(HERE / "manual_codes.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "title", "code"])
        for i, pid in enumerate(ids):
            wr.writerow([pid, w[pid]["title"], pairs[str(i)]])
    print(f"records: {len(w)}, keyword-coded: {len(coded)}, hand-coded: {len(ids)}")


if __name__ == "__main__":
    main(sys.argv[1:])
