"""
Workflow guides shipped inside UBEM-MCP — new in UBEM-MCP. See NOTICE.md.

Each guide is a Markdown file in this folder with a small front matter
(title, use_case, description). The server offers every guide three ways, so
that any MCP client can use them: as an MCP prompt, as an MCP resource
(ubem://guides/<name>), and through the get_workflow_guide tool for clients
that support tools only. Nothing depends on one vendor's client features.
"""

from functools import lru_cache
from pathlib import Path
from typing import Dict, List

GUIDE_DIR = Path(__file__).parent


@lru_cache(maxsize=1)
def _load() -> Dict[str, Dict[str, str]]:
    guides: Dict[str, Dict[str, str]] = {}
    for path in sorted(GUIDE_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta: Dict[str, str] = {}
        body = text
        if text.startswith("---\n"):
            head, _, body = text[4:].partition("\n---\n")
            for line in head.splitlines():
                key, _, value = line.partition(":")
                if key.strip():
                    meta[key.strip()] = value.strip()
        guides[path.stem] = {"name": path.stem, "title": meta.get("title", path.stem),
                             "use_case": meta.get("use_case", ""), "description": meta.get("description", ""),
                             "text": body.strip() + "\n"}
    return guides


def list_guides() -> List[Dict[str, str]]:
    return [{k: g[k] for k in ("name", "title", "use_case", "description")} for g in _load().values()]


def get_guide(name: str) -> Dict[str, str]:
    guides = _load()
    if name not in guides:
        raise KeyError(f"Unknown guide {name!r}; available: {', '.join(guides)}")
    return guides[name]
