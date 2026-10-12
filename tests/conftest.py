from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point every CCINBOX_* path at a temp dir and reload config."""
    home = tmp_path / "home"
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("CCINBOX_HOME", str(home))
    monkeypatch.setenv("CCINBOX_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CCINBOX_TOKEN", "")
    monkeypatch.delenv("CCINBOX_MIN_USER_TURNS", raising=False)
    from ccinbox import config

    importlib.reload(config)
    config.ensure_dirs()
    yield {"home": home, "projects": projects, "tmp": tmp_path}
    importlib.reload(config)


@pytest.fixture
def transcript_path(env) -> Path:
    project_dir = env["projects"] / "-Users-me-projects-demo-backend"
    project_dir.mkdir()
    dest = project_dir / "11111111-2222-3333-4444-555555555555.jsonl"
    shutil.copy(FIXTURES / "session.jsonl", dest)
    return dest


@pytest.fixture
def conn(env):
    from ccinbox import db

    c = db.connect()
    yield c
    c.close()


def write_transcript(path: Path, turns: list[tuple[str, str]], *, cwd="/Users/me/projects/demo-backend", session_id="s-1"):
    """Write a minimal transcript with the given (role, text) turns."""
    lines = []
    for i, (role, text) in enumerate(turns):
        rec = {
            "type": role,
            "isSidechain": False,
            "uuid": f"u{i}",
            "timestamp": f"2026-10-09T14:{i:02d}:00.000Z",
            "cwd": cwd,
            "sessionId": session_id,
            "message": {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}]},
        }
        lines.append(json.dumps(rec))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
