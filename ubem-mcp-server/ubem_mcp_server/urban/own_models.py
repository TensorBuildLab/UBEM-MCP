"""
Users' own building models inside a model set — new in UBEM-MCP. See NOTICE.md.

A model set mixes archetype models with complete EnergyPlus models supplied by
the user (from OpenStudio, a consultant, a calibration study or another UBEM
tool). A supplied model is copied, not edited, except that the outputs that
district aggregation reads are added when they are missing, and every
addition is recorded.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_VERSION = re.compile(r"^\s*Version\s*,\s*([0-9.]+)\s*;", re.I | re.M)
_SQLITE = re.compile(r"^(\s*Output:SQLite\s*,\s*)([A-Za-z]+)(?=\s*[,;])", re.I | re.M)
_SUMMARY = re.compile(r"^\s*Output:Table:SummaryReports\s*,([^;]*);", re.I | re.M)
_METER = r"^\s*Output:Meter\s*,\s*{name}\s*,\s*Hourly\s*;"
_BUILDING = re.compile(r"^\s*Building\s*,", re.I | re.M)
REQUIRED_METERS = ("Electricity:Facility", "NaturalGas:Facility")
SUPPLIED_HVAC = "supplied model"


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("!", 1)[0] for line in text.splitlines())


def _major_minor(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    parts = version.split(".")
    return ".".join(parts[:2])


def prepare_supplied_model(src: str, dst: str, energyplus_version: Optional[str] = None) -> Dict[str, Any]:
    """Copy a user's IDF to `dst`, adding the outputs aggregation needs.

    Returns what was added and any warnings. Raises ValueError for a file that
    is not an EnergyPlus model."""
    path = Path(src)
    if not path.is_file():
        raise FileNotFoundError(f"Model file not found: {src}")
    if path.suffix.lower() != ".idf":
        raise ValueError(f"{path.name}: only EnergyPlus .idf files can be used (convert .osm files with "
                         f"OpenStudio first)")
    text = path.read_text(encoding="utf-8", errors="replace")
    code = _strip_comments(text)
    if not _BUILDING.search(code):
        raise ValueError(f"{path.name} has no Building object; is it a complete EnergyPlus model?")

    warnings: List[str] = []
    added: List[str] = []
    version = (_VERSION.search(code) or [None, None])[1]
    if energyplus_version and _major_minor(version) != _major_minor(energyplus_version):
        warnings.append(f"{path.name} is for EnergyPlus {version or 'an unknown version'}, the server runs "
                        f"{energyplus_version}; convert it with IDFVersionUpdater or it may fail")

    extra: List[str] = []
    sqlite = _SQLITE.search(code)
    if not sqlite:
        extra.append("  Output:SQLite, SimpleAndTabular;")
        added.append("Output:SQLite")
    elif sqlite.group(2).lower() != "simpleandtabular":
        text = _SQLITE.sub(lambda m: m.group(1) + "SimpleAndTabular", text, count=1)
        added.append("Output:SQLite option SimpleAndTabular")
    summary = _SUMMARY.search(code)
    if not summary:
        extra.append("  Output:Table:SummaryReports, AllSummary;")
        added.append("Output:Table:SummaryReports")
    elif not re.search(r"AllSummary|AnnualBuildingUtilityPerformanceSummary", summary.group(1), re.I):
        warnings.append(f"{path.name}: Output:Table:SummaryReports lacks AnnualBuildingUtilityPerformanceSummary;"
                        f" annual end uses will be missing")
    for meter in REQUIRED_METERS:
        if not re.search(_METER.format(name=re.escape(meter)), code, re.I | re.M):
            extra.append(f"  Output:Meter, {meter}, Hourly;")
            added.append(f"Output:Meter {meter} hourly")

    header = (f"! Supplied model {path.name}, used by UBEM-MCP without changes"
              + (f" except these added outputs: {', '.join(added)}." if added else ".") + "\n")
    body = text if text.endswith("\n") else text + "\n"
    if extra:
        body += "\n! Outputs added by UBEM-MCP for district aggregation\n" + "\n".join(extra) + "\n"
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(header + body, encoding="utf-8")
    return {"energyplus_version": version, "outputs_added": added, "warnings": warnings}


def supplied_entry(building: Any, idf_path: str, src: str, src_sha256: Optional[str],
                   info: Dict[str, Any], license: Optional[str]) -> Dict[str, Any]:
    """Manifest entry for a supplied model. Stock attributes are kept for
    grouping; the model's own floor area is what results report."""
    gfa = building.gross_floor_area_m2
    return {
        "building_id": building.building_id,
        "idf_path": idf_path,
        "archetype": building.archetype,
        "vintage": building.vintage,
        "num_floors": building.num_floors,
        "gross_floor_area_m2": round(gfa, 1) if gfa else None,
        "hvac_system": SUPPLIED_HVAC,
        "model_source": "supplied",
        "supplied_from": src,
        "supplied_sha256": src_sha256,
        "supplied_license": license,
        "energyplus_version": info.get("energyplus_version"),
        "outputs_added": info.get("outputs_added", []),
        "inferred_fields": [],
        "notes": ["model supplied by the user; archetype assumptions do not apply"],
        "assumptions": {},
    }
