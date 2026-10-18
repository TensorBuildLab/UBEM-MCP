"""Shares of each main contribution in the hand-coded sample (paper Section 1.4, Table 2).

    python analyze_sample.py [manual_codes.csv] [second_coder.csv]

With a second coder's sheet (same columns, code filled in), also reports
percent agreement and Cohen's kappa on the papers both coded.
"""

import collections
import csv
import math
import sys
from pathlib import Path

HERE = Path(__file__).parent
CODES = {
    "G": "Build or generate UBEM models from GIS and building data (incl. speed-ups, archetypes, LoD)",
    "D": "UBEM data work (acquire, enrich, characterise stock, data-driven prediction)",
    "S": "Scenario, retrofit, policy or decarbonisation decisions",
    "V": "Calibration, validation, uncertainty",
    "M": "Baseline demand estimation and mapping",
    "R": "Supply side: renewables, district energy",
    "O": "Occupant behaviour",
    "C": "Climate change, microclimate, urban heat island",
    "P": "Urban planning and morphology",
    "L": "Grid flexibility and load profiles",
    "X": "Reviews, education, other",
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return {r["id"]: r["code"].strip() for r in csv.DictReader(f) if r.get("code", "").strip()}


def wilson(k, n, z=1.96):
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return centre - half, centre + half


def main():
    first = read(sys.argv[1] if len(sys.argv) > 1 else HERE / "manual_codes.csv")
    codes = [c for c in first.values() if c != "off"]
    n = len(codes)
    print(f"n = {n} on-topic papers ({len(first) - n} off topic)")
    for k, c in collections.Counter(codes).most_common():
        lo, hi = wilson(c, n)
        print(f"{k} {c:3d} {100 * c / n:5.1f}%  [{100 * lo:4.1f}-{100 * hi:4.1f}]  {CODES[k]}")
    if len(sys.argv) > 2:
        second = read(sys.argv[2])
        both = [i for i in second if i in first]
        a = [first[i] for i in both]
        b = [second[i] for i in both]
        po = sum(x == y for x, y in zip(a, b)) / len(both)
        ca, cb = collections.Counter(a), collections.Counter(b)
        pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / len(both) ** 2
        kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
        print(f"agreement on {len(both)} papers: {100 * po:.0f}%, Cohen's kappa {kappa:.2f}")


if __name__ == "__main__":
    main()
