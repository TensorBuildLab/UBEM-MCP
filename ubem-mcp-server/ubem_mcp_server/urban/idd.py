"""
Minimal EnergyPlus IDD reader, used to write IDF objects by field name.

Writing fields by name, ordered by the installed EnergyPlus's own data
dictionary, keeps generated objects correct when field lists change between
EnergyPlus versions (and avoids positional mistakes in long objects such as
the HVACTemplate family).
"""

import re
from functools import lru_cache
from typing import Dict, List, Optional, Sequence


class IDD:
    def __init__(self, classes: Dict[str, List[str]], version: Optional[str]):
        self._classes = classes  # lower-case class name -> ordered field names
        self.version = version

    def fields(self, class_name: str) -> List[str]:
        try:
            return self._classes[class_name.lower()]
        except KeyError:
            raise KeyError(f"Object class {class_name!r} not in EnergyPlus {self.version} IDD") from None

    def has(self, class_name: str) -> bool:
        return class_name.lower() in self._classes


@lru_cache(maxsize=4)
def load_idd(path: str) -> IDD:
    classes: Dict[str, List[str]] = {}
    version = None
    current: Optional[List[str]] = None
    with open(path, encoding="latin-1") as f:
        for line in f:
            if version is None and line.startswith("!IDD_Version"):
                version = line.split()[1].strip()
            stripped = line.strip()
            if not stripped or stripped.startswith("!"):
                continue
            if not line[0].isspace() and not stripped.startswith("\\"):
                # A class header such as "Zone," (or "Lead Input;" for empty classes).
                name = stripped.rstrip(",;").strip()
                current = classes.setdefault(name.lower(), [])
                continue
            if current is None:
                continue
            m = re.search(r"\\field\s+(.*)$", stripped)
            if m:
                current.append(m.group(1).strip())
    return IDD(classes, version)


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class IDFWriter:
    """Accumulates IDF objects as text."""

    def __init__(self, idd: IDD):
        self.idd = idd
        self._objects: List[str] = []

    def add(self, class_name: str, fields: Optional[Dict[str, object]] = None,
            extensible: Sequence[object] = ()) -> None:
        """Add an object; `fields` maps IDD field names to values, unnamed
        fields are left blank (EnergyPlus defaults). `extensible` values
        (e.g. vertex coordinates) are appended after the named fields."""
        names = self.idd.fields(class_name)
        fields = dict(fields or {})
        lower = {n.lower(): i for i, n in enumerate(names)}
        values: List[str] = []
        last = -1
        for key, value in fields.items():
            idx = lower.get(key.lower())
            if idx is None:
                raise KeyError(f"{class_name} has no field {key!r}")
            while len(values) <= idx:
                values.append("")
            values[idx] = _fmt(value)
            last = max(last, idx)
        values = values[: last + 1]
        if extensible:
            values += [_fmt(v) for v in extensible]
        self._raw(class_name, values, [names[i] if i < len(names) else "" for i in range(len(values))])

    def add_raw(self, class_name: str, values: Sequence[object]) -> None:
        """Add an object from positional values (for simple, stable objects)."""
        self._raw(class_name, [_fmt(v) for v in values], [""] * len(values))

    def _raw(self, class_name: str, values: List[str], comments: List[str]) -> None:
        if not values:
            self._objects.append(f"{class_name};\n")
            return
        lines = [f"{class_name},"]
        for i, (v, c) in enumerate(zip(values, comments, strict=True)):
            end = ";" if i == len(values) - 1 else ","
            text = f"  {v}{end}"
            lines.append(f"{text:<40}!- {c}" if c else text)
        self._objects.append("\n".join(lines) + "\n")

    def text(self) -> str:
        return "\n".join(self._objects)
