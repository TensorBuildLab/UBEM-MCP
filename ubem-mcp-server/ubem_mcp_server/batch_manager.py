"""
Batch simulation orchestration for UBEM-MCP.

This module is new to UBEM-MCP (not part of EnergyPlus-MCP, which only runs
one IDF at a time). See NOTICE.md for the relationship to EnergyPlus-MCP.

Design notes:
- eppy's IDF class holds module-level global state (IDF.setiddname), so
  concurrent simulations cannot safely share a process via threads. Each
  model in a batch runs in its own worker process (ProcessPoolExecutor),
  which builds its own EnergyPlusManager and therefore its own eppy state.
- A batch can take minutes to hours, which would exceed typical MCP client
  call timeouts if a single tool call blocked until every model finished.
  submit_batch() returns a batch_id immediately; the batch runs as a
  background asyncio task, polled via get_status()/get_results().
"""

import asyncio
import json
import logging
import os
import re
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import Config
from .energyplus_tools import EnergyPlusManager
from .utils.path_utils import resolve_idf_path, resolve_weather_file_path, get_file_info

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}
_TERMINAL_MODEL_STATUSES = {"success", "failed", "cancelled"}


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
    duration_seconds: Optional[float] = None


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


def _run_single_model_worker(
    config: Config,
    idf_path: str,
    weather_file: Optional[str],
    output_directory: str,
    sim_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Entry point executed in a worker process. Must stay at module scope
    (not a method/lambda/closure) so ProcessPoolExecutor can pickle it.

    Builds its own EnergyPlusManager so this process's eppy global IDD state
    is independent of every other concurrently-running worker.
    """
    try:
        manager = EnergyPlusManager(config)
        raw = manager.run_simulation(
            idf_path=idf_path,
            weather_file=weather_file,
            output_directory=output_directory,
            **sim_kwargs,
        )
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001 - worker boundary: always return a result, never raise
        return {
            "success": False,
            "input_idf": idf_path,
            "weather_file": weather_file,
            "output_directory": output_directory,
            "error": str(e),
        }


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
    ) -> Dict[str, Any]:
        """Validate and register a batch, then schedule it to run in the
        background. Returns immediately with a batch_id and initial status.
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
        executor = ProcessPoolExecutor(max_workers=max(1, job.max_workers))
        futures: Dict[asyncio.Future, ModelSpec] = {}
        try:
            for spec in pending_specs:
                spec.status = "running"
                spec.started_at = datetime.now().isoformat()
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
                    if spec.started_at:
                        spec.duration_seconds = round(
                            (
                                datetime.fromisoformat(spec.completed_at)
                                - datetime.fromisoformat(spec.started_at)
                            ).total_seconds(),
                            2,
                        )
                    spec.result = result
                    if result.get("success"):
                        spec.status = "success"
                    else:
                        spec.status = "failed"
                        spec.error = result.get("error", "simulation failed")
                        if not job.continue_on_error:
                            stop_early = True

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
        return {
            "batch_id": job.batch_id,
            "status": job.status,
            "created_at": job.created_at,
            "total_models": len(job.models),
            "counts": dict(counts),
            "is_complete": job.status in _TERMINAL_JOB_STATUSES,
            "max_workers": job.max_workers,
            "continue_on_error": job.continue_on_error,
            "output_directory": job.output_directory,
        }

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
