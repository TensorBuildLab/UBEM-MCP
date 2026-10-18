"""
Batch simulation orchestration for UBEM-MCP.

This module is new to UBEM-MCP (not part of EnergyPlus-MCP, which only runs
one IDF at a time). See NOTICE.md for the relationship to EnergyPlus-MCP.

Design notes:
- Each model in a batch runs in its own worker process (ProcessPoolExecutor),
  which calls the EnergyPlus command line directly rather than going through
  eppy (see run_energyplus_cli).
- A batch can take minutes to hours, which would exceed typical MCP client
  call timeouts if a single tool call blocked until every model finished.
  submit_batch() returns a batch_id immediately; the batch runs as a
  background asyncio task, polled via get_status()/get_results().
"""

import asyncio
import json
import logging
import math
import multiprocessing
import os
import sys
import re
import subprocess
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import Config
from .utils.err_parser import group_failures, parse_err_file
from .utils.path_utils import resolve_idf_path, resolve_weather_file_path, get_file_info

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}
_TERMINAL_MODEL_STATUSES = {"success", "failed", "cancelled"}


_RealProcessPool = ProcessPoolExecutor


def _make_executor(workers: int):
    """Worker pool for EnergyPlus runs. Workers start with forkserver (Linux)
    or spawn rather than fork: the server process runs threads, and forking a
    threaded process can deadlock the child."""
    if ProcessPoolExecutor is _RealProcessPool:
        method = "forkserver" if sys.platform.startswith("linux") else "spawn"
        return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context(method))
    return ProcessPoolExecutor(max_workers=workers)  # replaced in tests


def _safe_name(name: str) -> str:
    """Sanitize a string for use as a directory name component."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", name or "").strip("_")
    return cleaned[:80] or "model"


@dataclass
class ModelSpec:
    """One building/model within a batch."""
    input_idf_path: str
    input_weather_file: Optional[str] = None
    label: str = ""
    resolved_idf_path: Optional[str] = None
    resolved_weather_file: Optional[str] = None
    output_directory: Optional[str] = None
    status: str = "queued"  # queued | running | success | failed | cancelled
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None  # EnergyPlus run time, without time spent waiting in the queue


@dataclass
class BatchJob:
    """A batch of models submitted together."""
    batch_id: str
    created_at: str
    status: str  # queued | running | completed | completed_with_errors | failed | cancelled
    max_workers: int
    continue_on_error: bool
    output_directory: str
    models: List[ModelSpec] = field(default_factory=list)
    sim_kwargs: Dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _run_single_model_worker(
    config: Config,
    idf_path: str,
    weather_file: Optional[str],
    output_directory: str,
    sim_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Entry point executed in a worker process. Must stay at module scope
    (not a method/lambda/closure) so ProcessPoolExecutor can pickle it.

    Runs the EnergyPlus command line directly. EnergyPlusManager.run_simulation
    loads the model with eppy and writes it back out before running, which
    for a typical generated building took about three times as long as the
    simulation itself; a batch runs files that are already complete.
    """
    try:
        return run_energyplus_cli(config, idf_path, weather_file, output_directory, **sim_kwargs)
    except Exception as e:  # noqa: BLE001 - worker boundary: always return a result, never raise
        return {
            "success": False,
            "input_idf": idf_path,
            "weather_file": weather_file,
            "output_directory": output_directory,
            "error": str(e),
        }


def run_energyplus_cli(
    config: Config,
    idf_path: str,
    weather_file: Optional[str],
    output_directory: str,
    annual: bool = True,
    design_day: bool = False,
    readvars: bool = True,
    expandobjects: bool = True,
) -> Dict[str, Any]:
    """Run one model with the EnergyPlus command line, using the same options
    and output naming as EnergyPlusManager.run_simulation."""
    # EnergyPlus runs inside the output directory, so every path must be absolute.
    output_directory = os.path.abspath(output_directory)
    idf_path = os.path.abspath(idf_path)
    weather_file = os.path.abspath(weather_file) if weather_file else None
    os.makedirs(output_directory, exist_ok=True)
    prefix = Path(idf_path).stem
    exe = config.energyplus.executable_path or "energyplus"
    cmd = [exe, "--output-directory", output_directory, "--output-prefix", prefix, "--output-suffix", "C"]
    if config.energyplus.idd_path:
        cmd += ["--idd", config.energyplus.idd_path]
    if weather_file:
        cmd += ["--weather", weather_file]
    if annual:
        cmd.append("--annual")
    if design_day:
        cmd.append("--design-day")
    if readvars:
        cmd.append("--readvars")
    if expandobjects:
        cmd.append("--expandobjects")
    cmd.append(idf_path)

    start = datetime.now()
    # ExpandObjects writes its intermediate files to the working directory,
    # so each run works in its own output directory.
    proc = subprocess.run(cmd, cwd=output_directory, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True, errors="replace")
    end = datetime.now()
    result: Dict[str, Any] = {
        "success": proc.returncode == 0,
        "input_idf": idf_path,
        "weather_file": weather_file,
        "output_directory": output_directory,
        "simulation_duration": str(end - start),
        "started_at": start.isoformat(),
        "simulation_seconds": round((end - start).total_seconds(), 2),
        "simulation_options": {"annual": annual, "design_day": design_day, "readvars": readvars,
                               "expandobjects": expandobjects, "output_prefix": prefix, "output_suffix": "C"},
        "output_files": sorted(p.name for p in Path(output_directory).iterdir() if p.is_file()),
        "timestamp": end.isoformat(),
    }
    err_file = Path(output_directory) / f"{prefix}.err"
    err_summary = parse_err_file(str(err_file))
    if err_summary is not None:
        result["err_summary"] = err_summary
    if proc.returncode != 0:
        details = err_file.read_text(errors="replace") if err_file.exists() else proc.stderr
        first = next((m["message"] for m in (err_summary or {}).get("messages", [])), None)
        result["error"] = f"EnergyPlus exited with code {proc.returncode}" + (f": {first}" if first else "")
        result["error_details"] = details[-4000:]
    return result


def discover_idf_files(config: Config, directory: str, pattern: str = "*.idf") -> List[Dict[str, Any]]:
    """Glob a directory for IDF files to use as batch model inputs."""
    search_dir = directory
    if not os.path.isabs(search_dir):
        candidate = os.path.join(config.paths.workspace_root, search_dir)
        if os.path.isdir(candidate):
            search_dir = candidate

    if not os.path.isdir(search_dir):
        raise FileNotFoundError(f"Directory not found: {directory}")

    matches = sorted(Path(search_dir).glob(pattern))
    return [
        {
            "idf_path": str(p.resolve()),
            "label": p.stem,
            **get_file_info(str(p)),
        }
        for p in matches
        if p.is_file()
    ]


class BatchManager:
    """Orchestrates batch (multi-model) EnergyPlus simulations."""

    def __init__(self, config: Config):
        self.config = config
        self._jobs: Dict[str, BatchJob] = {}

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #

    def submit_batch(
        self,
        models: List[Union[str, Dict[str, Any]]],
        weather_file: Optional[str] = None,
        max_workers: Optional[int] = None,
        continue_on_error: bool = True,
        output_directory: Optional[str] = None,
        annual: bool = True,
        design_day: bool = False,
        readvars: bool = True,
        expandobjects: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate and register a batch, then schedule it to run in the
        background. Returns immediately with a batch_id and initial status.
        `metadata` is stored with the batch (e.g. the stock and model set it
        simulates).
        """
        normalized = self._normalize_models(models)
        if not normalized:
            raise ValueError("models list cannot be empty")

        batch_id = uuid.uuid4().hex[:12]
        created_at = datetime.now().isoformat()
        manifest_dir = self._manifest_dir(batch_id)
        os.makedirs(manifest_dir, exist_ok=True)
        batch_root = output_directory or str(manifest_dir)
        os.makedirs(batch_root, exist_ok=True)

        model_specs: List[ModelSpec] = []
        for i, m in enumerate(normalized):
            label = m.get("label") or Path(m["idf_path"]).stem
            spec = ModelSpec(
                input_idf_path=m["idf_path"],
                input_weather_file=m.get("weather_file") or weather_file,
                label=label,
            )
            spec.output_directory = str(Path(batch_root) / f"{i:04d}_{_safe_name(label)}")

            try:
                spec.resolved_idf_path = resolve_idf_path(self.config, spec.input_idf_path)
                if spec.input_weather_file:
                    spec.resolved_weather_file = resolve_weather_file_path(
                        self.config, spec.input_weather_file
                    )
            except (FileNotFoundError, ValueError) as e:
                spec.status = "failed"
                spec.error = str(e)

            model_specs.append(spec)

        job = BatchJob(
            batch_id=batch_id,
            created_at=created_at,
            status="queued",
            max_workers=max_workers or self.config.batch.max_workers,
            continue_on_error=continue_on_error,
            output_directory=batch_root,
            models=model_specs,
            sim_kwargs={
                "annual": annual,
                "design_day": design_day,
                "readvars": readvars,
                "expandobjects": expandobjects,
            },
            metadata=dict(metadata or {}),
        )
        self._jobs[batch_id] = job
        self._save_manifest(job)

        asyncio.create_task(self._run_batch(batch_id))
        logger.info(
            "Batch %s submitted: %d model(s), max_workers=%d",
            batch_id, len(model_specs), job.max_workers,
        )
        return self._status_summary(job)

    def _normalize_models(self, models: List[Union[str, Dict[str, Any]]]) -> List[Dict[str, Any]]:
        normalized = []
        for m in models:
            if isinstance(m, str):
                normalized.append({"idf_path": m})
            elif isinstance(m, dict):
                if "idf_path" not in m:
                    raise ValueError(f"model spec missing 'idf_path': {m!r}")
                normalized.append(m)
            else:
                raise ValueError(f"Unsupported model spec type: {type(m)!r}")
        return normalized

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    async def _run_batch(self, batch_id: str) -> None:
        job = self._jobs[batch_id]
        job.status = "running"
        self._save_manifest(job)

        pending_specs = [m for m in job.models if m.status == "queued"]
        if not pending_specs:
            job.status = self._final_status(job)
            self._save_manifest(job)
            return

        loop = asyncio.get_running_loop()
        workers = max(1, job.max_workers)
        executor = _make_executor(workers)
        futures: Dict[asyncio.Future, ModelSpec] = {}
        in_flight: List[ModelSpec] = list(pending_specs)  # submission order = start order (FIFO pool)

        def mark_running():
            for spec in in_flight[:workers]:
                if spec.status == "queued":
                    spec.status = "running"
                    spec.started_at = datetime.now().isoformat()

        try:
            for spec in pending_specs:
                fut = loop.run_in_executor(
                    executor,
                    _run_single_model_worker,
                    self.config,
                    spec.resolved_idf_path,
                    spec.resolved_weather_file,
                    spec.output_directory,
                    job.sim_kwargs,
                )
                futures[fut] = spec
            mark_running()
            self._save_manifest(job)

            remaining = set(futures.keys())
            stop_early = False
            while remaining:
                done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    spec = futures[fut]
                    try:
                        result = fut.result()
                    except Exception as e:  # noqa: BLE001
                        result = {"success": False, "error": str(e)}
                    spec.completed_at = datetime.now().isoformat()
                    if result.get("started_at"):
                        spec.started_at = result["started_at"]
                    if result.get("simulation_seconds") is not None:
                        spec.duration_seconds = result["simulation_seconds"]
                    elif spec.started_at:
                        spec.duration_seconds = round(
                            (datetime.fromisoformat(spec.completed_at)
                             - datetime.fromisoformat(spec.started_at)).total_seconds(), 2)
                    if spec in in_flight:
                        in_flight.remove(spec)
                    spec.result = result
                    if result.get("success"):
                        spec.status = "success"
                    else:
                        spec.status = "failed"
                        spec.error = result.get("error", "simulation failed")
                        if not job.continue_on_error:
                            stop_early = True

                mark_running()
                if (stop_early or job.cancel_requested) and remaining:
                    for fut in remaining:
                        fut.cancel()
                        futures[fut].status = "cancelled"
                        futures[fut].completed_at = datetime.now().isoformat()
                    self._save_manifest(job)
                    break

                self._save_manifest(job)
        finally:
            # Best-effort: stops not-yet-started work; cannot kill an
            # EnergyPlus subprocess already in flight in a worker process.
            executor.shutdown(wait=False, cancel_futures=True)

        job.status = self._final_status(job)
        self._save_manifest(job)
        logger.info("Batch %s finished with status=%s", batch_id, job.status)

    def _final_status(self, job: BatchJob) -> str:
        statuses = [m.status for m in job.models]
        if not statuses:
            return "failed"
        if job.cancel_requested and any(s == "cancelled" for s in statuses):
            return "cancelled"
        if all(s == "success" for s in statuses):
            return "completed"
        if any(s == "success" for s in statuses):
            return "completed_with_errors"
        return "failed"

    # ------------------------------------------------------------------ #
    # Querying
    # ------------------------------------------------------------------ #

    def get_status(self, batch_id: str) -> Dict[str, Any]:
        job = self._get_job(batch_id)
        return self._status_summary(job)

    def get_results(self, batch_id: str) -> Dict[str, Any]:
        job = self._get_job(batch_id)
        summary = self._status_summary(job)
        summary["models"] = [asdict(m) for m in job.models]
        return summary

    def list_batches(self) -> List[Dict[str, Any]]:
        ids = set(self._jobs.keys())
        root = Path(self.config.batch.batch_output_dir)
        if root.exists():
            for d in root.iterdir():
                if d.is_dir() and (d / "manifest.json").exists():
                    ids.add(d.name)

        summaries = []
        for batch_id in sorted(ids):
            try:
                job = self._get_job(batch_id)
                summaries.append(self._status_summary(job))
            except Exception as e:  # noqa: BLE001
                logger.warning("Skipping unreadable batch %s: %s", batch_id, e)
        return summaries

    def cancel_batch(self, batch_id: str) -> Dict[str, Any]:
        job = self._get_job(batch_id)
        job.cancel_requested = True
        for spec in job.models:
            if spec.status == "queued":
                spec.status = "cancelled"
                spec.completed_at = datetime.now().isoformat()
        if job.status not in _TERMINAL_JOB_STATUSES:
            all_terminal = all(m.status in _TERMINAL_MODEL_STATUSES for m in job.models)
            if all_terminal:
                job.status = self._final_status(job)
        self._save_manifest(job)
        return self._status_summary(job)

    def _status_summary(self, job: BatchJob) -> Dict[str, Any]:
        counts = Counter(m.status for m in job.models)
        out = {
            "batch_id": job.batch_id,
            "status": job.status,
            "created_at": job.created_at,
            "total_models": len(job.models),
            "counts": dict(counts),
            "is_complete": job.status in _TERMINAL_JOB_STATUSES,
            "max_workers": job.max_workers,
            "continue_on_error": job.continue_on_error,
            "output_directory": job.output_directory,
            "metadata": job.metadata,
        }
        progress = self._progress(job, counts)
        if progress:
            out["progress"] = progress
        diagnosis = self._diagnosis(job)
        if diagnosis:
            out["diagnosis"] = diagnosis
        return out

    @staticmethod
    def _progress(job: BatchJob, counts: Counter) -> Optional[Dict[str, Any]]:
        """Elapsed time and, once some models have finished, an estimate of
        the time left from their mean EnergyPlus run time."""
        starts = [m.started_at for m in job.models if m.started_at]
        if not starts:
            return None
        done = [m for m in job.models if m.status in _TERMINAL_MODEL_STATUSES]
        ends = [m.completed_at for m in done if m.completed_at]
        last = max(ends) if job.status in _TERMINAL_JOB_STATUSES and ends else datetime.now().isoformat()
        elapsed = (datetime.fromisoformat(last) - datetime.fromisoformat(min(starts))).total_seconds()
        out: Dict[str, Any] = {"finished": len(done), "total": len(job.models), "elapsed_seconds": round(elapsed)}
        times = [m.duration_seconds for m in done if m.status == "success" and m.duration_seconds]
        if times:
            mean = sum(times) / len(times)
            out["mean_simulation_seconds"] = round(mean, 1)
            left = counts.get("queued", 0) + counts.get("running", 0)
            if left and job.status not in _TERMINAL_JOB_STATUSES:
                out["estimated_remaining_seconds"] = round(math.ceil(left / max(1, job.max_workers)) * mean)
        return out

    @staticmethod
    def _diagnosis(job: BatchJob) -> Optional[Dict[str, Any]]:
        """Failures grouped by cause, with a suggested fix, and successful runs
        that still reported severe errors."""
        failed = [{"label": m.label, "error": m.error, "err_summary": (m.result or {}).get("err_summary")}
                  for m in job.models if m.status == "failed"]
        severe_ok = [m.label for m in job.models if m.status == "success"
                     and ((m.result or {}).get("err_summary") or {}).get("severe")]
        if not failed and not severe_ok:
            return None
        out: Dict[str, Any] = {}
        if failed:
            out["failure_causes"] = group_failures(failed)
            out["next_step"] = ("read one failed model's error_details with get_batch_results(status='failed') "
                                "and fix the cause shared by the largest group first")
        if severe_ok:
            out["completed_with_severe_errors"] = {"count": len(severe_ok), "examples": severe_ok[:5]}
        return out

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _manifest_dir(self, batch_id: str) -> Path:
        return Path(self.config.batch.batch_output_dir) / batch_id

    def _manifest_path(self, batch_id: str) -> Path:
        return self._manifest_dir(batch_id) / "manifest.json"

    def _save_manifest(self, job: BatchJob) -> None:
        path = self._manifest_path(job.batch_id)
        try:
            os.makedirs(path.parent, exist_ok=True)
            with open(path, "w") as f:
                json.dump(asdict(job), f, indent=2, default=str)
        except OSError as e:
            logger.warning("Could not persist batch manifest %s: %s", path, e)

    def _load_manifest(self, batch_id: str) -> Optional[BatchJob]:
        path = self._manifest_path(batch_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not read batch manifest %s: %s", path, e)
            return None

        try:
            models = [ModelSpec(**m) for m in data.pop("models", [])]
            return BatchJob(models=models, **data)
        except TypeError as e:
            logger.warning("Malformed batch manifest %s: %s", path, e)
            return None

    def _get_job(self, batch_id: str) -> BatchJob:
        job = self._jobs.get(batch_id)
        if job is not None:
            return job
        job = self._load_manifest(batch_id)
        if job is None:
            raise KeyError(f"Unknown batch_id: {batch_id}")
        self._jobs[batch_id] = job
        return job
