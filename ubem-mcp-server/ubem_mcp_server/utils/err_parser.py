"""
Read EnergyPlus .err files — new in UBEM-MCP. See NOTICE.md.

Turns an .err file into counts and the first severe and fatal messages, and
groups failures across a batch by cause so that one broken input reported by
hundreds of buildings shows up as one problem with a suggested fix.
"""

import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_MSG = re.compile(r"^\s*\*\*\s*(Warning|Severe|Fatal)\s*\*\*\s?(.*)$")
_CONT = re.compile(r"^\s*\*\*\s+~~~\s+\*\*\s?(.*)$")
_END = re.compile(r"EnergyPlus (Completed Successfully|Terminated--Fatal Error Detected)"
                  r".*?(\d+)\s+Warning;\s+(\d+)\s+Severe Errors")
MAX_MESSAGES = 5

# (pattern in the message, advice). The first match wins.
HINTS = [
    (re.compile(r"weather file|\.epw", re.I),
     "check that the weather file exists and is visible to the server (container paths)"),
    (re.compile(r"Version: in IDF=|IDF version|version mismatch", re.I),
     "the model was written for another EnergyPlus version; convert it with IDFVersionUpdater"),
    (re.compile(r"HVACTemplate|ExpandObjects", re.I),
     "an HVACTemplate object could not be expanded; check its fields or run with expandobjects"),
    (re.compile(r"Surface|Vertex|vertices|GetSurfaceData|Zone.*volume", re.I),
     "geometry problem in the model (surfaces, vertices or zone volume)"),
    (re.compile(r"Schedule", re.I), "a schedule is missing or malformed"),
    (re.compile(r"Construction|Material", re.I), "a construction or material is missing or invalid"),
    (re.compile(r"Node|Branch|Loop|Air ?Loop|Plant", re.I), "HVAC topology problem (nodes, branches or loops)"),
    (re.compile(r"not found|could not find|missing", re.I), "an input refers to an object or file that does not exist"),
]


def parse_err_text(text: str) -> Dict[str, Any]:
    """Counts and the first severe/fatal messages of one .err file."""
    messages: List[Dict[str, str]] = []
    warnings = severe = 0
    fatal = False
    current: Optional[Dict[str, str]] = None
    for line in text.splitlines():
        m = _MSG.match(line)
        if m:
            level, body = m.group(1), m.group(2).strip()
            current = None
            if level == "Warning":
                warnings += 1
            elif level == "Severe":
                severe += 1
            else:
                fatal = True
            if level != "Warning" and len(messages) < MAX_MESSAGES:
                current = {"level": level.lower(), "message": body}
                messages.append(current)
            continue
        c = _CONT.match(line)
        if c and current is not None and len(current["message"]) < 500:
            current["message"] = (current["message"] + " " + c.group(1).strip()).strip()
    out: Dict[str, Any] = {"completed": None, "warnings": warnings, "severe": severe, "fatal": fatal,
                           "messages": messages}
    end = _END.search(text)
    if end:
        out["completed"] = end.group(1) == "Completed Successfully"
        out["warnings"], out["severe"] = int(end.group(2)), int(end.group(3))
    return out


def parse_err_file(path: str) -> Optional[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return None
    return parse_err_text(p.read_text(errors="replace"))


def cause_key(message: str) -> str:
    """A message with building-specific names and numbers removed, so the
    same problem in different buildings groups together."""
    s = re.sub(r'"[^"]*"|\[[^\]]*\]', "…", message)
    s = re.sub(r"=\s*[^\s,;]+", "=…", s)
    s = re.sub(r"\d+(\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:200]


def hint_for(message: str) -> Optional[str]:
    for pattern, advice in HINTS:
        if pattern.search(message):
            return advice
    return None


def group_failures(failures: Iterable[Dict[str, Any]], max_groups: int = 5) -> List[Dict[str, Any]]:
    """Group failed runs ({"label", "error", "err_summary"}) by first cause,
    largest group first."""
    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for f in failures:
        summary = f.get("err_summary") or {}
        msgs = summary.get("messages") or []
        first = next((m["message"] for m in msgs if m["level"] == "severe"), None) \
            or (msgs[0]["message"] if msgs else None) or f.get("error") or "unknown error"
        key = cause_key(first)
        g = groups.setdefault(key, {"cause": key, "example_message": first, "count": 0, "examples": [],
                                    "hint": hint_for(first)})
        g["count"] += 1
        if len(g["examples"]) < 3:
            g["examples"].append(f.get("label"))
    return sorted(groups.values(), key=lambda g: -g["count"])[:max_groups]
