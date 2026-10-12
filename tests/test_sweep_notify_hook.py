import io
import json
import os
import time

from ccinbox import config, db, hook, sweep
from tests.conftest import write_transcript


def test_sweep_skips_fresh_files_and_the_extractor_dir(env, conn):
    projects = env["projects"]
    old = write_transcript(projects / "-Users-me-projects-demo" / "old.jsonl", [("user", "a"), ("assistant", "b"), ("user", "c")], session_id="old")
    fresh = write_transcript(projects / "-Users-me-projects-demo" / "fresh.jsonl", [("user", "a"), ("assistant", "b"), ("user", "c")], session_id="fresh")
    ancient = write_transcript(projects / "-Users-me-projects-demo" / "ancient.jsonl", [("user", "a"), ("assistant", "b")], session_id="ancient")
    own_dir = projects / sweep.encode_project_dir(config.WORK_DIR)
    write_transcript(own_dir / "own.jsonl", [("user", "a"), ("assistant", "b"), ("user", "c")], session_id="own")
    now = time.time()
    os.utime(old, (now - 3600, now - 3600))
    os.utime(ancient, (now - 10 * 86400, now - 10 * 86400))
    assert sweep.candidate_transcripts(since_days=1, idle_min=20) == [old]
    calls = []

    def ex(prompt):
        calls.append(prompt)
        return {"items": [{"title": "Follow up", "details": ""}], "resolved_ids": []}

    results = sweep.sweep(since_days=1, idle_min=20, extractor=ex)
    assert [r.status for r in results] == ["processed"] and len(calls) == 1
    assert sweep.sweep(since_days=1, idle_min=0, limit=0, extractor=ex) == [] or len(calls) == 1
    assert sweep.encode_project_dir("/Users/me/projects/demo-backend") == "-Users-me-projects-demo-backend"


def test_digest_groups_by_project(env, conn):
    from ccinbox.notify import build_digest, send_digest

    assert build_digest(conn) == ""
    db.insert_item(conn, "beta", "B1")
    db.insert_item(conn, "alpha", "A1")
    db.insert_item(conn, "alpha", "A2")
    body = send_digest(conn, dry_run=True)
    assert body.splitlines() == ["alpha (2)", "  - A1", "  - A2", "beta (1)", "  - B1"]


def test_hook_spawns_detached_worker(env, monkeypatch):
    spawned = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(hook.subprocess, "Popen", fake_popen)
    payload = {"session_id": "abc", "transcript_path": "/tmp/t.jsonl", "hook_event_name": "SessionEnd", "reason": "other"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    assert spawned["args"][-3:] == ["process", "/tmp/t.jsonl", "--then-sweep"] and spawned["kwargs"]["start_new_session"] is True
    assert "spawned pid=4242" in config.LOG_PATH.read_text()

    payload["hook_event_name"] = "Stop"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hook.main()
    assert spawned["args"][-4:] == ["process", "/tmp/t.jsonl", "--then-sweep", "--debounce", str(config.DEBOUNCE_SEC)][-4:]
    assert "--then-sweep" in spawned["args"] and spawned["args"][-1] == str(config.DEBOUNCE_SEC)

    spawned.clear()
    monkeypatch.setenv("CCINBOX_EXTRACTING", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0 and spawned == {}
    monkeypatch.delenv("CCINBOX_EXTRACTING")
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert hook.main() == 0 and spawned == {}
