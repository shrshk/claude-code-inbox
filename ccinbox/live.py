"""Which Claude Code sessions are running right now.

Claude Code keeps ``~/.claude/sessions/<pid>.json`` for every running process (session id, cwd, status).
A session is live when that file exists and the pid still answers. Without a registry (older versions)
fall back to transcripts modified in the last ``LIVE_FALLBACK_MIN`` minutes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ccinbox import config
from ccinbox.sweep import encode_project_dir


@dataclass
class LiveSession:
    pid: int
    session_id: str
    cwd: str
    name: str
    status: str
    transcript: Path

    @property
    def exists(self) -> bool:
        return self.transcript.is_file()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def registry_dir() -> Path:
    return config.SESSIONS_DIR


def live_sessions(projects_dir: Path | None = None) -> list[LiveSession]:
    root = Path(projects_dir or config.PROJECTS_DIR)
    out: list[LiveSession] = []
    reg = registry_dir()
    if not reg.is_dir():
        return out
    for f in sorted(reg.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        pid, sid, cwd = d.get("pid"), d.get("sessionId"), d.get("cwd")
        if not isinstance(pid, int) or not isinstance(sid, str) or not isinstance(cwd, str):
            continue
        if not _pid_alive(pid):
            continue
        transcript = root / encode_project_dir(cwd) / f"{sid}.jsonl"
        if not transcript.is_file():  # the registry cwd can differ from the transcript's folder (worktrees)
            transcript = next(iter(root.glob(f"*/{sid}.jsonl")), transcript)
        out.append(
            LiveSession(
                pid=pid,
                session_id=sid,
                cwd=cwd,
                name=str(d.get("name") or ""),
                status=str(d.get("status") or ""),
                transcript=transcript,
            )
        )
    return out


def live_transcripts(projects_dir: Path | None = None) -> list[Path]:
    """Transcripts of live sessions, oldest-modified first. Falls back to recently modified files."""
    root = Path(projects_dir or config.PROJECTS_DIR)
    if registry_dir().is_dir():
        paths = [s.transcript for s in live_sessions(root) if s.exists]
        return sorted(set(paths), key=lambda p: p.stat().st_mtime)
    cutoff = time.time() - config.LIVE_FALLBACK_MIN * 60
    found = [p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff and p.parent.name != encode_project_dir(config.WORK_DIR)]
    return sorted(found, key=lambda p: p.stat().st_mtime)
