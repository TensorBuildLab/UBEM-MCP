"""
UBEM-MCP on the command line — new in UBEM-MCP. See NOTICE.md.

Every MCP tool is a subcommand, with options built from the tool's own
argument schema, and runs the same server code an agent's call runs:

    ubem-mcp tools                               # list tools
    ubem-mcp help load_building_stock            # one tool's description and options
    ubem-mcp guide stock-to-district             # a workflow guide
    ubem-mcp load_building_stock --geojson-path campus.geojson --stock-id campus
    ubem-mcp simulate_building_stock --stock-id campus --max-buildings 10
    ubem-mcp aggregate_stock_results --batch-id 1a2b3c4d5e6f
    ubem-mcp serve                               # run the MCP server

Results are printed as JSON on stdout (or written with --output). A command
that starts simulations waits for them, printing progress on stderr, since
the batch would stop when the process exits; --no-wait returns at once.
Arrays and objects are given as JSON, e.g. --field-map '{"year_built": "YR"}';
--args '{...}' passes all arguments as one JSON object.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

_TYPES = {"string": "text", "integer": "integer", "number": "number", "boolean": "true|false",
          "array": "JSON list", "object": "JSON object"}


def _schema_type(prop: Dict[str, Any]) -> str:
    if "type" in prop:
        return prop["type"]
    for alt in prop.get("anyOf") or []:
        if alt.get("type") and alt["type"] != "null":
            return alt["type"]
    return "string"


def _convert(value: str, kind: str, name: str) -> Any:
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
        if kind == "boolean":
            low = value.lower()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False
            raise ValueError(value)
        if kind in ("array", "object"):
            try:
                return json.loads(value)
            except ValueError:
                if kind == "array":  # plain comma list for lists of names
                    return [v.strip() for v in value.split(",") if v.strip()]
                raise
    except ValueError:
        raise SystemExit(f"--{name.replace('_', '-')}: expected {_TYPES.get(kind, kind)}, got {value!r}")
    return value


def _tool_parser(tool) -> argparse.ArgumentParser:
    schema = tool.inputSchema or {}
    required = set(schema.get("required") or [])
    p = argparse.ArgumentParser(prog=f"ubem-mcp {tool.name}", description=tool.description,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, prop in (schema.get("properties") or {}).items():
        kind = _schema_type(prop)
        flags = [f"--{name.replace('_', '-')}"] + ([f"--{name}"] if "_" in name else [])
        default = prop.get("default")
        hint = _TYPES.get(kind, kind) + (" (required)" if name in required else
                                         f" (default: {json.dumps(default)})" if "default" in prop else "")
        p.add_argument(*flags, dest=name, metavar=kind.upper(), help=hint)
    p.add_argument("--args", dest="_args", metavar="JSON", help="all arguments as one JSON object")
    p.add_argument("--output", "-o", dest="_output", metavar="FILE", help="write the JSON result to a file")
    p.add_argument("--no-wait", dest="_no_wait", action="store_true",
                   help="return as soon as simulations start (they stop when this process exits)")
    p.add_argument("--poll", dest="_poll", type=float, default=10.0, help="seconds between progress checks")
    return p


def _print_progress(status: Dict[str, Any]) -> None:
    counts = ", ".join(f"{k} {v}" for k, v in sorted((status.get("counts") or {}).items()))
    prog = status.get("progress") or {}
    eta = prog.get("estimated_remaining_seconds")
    print(f"  batch {status.get('batch_id')}: {status.get('status')} ({counts})"
          + (f", about {eta} s left" if eta else ""), file=sys.stderr)


def _emit(result: Dict[str, Any], output: Optional[str]) -> None:
    text = json.dumps(result, indent=2, default=str)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {output}", file=sys.stderr)
    else:
        print(text)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "--verbose" in argv
    if verbose:
        argv.remove("--verbose")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command = argv[0]

    if command == "serve":
        from .server import main as serve
        serve()
        return 0

    if not verbose:
        os.environ.setdefault("UBEM_LOG_LEVEL", "WARNING")
    from .api import UBEM, batch_ids

    ubem = UBEM(raise_on_error=False)
    try:
        if command == "tools":
            for t in ubem.tools():
                print(f"{t['name']:32s} {t['description']}")
            return 0
        if command == "help":
            if len(argv) < 2:
                raise SystemExit("usage: ubem-mcp help <tool>")
            _tool_parser(ubem._tools[argv[1]]).print_help()
            return 0
        if command == "guide":
            result = ubem.call("get_workflow_guide", name=argv[1] if len(argv) > 1 else None)
            if "text" in result:
                print(result["text"])
            else:
                for g in result.get("guides", []):
                    print(f"{g['name']:20s} {g['use_case']:4s} {g['title']}")
            return 0
        if command not in ubem._tools:
            print(f"unknown command {command!r}; run 'ubem-mcp tools' to list them", file=sys.stderr)
            return 2

        tool = ubem._tools[command]
        ns = _tool_parser(tool).parse_args(argv[1:])
        props = (tool.inputSchema or {}).get("properties") or {}
        args: Dict[str, Any] = json.loads(ns._args) if ns._args else {}
        for name, prop in props.items():
            value = getattr(ns, name, None)
            if value is not None:
                args[name] = _convert(value, _schema_type(prop), name)
        missing = [n for n in (tool.inputSchema or {}).get("required") or [] if n not in args]
        if missing:
            raise SystemExit(f"missing required option(s): {', '.join('--' + m.replace('_', '-') for m in missing)}")

        result = ubem.call(command, **args)
        ids = [] if ns._no_wait else batch_ids(result)
        if ids:
            print(f"waiting for {len(ids)} batch(es); Ctrl+C stops them", file=sys.stderr)
            result["batch_status"] = {b: ubem.wait(b, poll_seconds=ns._poll, progress=_print_progress) for b in ids}
        _emit(result, ns._output)
        return 1 if result.get("ok") is False else 0
    finally:
        ubem.close()


if __name__ == "__main__":
    sys.exit(main())
