"""
Users' own building models inside a model set — new in UBEM-MCP. See NOTICE.md.

A model set mixes archetype models with complete EnergyPlus models supplied by
the user (from OpenStudio, a consultant, a calibration study or another UBEM
tool). A supplied model is copied, not edited, except that a model made for an
older EnergyPlus is upgraded with EnergyPlus's own transition programs and
the outputs that district aggregation reads are added when they are missing.
Every change is recorded.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


_TRANSITION = re.compile(r"^Transition-V(\d+)-(\d+)-\d+-to-V(\d+)-(\d+)-\d+$")


def _key(version: str) -> Tuple[int, int]:
    major, minor = (version.split(".") + ["0"])[:2]
    return int(major), int(minor)


def transition_chain(transition_dir: str, from_version: str, to_version: str) -> List[Path]:
    """The EnergyPlus transition programs that take a model from one version to
    another, in order. Raises ValueError when the chain is incomplete."""
    steps = {}
    for exe in Path(transition_dir).glob("Transition-V*"):
        m = _TRANSITION.match(exe.name)
        if m:
            steps[(int(m[1]), int(m[2]))] = ((int(m[3]), int(m[4])), exe)
    chain, at, goal = [], _key(from_version), _key(to_version)
    while at < goal:
        if at not in steps:
            raise ValueError(f"No EnergyPlus transition program from version {at[0]}.{at[1]} in {transition_dir}")
        at, exe = steps[at]
        chain.append(exe)
    return chain


def upgrade_idf(path: Path, from_version: str, to_version: str, transition_dir: str) -> Dict[str, Any]:
    """Upgrade an IDF in place by running the transition programs one by one,
    as EnergyPlus's IDFVersionUpdater does. Returns the steps and the first
    transition notes (from the .VCpErr files)."""
    chain = transition_chain(transition_dir, from_version, to_version)
    notes: List[str] = []
    with tempfile.TemporaryDirectory(prefix="ubem_transition_") as tmp:
        work = Path(tmp) / "model.idf"
        shutil.copyfile(path, work)
        for exe in chain:
            proc = subprocess.run([str(exe), str(work)], cwd=transition_dir, capture_output=True, text=True,
                                  timeout=600)
            text = work.read_text(encoding="utf-8", errors="replace")
            new = (_VERSION.search(_strip_comments(text)) or [None, ""])[1]
            if proc.returncode != 0 or _major_minor(new) == _major_minor(from_version):
                raise ValueError(f"{exe.name} could not upgrade {path.name}: {(proc.stdout + proc.stderr)[-300:]}")
            err = work.with_suffix(".VCpErr")
            if err.exists():
                notes += [line.strip() for line in err.read_text(errors="replace").splitlines()
                          if line.strip() and not line.startswith(("Program Version", " Conversion"))][:5]
                err.unlink()
            from_version = new
        shutil.copyfile(work, path)
    return {"steps": [exe.name for exe in chain], "notes": notes[:10]}


def prepare_supplied_model(src: str, dst: str, energyplus_version: Optional[str] = None,
                           transition_dir: Optional[str] = None) -> Dict[str, Any]:
    """Copy a user's IDF to `dst`, upgrading it to the server's EnergyPlus
    version when transition_dir holds the transition programs, and adding the
    outputs aggregation needs.

    Returns what was changed and any warnings. Raises ValueError for a file
    that is not an EnergyPlus model."""
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
    upgraded = None
    version = (_VERSION.search(code) or [None, None])[1]
    if energyplus_version and _major_minor(version) != _major_minor(energyplus_version):
        older = version is not None and _key(version) < _key(energyplus_version)
        if older and transition_dir and Path(transition_dir).is_dir():
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            staged = Path(dst).with_suffix(".source.idf")
            shutil.copyfile(path, staged)
            try:
                steps = upgrade_idf(staged, version, energyplus_version, transition_dir)
                text = staged.read_text(encoding="utf-8", errors="replace")
            finally:
                staged.unlink(missing_ok=True)
            code = _strip_comments(text)
            upgraded = {"from": version, "to": _major_minor(energyplus_version), **steps}
            warnings.append(f"{path.name} was upgraded from EnergyPlus {version} to "
                            f"{_major_minor(energyplus_version)} with the EnergyPlus transition programs; "
                            f"check its results")
        elif older:
            warnings.append(f"{path.name} is for EnergyPlus {version}, the server runs {energyplus_version}; "
                            f"convert it with IDFVersionUpdater or it may fail")
        else:
            warnings.append(f"{path.name} is for EnergyPlus {version or 'an unknown version'}, the server runs "
                            f"{energyplus_version}; a model for a newer or unknown version may fail")

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

    changes = ([f"upgraded from EnergyPlus {upgraded['from']} to {upgraded['to']}"] if upgraded else []) + \
        ([f"added outputs: {', '.join(added)}"] if added else [])
    header = (f"! Supplied model {path.name}, used by UBEM-MCP without changes"
              + (f" except: {'; '.join(changes)}." if changes else ".") + "\n")
    body = text if text.endswith("\n") else text + "\n"
    if extra:
        body += "\n! Outputs added by UBEM-MCP for district aggregation\n" + "\n".join(extra) + "\n"
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(header + body, encoding="utf-8")
    return {"energyplus_version": version, "upgraded": upgraded, "outputs_added": added, "warnings": warnings}


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
        "upgraded": info.get("upgraded"),
        "outputs_added": info.get("outputs_added", []),
        "inferred_fields": [],
        "notes": ["model supplied by the user; archetype assumptions do not apply"],
        "assumptions": {},
    }
