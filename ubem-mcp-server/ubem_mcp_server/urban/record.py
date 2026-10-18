"""
Project records: what was run, on which inputs, with which software — new in
UBEM-MCP. See NOTICE.md.

Every step that creates a stock or a model set stores a small "recipe" (the
operation, its arguments and the SHA-256 of each input file). A project record
gathers, for one stock, the recipes of its whole lineage (from the original
footprints through enrichment and archetype assignment), its weather, every
model set with a hash of each generated model, the aggregated results and the
software versions. A record can be rerun: the same steps are repeated on the
same files and the regenerated models are compared with the recorded hashes.
"""

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

RECORD_VERSION = 1
_PACKAGES = ("mcp", "eppy", "shapely", "numpy", "pandas", "pyproj")
_LIBRARY_FILES = ("reference_buildings.json", "residential_prototype.json")


def sha256_file(path: str) -> Optional[str]:
    """SHA-256 of a file, or None if it does not exist."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_entry(role: str, path: Optional[str]) -> Dict[str, Any]:
    return {"role": role, "path": path, "sha256": sha256_file(path) if path else None}


def recipe(op: str, args: Dict[str, Any], inputs: Iterable[Dict[str, Any]] = (),
           parent: Optional[str] = None) -> Dict[str, Any]:
    out = {"op": op, "args": args, "inputs": list(inputs)}
    if parent:
        out["parent"] = parent
    return out


def combined_hash(hashes: Dict[str, Optional[str]]) -> str:
    """One hash over a {name: sha256} map, independent of order."""
    h = hashlib.sha256()
    for key in sorted(hashes):
        h.update(f"{key}:{hashes[key]}\n".encode())
    return h.hexdigest()


def _idd_header(idd_path: str) -> Dict[str, Optional[str]]:
    version = build = None
    try:
        with open(idd_path, encoding="latin-1") as f:
            for _ in range(5):
                line = f.readline()
                if line.startswith("!IDD_Version"):
                    version = line.split()[1]
                elif line.startswith("!IDD_BUILD"):
                    build = line.split()[1]
    except OSError:
        pass
    return {"version": version, "build": build}


@lru_cache(maxsize=4)
def _energyplus_cli_version(executable: str) -> Optional[str]:
    if not executable:
        return None
    try:
        out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else None
    except (OSError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=1)
def archetype_library() -> Dict[str, Any]:
    """Which archetype library this server bundles: titles and file hashes."""
    data = Path(__file__).parent / "data"
    files = {name: sha256_file(str(data / name)) for name in _LIBRARY_FILES}
    titles = {}
    for name in _LIBRARY_FILES:
        try:
            titles[name] = (json.loads((data / name).read_text(encoding="utf-8")).get("source") or {}).get("title")
        except (OSError, ValueError):
            titles[name] = None
    return {"files": files, "titles": titles, "sha256": combined_hash(files)}


def software_versions(idd_path: str, executable: str = "") -> Dict[str, Any]:
    from .. import __version__

    packages = {}
    for name in _PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    idd = _idd_header(idd_path)
    return {
        "ubem_mcp": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": packages,
        "energyplus": {"idd_version": idd["version"], "idd_build": idd["build"],
                       "cli": _energyplus_cli_version(executable)},
        "archetype_library": archetype_library(),
    }


def compare_software(recorded: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Human-readable differences that could change results."""
    diffs = []
    pairs = [
        ("UBEM-MCP", recorded.get("ubem_mcp"), current.get("ubem_mcp")),
        ("EnergyPlus", (recorded.get("energyplus") or {}).get("idd_version"),
         (current.get("energyplus") or {}).get("idd_version")),
        ("EnergyPlus build", (recorded.get("energyplus") or {}).get("idd_build"),
         (current.get("energyplus") or {}).get("idd_build")),
        ("archetype library", (recorded.get("archetype_library") or {}).get("sha256"),
         (current.get("archetype_library") or {}).get("sha256")),
    ]
    for label, old, new in pairs:
        if old != new:
            diffs.append(f"{label} differs: recorded {old}, now {new}")
    return diffs


def results_entry(results_dir: Path) -> Dict[str, Any]:
    summary_path = results_dir / "summary.json"
    entry: Dict[str, Any] = {"results_id": results_dir.name, "path": str(results_dir),
                             "summary_sha256": sha256_file(str(summary_path))}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        entry["created_at"] = summary.get("created_at")
        entry["district"] = summary.get("district")
        entry["coverage"] = summary.get("coverage")
    except (OSError, ValueError):
        pass
    return entry


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
