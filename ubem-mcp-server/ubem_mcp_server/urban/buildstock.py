"""
NREL ComStock and ResStock data — new in UBEM-MCP. See NOTICE.md.

ComStock (commercial) and ResStock (residential) publish, for every US
county, a sample of simulated buildings with their characteristics, annual
energy use and a weight: the number of real buildings each sample stands
for. This module downloads the annual results from the Open Energy Data
Initiative (OEDI), keeps a compact copy, and answers weighted questions:
how many buildings of a type, how much energy, what EUI, what an upgrade
saves. Every answer carries the number of samples behind it, because a
weighted total over a handful of samples looks precise and is not.

The data are CC BY 4.0; the attribution travels with every answer.
"""

import gzip
import io
import json
import math
import re
import statistics
import tarfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

OEDI = "https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock"
TBTU_TO_KWH = 2.9307107e8
FT2_TO_M2 = 0.09290304
FUELS = ("electricity", "natural_gas", "propane", "fuel_oil", "district_heating", "district_cooling", "other_fuel")
DEFAULT_MIN_SAMPLES = 30

RELEASES: Dict[str, Dict[str, Any]] = {
    "comstock": {
        "default": "2025_amy2018_r3",
        "2025_amy2018_r3": {
            "title": "ComStock 2025 Release 3 (AMY2018)",
            "root": f"{OEDI}/2025/comstock_amy2018_release_3",
            "file": "metadata_and_annual_results/by_state_and_county/basic/csv/state={state}/county={county}/"
                    "{state}_{county}_upgrade{upgrade}_basic.csv.gz",
            "needs_county": True,
            "columns": {"building_type": "in.comstock_building_type", "vintage": "in.vintage",
                        "county": "in.nhgis_county_gisjoin", "climate_zone": "in.ashrae_iecc_climate_zone_2006"},
        },
    },
    "resstock": {
        "default": "2025_amy2018_r1",
        "2025_amy2018_r1": {
            "title": "ResStock 2025 Release 1 (AMY2018)",
            "root": f"{OEDI}/2025/resstock_amy2018_release_1",
            "file": "metadata_and_annual_results/by_state/full/csv/state={state}/{state}_upgrade{upgrade}.csv.gz",
            "needs_county": False,
            "columns": {"building_type": "in.geometry_building_type_recs", "vintage": "in.vintage",
                        "county": "in.county", "climate_zone": "in.ashrae_iecc_climate_zone_2004"},
        },
    },
}

# UBEM-MCP archetype -> (dataset, building type in that dataset)
ARCHETYPE_TYPES = {
    "single_family": ("resstock", "Single-Family Detached"),
    "midrise_apartment": ("resstock", "Multi-Family with 5+ Units"),
    "highrise_apartment": ("resstock", "Multi-Family with 5+ Units"),
    "small_office": ("comstock", "SmallOffice"), "medium_office": ("comstock", "MediumOffice"),
    "large_office": ("comstock", "LargeOffice"), "standalone_retail": ("comstock", "RetailStandalone"),
    "strip_mall": ("comstock", "RetailStripmall"), "primary_school": ("comstock", "PrimarySchool"),
    "secondary_school": ("comstock", "SecondarySchool"), "outpatient": ("comstock", "Outpatient"),
    "hospital": ("comstock", "Hospital"), "small_hotel": ("comstock", "SmallHotel"),
    "large_hotel": ("comstock", "LargeHotel"), "warehouse": ("comstock", "Warehouse"),
    "quick_service_restaurant": ("comstock", "QuickServiceRestaurant"),
    "full_service_restaurant": ("comstock", "FullServiceRestaurant"),
}

STATE_FIPS = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
              "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
              "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
              "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
              "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
              "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
              "WV": "54", "WI": "55", "WY": "56"}


def release_info(dataset: str, release: Optional[str] = None) -> Dict[str, Any]:
    if dataset not in RELEASES:
        raise ValueError(f"dataset must be one of {sorted(RELEASES)}")
    rel = release or RELEASES[dataset]["default"]
    if rel not in RELEASES[dataset]:
        raise ValueError(f"Unknown {dataset} release {rel!r}; available: "
                         f"{[k for k in RELEASES[dataset] if k != 'default']}")
    return {"dataset": dataset, "release": rel, **RELEASES[dataset][rel]}


def county_gisjoin(state: str, county: str) -> str:
    """'G0400190' from 'G0400190', '04019' or '019' (with state 'AZ')."""
    c = county.strip().upper()
    if re.fullmatch(r"G\d{7}", c):
        return c
    digits = re.sub(r"\D", "", c)
    if len(digits) == 5:
        return f"G{digits[:2]}0{digits[2:]}0"
    if len(digits) == 3 and state.upper() in STATE_FIPS:
        return f"G{STATE_FIPS[state.upper()]}0{digits}0"
    raise ValueError(f"County {county!r}: give a 5-digit FIPS code such as '04019' or an NHGIS code such as "
                     f"'G0400190'")


def file_url(info: Dict[str, Any], state: str, county: Optional[str], upgrade: int) -> str:
    state = state.strip().upper()
    if state not in STATE_FIPS:
        raise ValueError(f"Unknown US state {state!r}; use the two-letter code, e.g. 'AZ'")
    if info["needs_county"] and not county:
        raise ValueError(f"{info['title']} is published by county; give county (5-digit FIPS, e.g. '04019')")
    g = county_gisjoin(state, county) if county else None
    return f"{info['root']}/" + info["file"].format(state=state, county=g, upgrade=int(upgrade))


def attribution(info: Dict[str, Any]) -> str:
    name = "ComStock" if info["dataset"] == "comstock" else "ResStock"
    return (f"NREL {name}: {info['title']}, End-Use Load Profiles for the U.S. Building Stock, "
            f"Open Energy Data Initiative (OEDI), CC BY 4.0")


def _csv_bytes(raw: bytes) -> bytes:
    """The CSV inside a download: OEDI's .csv.gz files are gzipped tar archives
    holding one CSV; plain gzip and plain CSV are accepted too."""
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if len(data) > 512 and data[257:262] == b"ustar":
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            member = next(m for m in tar.getmembers() if m.isfile())
            return tar.extractfile(member).read()
    return data


def normalize(raw: bytes, info: Dict[str, Any], county: Optional[str] = None) -> pd.DataFrame:
    """A compact table: id, weight, standard fields, kWh by fuel, EUI, and
    every `in.*` characteristic column."""
    text_io = io.BytesIO(_csv_bytes(raw))
    header = pd.read_csv(text_io, nrows=0).columns.tolist()
    text_io.seek(0)
    wanted = {header[0], "bldg_id", "weight", "upgrade", "applicability", "completed_status", "in.sqft..ft2"}
    wanted |= {c for c in header if c.startswith("in.")}
    fuel_cols = {}
    for f in FUELS + ("site_energy",):
        kwh = f"out.{f}.total.energy_consumption..kwh"
        tbtu = f"calc.weighted.{f}.total.energy_consumption..tbtu"
        if kwh in header:
            fuel_cols[f] = (kwh, 1.0, False)
        elif tbtu in header:
            fuel_cols[f] = (tbtu, TBTU_TO_KWH, True)
    wanted |= {c for c, _, _ in fuel_cols.values()}
    df = pd.read_csv(text_io, usecols=[c for c in header if c in wanted], low_memory=False)
    id_col = "bldg_id" if "bldg_id" in df.columns else header[0]  # some CSV exports name it after the file
    if "completed_status" in df.columns:
        df = df[df["completed_status"].astype(str) == "Success"]
    cols = info["columns"]
    if county:
        df = df[df[cols["county"]].astype(str) == county]
    out = pd.DataFrame({"bldg_id": df[id_col].astype("int64"), "weight": df["weight"].astype(float)})
    out["upgrade"] = df["upgrade"].astype(int) if "upgrade" in df.columns else 0
    out["applicable"] = df["applicability"].astype(str).str.lower().isin(("true", "1")) \
        if "applicability" in df.columns else True
    for key, col in cols.items():
        out[key] = df[col].astype(str) if col in df.columns else None
    out["floor_area_m2"] = df["in.sqft..ft2"].astype(float) * FT2_TO_M2
    for f, (col, factor, weighted) in fuel_cols.items():
        v = df[col].astype(float) * factor
        out[f"{'site_total' if f == 'site_energy' else f}_kwh"] = v / df["weight"] if weighted else v
    if "site_total_kwh" not in out:
        out["site_total_kwh"] = out[[c for c in out.columns if c.endswith("_kwh")]].sum(axis=1)
    out["eui_kwh_m2"] = out["site_total_kwh"] / out["floor_area_m2"]
    extra = df[[c for c in df.columns if c.startswith("in.") and c not in cols.values()]]
    return pd.concat([out.reset_index(drop=True), extra.reset_index(drop=True)], axis=1)


# --------------------------------------------------------------------------- #
# Weighted statistics
# --------------------------------------------------------------------------- #

def weighted_quantiles(values: Sequence[float], weights: Sequence[float],
                       qs: Sequence[float] = (0.25, 0.5, 0.75)) -> List[Optional[float]]:
    pairs = sorted((v, w) for v, w in zip(values, weights) if v is not None and math.isfinite(v) and w > 0)
    if not pairs:
        return [None] * len(qs)
    total = sum(w for _, w in pairs)
    out, acc, i = [], 0.0, 0
    for q in qs:
        target = q * total
        while i < len(pairs) - 1 and acc + pairs[i][1] < target:
            acc += pairs[i][1]
            i += 1
        out.append(pairs[i][0])
    return out


def weighted_percentile_of(value: float, values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    pairs = [(v, w) for v, w in zip(values, weights) if v is not None and math.isfinite(v) and w > 0]
    total = sum(w for _, w in pairs)
    if not total:
        return None
    return round(100 * sum(w for v, w in pairs if v <= value) / total, 1)


def resolve_column(df: pd.DataFrame, name: str) -> str:
    if name in df.columns:
        return name
    for cand in (f"in.{name}", name.lower()):
        if cand in df.columns:
            return cand
    low = {c.lower(): c for c in df.columns}
    if name.lower() in low:
        return low[name.lower()]
    raise KeyError(f"No column {name!r}; standard fields: building_type, vintage, county, climate_zone, "
                   f"floor_area_m2, eui_kwh_m2, <fuel>_kwh; or any 'in.*' characteristic")


def apply_filters(df: pd.DataFrame, filters: Optional[Dict[str, Any]]) -> pd.DataFrame:
    for name, cond in (filters or {}).items():
        col = resolve_column(df, name)
        s = df[col]
        if isinstance(cond, dict):
            num = pd.to_numeric(s, errors="coerce")
            mask = pd.Series(True, index=df.index)
            if "min" in cond:
                mask &= num >= cond["min"]
            if "max" in cond:
                mask &= num <= cond["max"]
            df = df[mask]
        elif isinstance(cond, list):
            df = df[s.astype(str).isin([str(c) for c in cond])]
        else:
            df = df[s.astype(str) == str(cond)]
    return df


def summarize(df: pd.DataFrame, group_by: Optional[Sequence[str]] = None,
              min_samples: int = DEFAULT_MIN_SAMPLES, max_groups: int = 30) -> Dict[str, Any]:
    """Weighted totals and EUI, overall and by group, with sample counts."""
    fuels = [c for c in df.columns if c.endswith("_kwh")]

    def stats(part: pd.DataFrame) -> Dict[str, Any]:
        w = part["weight"]
        area = float((part["floor_area_m2"] * w).sum())
        energy = {c[:-4]: round(float((part[c].fillna(0) * w).sum())) for c in fuels}
        q = weighted_quantiles(part["eui_kwh_m2"].tolist(), w.tolist())
        return {"samples": int(len(part)), "buildings_represented": round(float(w.sum()), 1),
                "floor_area_m2": round(area), "energy_kwh": energy,
                "eui_kwh_m2": round(energy.get("site_total", 0) / area, 1) if area else None,
                "eui_kwh_m2_p25_p50_p75": [None if x is None else round(x, 1) for x in q],
                **({"few_samples": True} if len(part) < min_samples else {})}

    out: Dict[str, Any] = {"overall": stats(df) if len(df) else {"samples": 0}}
    if group_by:
        cols = [resolve_column(df, g) for g in group_by]
        groups = []
        for key, part in df.groupby(cols, dropna=False):
            key = key if isinstance(key, tuple) else (key,)
            groups.append({"group": {g: (None if pd.isna(k) else k) for g, k in zip(group_by, key)}, **stats(part)})
        groups.sort(key=lambda g: -g["buildings_represented"])
        out["num_groups"] = len(groups)
        out["groups"] = groups[:max_groups]
        few = sum(1 for g in groups if g.get("few_samples"))
        if few:
            out["sample_warning"] = (f"{few} group(s) rest on fewer than {min_samples} simulated buildings; their "
                                     f"weighted totals are uncertain. Group more coarsely or report them as "
                                     f"indicative.")
    return out


def upgrade_savings(base: pd.DataFrame, upg: pd.DataFrame, group_by: Optional[Sequence[str]] = None,
                    min_samples: int = DEFAULT_MIN_SAMPLES, max_groups: int = 30) -> Dict[str, Any]:
    """Weighted savings of an upgrade over the baseline, matched by bldg_id,
    for the buildings the upgrade applies to."""
    fuels = [c for c in base.columns if c.endswith("_kwh") and c in upg.columns]
    m = base.merge(upg[["bldg_id", "applicable"] + fuels], on="bldg_id", suffixes=("", "_upg"))
    applicable = m[m["applicable_upg"]]

    def stats(part):
        w = part["weight"]
        res = {"samples": int(len(part)), "buildings_represented": round(float(w.sum()), 1)}
        for c in fuels:
            b = float((part[c].fillna(0) * w).sum())
            u = float((part[f"{c}_upg"].fillna(0) * w).sum())
            res[f"{c[:-4]}_savings_kwh"] = round(b - u)
            if c == "site_total_kwh":
                res["site_energy_savings_percent"] = round(100 * (b - u) / b, 1) if b else None
        if len(part) < min_samples:
            res["few_samples"] = True
        return res

    out = {"applicable_samples": int(len(applicable)), "baseline_samples": int(len(base)),
           "overall": stats(applicable)}
    if group_by and len(applicable):
        cols = [resolve_column(applicable, g) for g in group_by]
        groups = [{"group": dict(zip(group_by, k if isinstance(k, tuple) else (k,))), **stats(p)}
                  for k, p in applicable.groupby(cols, dropna=False)]
        groups.sort(key=lambda g: -g["buildings_represented"])
        out["groups"] = groups[:max_groups]
    return out


def benchmark(modeled: Sequence[Dict[str, Any]], references: Dict[str, pd.DataFrame],
              min_samples: int = DEFAULT_MIN_SAMPLES) -> Dict[str, Any]:
    """Modeled EUI by archetype against the weighted EUI distribution of the
    matching ComStock/ResStock building type."""
    by_arch: Dict[str, List[float]] = {}
    for r in modeled:
        if r.get("site_eui_kwh_m2") not in (None, ""):
            by_arch.setdefault(r["archetype"], []).append(float(r["site_eui_kwh_m2"]))
    rows, not_covered = [], []
    for arch, euis in sorted(by_arch.items()):
        ds, btype = ARCHETYPE_TYPES.get(arch, (None, None))
        ref = references.get(ds) if ds else None
        if ref is None:
            not_covered.append(arch)
            continue
        part = ref[ref["building_type"] == btype]
        q = weighted_quantiles(part["eui_kwh_m2"].tolist(), part["weight"].tolist())
        med = statistics.median(euis)
        row = {"archetype": arch, "modeled_buildings": len(euis), "modeled_median_eui_kwh_m2": round(med, 1),
               "reference": {"dataset": ds, "building_type": btype, "samples": int(len(part)),
                             "buildings_represented": round(float(part["weight"].sum()), 1),
                             "eui_kwh_m2_p25_p50_p75": [None if x is None else round(x, 1) for x in q]},
               "modeled_median_percentile_in_reference": weighted_percentile_of(
                   med, part["eui_kwh_m2"].tolist(), part["weight"].tolist())}
        if len(part) < min_samples:
            row["few_reference_samples"] = True
        rows.append(row)
    return {"archetypes": rows, "not_covered": not_covered,
            "note": "Both are model results: this shows whether the archetype models fall within the range of "
                    "NREL's stock models for the same building type and area, not whether either is right."}


def http_fetch(url: str, timeout: float = 300.0) -> bytes:
    from .weather import http_get
    return http_get(url, timeout=timeout)


def upgrades_lookup(info: Dict[str, Any], fetch: Callable[[str], bytes]) -> Dict[str, str]:
    try:
        return json.loads(fetch(f"{info['root']}/upgrades_lookup.json").decode("utf-8"))
    except Exception:  # noqa: BLE001 - optional extra; the data are usable without it
        return {}


def save_table(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def load_table(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    df["applicable"] = df["applicable"].astype(str).str.lower().isin(("true", "1"))
    return df
