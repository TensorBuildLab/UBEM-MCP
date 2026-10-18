"""
UBEM-MCP as a Python library — new in UBEM-MCP. See NOTICE.md.

Every MCP tool is a method of `UBEM`, with the same arguments and the same
result as when an agent calls it over MCP: the call goes through the same
server code. Use it in scripts, notebooks and pipelines, with no agent.

    from ubem_mcp_server.api import UBEM

    with UBEM(workspace="my_project") as ubem:
        ubem.load_building_stock(geojson_path="campus.geojson", stock_id="campus")
        run = ubem.simulate_building_stock(stock_id="campus", max_buildings=10)
        ubem.wait(run["batch"]["batch_id"])
        summary = ubem.aggregate_stock_results(batch_id=run["batch"]["batch_id"])

Simulations run in the background on an event loop that lives as long as the
`UBEM` object, so a script can start a batch, do other work and come back.
One `UBEM` object per process: it configures the server's shared state.
"""

import asyncio
import json
import os
import threading
import time
from functools import partial
from typing import Any, Callable, Dict, List, Optional


class UBEMError(RuntimeError):
    """A tool answered ok=false. `error_type` is not_found, invalid_input or internal."""

    def __init__(self, tool: str, error_type: Optional[str], error: Optional[str], result: Dict[str, Any]):
        super().__init__(f"{tool}: {error_type}: {error}")
        self.tool, self.error_type, self.error, self.result = tool, error_type, error, result


def unwrap(structured: Any) -> Dict[str, Any]:
    """Plain dict from a tool result. Tools inherited from EnergyPlus-MCP
    return text, which comes back as {"text": ...}."""
    if isinstance(structured, dict) and set(structured) == {"result"} and isinstance(structured["result"], str):
        text = structured["result"]
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"text": text}
        except ValueError:
            return {"text": text}
    if isinstance(structured, dict):
        return structured
    return {"text": str(structured)}


class UBEM:
    """All UBEM-MCP tools as Python methods.

    Args:
        workspace: Folder that relative input paths are resolved against
                   (default: UBEM_WORKSPACE_ROOT or the server default)
        output_dir: Where stocks, models, batches and results are written
                    (default: <workspace>/outputs)
        max_workers: Parallel EnergyPlus processes for batches
        raise_on_error: Raise UBEMError when a tool answers ok=false
                        (default) instead of returning the result
    """

    def __init__(self, workspace: Optional[str] = None, output_dir: Optional[str] = None,
                 max_workers: Optional[int] = None, raise_on_error: bool = True):
        if workspace:
            os.environ["UBEM_WORKSPACE_ROOT"] = os.path.abspath(workspace)
        if output_dir:
            os.environ["UBEM_OUTPUT_DIR"] = os.path.abspath(output_dir)
        if max_workers:
            os.environ["UBEM_BATCH_MAX_WORKERS"] = str(max_workers)

        from . import server
        from .batch_manager import BatchManager
        from .config import Config
        from .energyplus_tools import EnergyPlusManager
        from .urban.stock_manager import StockManager

        cfg = Config()
        server.config = cfg
        server.ep_manager = EnergyPlusManager(cfg)
        server.batch_manager = BatchManager(cfg)
        server.stock_manager = StockManager(cfg)
        self.config = cfg
        self._server = server
        self.raise_on_error = raise_on_error
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="ubem-mcp-loop", daemon=True)
        self._thread.start()
        self._tools = {t.name: t for t in self._run(server.mcp.list_tools())}

    # -- calling tools ------------------------------------------------------

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def call(self, tool: str, **arguments: Any) -> Dict[str, Any]:
        """Call a tool by name with keyword arguments; returns its result as a dict."""
        if tool not in self._tools:
            raise AttributeError(f"No tool {tool!r}; see UBEM.tools()")
        args = {k: v for k, v in arguments.items() if v is not None}
        out = self._run(self._server.mcp.call_tool(tool, args))
        structured = out[1] if isinstance(out, tuple) else out
        result = unwrap(structured)
        if self.raise_on_error and result.get("ok") is False:
            raise UBEMError(tool, result.get("error_type"), result.get("error"), result)
        return result

    def __getattr__(self, name: str) -> Callable[..., Dict[str, Any]]:
        tools = self.__dict__.get("_tools") or {}
        if name in tools:
            fn = partial(self.call, name)
            fn.__doc__ = tools[name].description
            return fn
        raise AttributeError(name)

    def __dir__(self) -> List[str]:
        return sorted(set(super().__dir__()) | set(self._tools))

    def tools(self) -> List[Dict[str, str]]:
        """Name and first line of the description of every tool."""
        return [{"name": n, "description": (t.description or "").strip().splitlines()[0]}
                for n, t in sorted(self._tools.items())]

    def describe(self, tool: str) -> Dict[str, Any]:
        """A tool's full description and argument schema."""
        t = self._tools[tool]
        return {"name": t.name, "description": t.description, "input_schema": t.inputSchema,
                "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None}

    # -- batches ------------------------------------------------------------

    def wait(self, batch_id: str, poll_seconds: float = 5.0, timeout: Optional[float] = None,
             progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """Block until a batch completes; returns its final status.
        `progress(status)` is called after every poll."""
        start = time.monotonic()
        while True:
            status = self.call("get_batch_status", batch_id=batch_id)
            if progress:
                progress(status)
            if status.get("is_complete"):
                return status
            if timeout is not None and time.monotonic() - start > timeout:
                raise TimeoutError(f"batch {batch_id} not complete after {timeout} s")
            time.sleep(poll_seconds)

    # -- lifetime -----------------------------------------------------------

    def close(self) -> None:
        """Stop the background event loop (running batches are abandoned)."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    def __enter__(self) -> "UBEM":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def batch_ids(result: Dict[str, Any]) -> List[str]:
    """Batch ids started by a tool call (simulate_*, run_batch_simulation, rerun_project_record)."""
    ids = []
    batch = result.get("batch")
    if isinstance(batch, dict) and batch.get("batch_id"):
        ids.append(batch["batch_id"])
    if result.get("batch_id") and result.get("is_complete") is False:
        ids.append(result["batch_id"])
    ids += [b["batch_id"] for b in result.get("batches") or [] if isinstance(b, dict) and b.get("batch_id")]
    return ids
