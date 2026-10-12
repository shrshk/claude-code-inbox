"""Backstop: process recent transcripts that no hook handled.

Only transcripts idle for ``SWEEP_IDLE_MIN`` minutes are considered, so a live
session is not re-extracted every half hour while it grows.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ccinbox import config
from ccinbox.extract import Extractor, ProcessResult, process_session

log = logging.getLogger("ccinbox.sweep")


def encode_project_dir(path: str | Path) -> str:
    """Claude Code names ``~/.claude/projects/<dir>`` after the cwd with ``/`` replaced by ``-``."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(Path(path).expanduser().resolve()))


def candidate_transcripts(
    since_days: int | None = None,
    idle_min: int | None = None,
    projects_dir: str | Path | None = None,
) -> list[Path]:
    root = Path(projects_dir or config.PROJECTS_DIR)
    if not root.is_dir():
        return []
    since_days = config.SWEEP_DAYS if since_days is None else since_days
    idle_min = config.SWEEP_IDLE_MIN if idle_min is None else idle_min
    now = time.time()
    oldest = now - since_days * 86400
    idle = idle_min * 60
    skip_dir = encode_project_dir(config.WORK_DIR)
    found: list[tuple[float, Path]] = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir() or project_dir.name == skip_dir:
            continue
        for f in project_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < oldest or now - mtime < idle:
                continue
            found.append((mtime, f))
    return [f for _, f in sorted(found)]


def sweep(
    since_days: int | None = None,
    idle_min: int | None = None,
    *,
    limit: int | None = None,
    force: bool = False,
    extractor: Extractor | None = None,
) -> list[ProcessResult]:
    results: list[ProcessResult] = []
    processed = 0
    for path in candidate_transcripts(since_days, idle_min):
        if limit is not None and processed >= limit:
            break
        res = process_session(path, extractor, force=force)
        results.append(res)
        if res.status in ("processed", "failed"):
            processed += 1
    log.info(
        "sweep_done candidates=%d processed=%d failed=%d",
        len(results),
        sum(r.status == "processed" for r in results),
        sum(r.status == "failed" for r in results),
    )
    return results
