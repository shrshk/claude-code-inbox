import json
import os

from ccinbox import config, live
from ccinbox.sweep import encode_project_dir
from tests.conftest import write_transcript


def test_live_sessions_from_registry(env, monkeypatch, tmp_path):
    reg = tmp_path / "sessions"; reg.mkdir()
    monkeypatch.setenv("CCINBOX_SESSIONS_DIR", str(reg))
    import importlib; importlib.reload(config)
    cwd = "/Users/me/projects/demo"
    mine = os.getpid()
    (reg / f"{mine}.json").write_text(json.dumps({"pid": mine, "sessionId": "live-1", "cwd": cwd, "name": "work", "status": "idle"}))
    (reg / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": "dead-1", "cwd": cwd}))
    (reg / "junk.json").write_text("not json")
    t = write_transcript(env["projects"] / encode_project_dir(cwd) / "live-1.jsonl", [("user", "a"), ("assistant", "b")], cwd=cwd, session_id="live-1")
    write_transcript(env["projects"] / encode_project_dir(cwd) / "old.jsonl", [("user", "a"), ("assistant", "b")], cwd=cwd, session_id="old")
    rows = live.live_sessions()
    assert [(r.session_id, r.name, r.exists) for r in rows] == [("live-1", "work", True)]
    assert live.live_transcripts() == [t]  # the dead pid and the unregistered transcript are left out
    # a session whose transcript sits in another project folder (a worktree) is still found by id
    (reg / f"{mine}.json").write_text(json.dumps({"pid": mine, "sessionId": "wt-1", "cwd": "/Users/me/projects/elsewhere"}))
    wt = write_transcript(env["projects"] / "-Users-me-projects-demo--claude-worktrees-X" / "wt-1.jsonl", [("user", "a")], session_id="wt-1")
    assert live.live_transcripts() == [wt]


def test_live_fallback_without_registry(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CCINBOX_SESSIONS_DIR", str(tmp_path / "missing"))
    import importlib; importlib.reload(config)
    recent = write_transcript(env["projects"] / "-p" / "r.jsonl", [("user", "a")], session_id="r")
    stale = write_transcript(env["projects"] / "-p" / "s.jsonl", [("user", "a")], session_id="s")
    import time
    os.utime(stale, (time.time() - 3 * 3600, time.time() - 3 * 3600))
    assert live.live_transcripts() == [recent]
