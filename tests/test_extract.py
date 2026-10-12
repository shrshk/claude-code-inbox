import os

import pytest

from ccinbox import config, db, extract
from ccinbox.extract import ExtractionError, ExtractorResponse, parse_output, process_session


def fake(items, resolved=()):
    calls = []

    def extractor(prompt):
        calls.append(prompt)
        return {"items": items, "resolved_ids": list(resolved)}

    extractor.calls = calls
    return extractor


def test_process_adds_items_and_is_idempotent_on_mtime(env, transcript_path, conn):
    ex = fake([{"title": "Open the PR for DEMO-42", "details": "fix is committed locally", "topic": "Balance tests"}, "Ask the reviewer about the rounding rule"])
    res = process_session(transcript_path, ex, conn=conn)
    assert res.status == "processed" and len(res.items_added) == 2
    prompt = ex.calls[0]
    assert "Open items:\n(none)" in prompt and "remind me to open the PR" in prompt and "Session title: balance tests" in prompt
    rows = db.open_items(conn, "demo-backend")
    assert [r["title"] for r in rows] == ["Open the PR for DEMO-42", "Ask the reviewer about the rounding rule"]
    assert rows[0]["session_title"] == "balance tests" and rows[0]["details"] == "fix is committed locally"
    assert rows[0]["topic"] == "Balance tests" and rows[1]["topic"] == ""

    assert process_session(transcript_path, ex, conn=conn).status == "unchanged"
    assert len(ex.calls) == 1
    assert process_session(transcript_path, ex, conn=conn, force=True).status == "processed"
    assert len(ex.calls) == 2
    assert "[%d] (Balance tests) Open the PR for DEMO-42" % rows[0]["id"] in ex.calls[1]
    assert len(db.open_items(conn, "demo-backend")) == 2  # dedupe held on the rerun
    sess = db.get_session(conn, "11111111-2222-3333-4444-555555555555")
    assert sess["items_added"] == 0 and sess["transcript_mtime"] == os.path.getmtime(transcript_path)


def test_resolved_ids_are_validated(env, transcript_path, conn):
    mine = db.insert_item(conn, "demo-backend", "Run the qa migration")
    other = db.insert_item(conn, "other-project", "Unrelated")
    res = process_session(transcript_path, fake([], resolved=[mine, other, 4242]), conn=conn)
    assert res.status == "processed" and res.items_resolved == [mine]
    assert db.get_item(conn, mine)["status"] == "auto_resolved"
    assert db.get_item(conn, other)["status"] == "open"


def test_reprocess_after_change_and_debounce(env, transcript_path, conn):
    ex = fake([])
    assert process_session(transcript_path, ex, conn=conn).status == "processed"
    with open(transcript_path, "a") as fh:
        fh.write('{"type":"user","message":{"role":"user","content":"one more thing"},"cwd":"/Users/me/projects/demo-backend"}\n')
    os.utime(transcript_path, None)
    assert process_session(transcript_path, ex, conn=conn, debounce_sec=3600).status == "debounced"
    assert process_session(transcript_path, ex, conn=conn).status == "processed"
    assert len(ex.calls) == 2


def test_failures_record_mtime_zero_so_sweep_retries(env, transcript_path, conn):
    def boom(prompt):
        raise ExtractionError("claude timed out")

    res = process_session(transcript_path, boom, conn=conn)
    assert res.status == "failed" and "timed out" in res.error
    sess = db.get_session(conn, "11111111-2222-3333-4444-555555555555")
    assert sess["transcript_mtime"] == 0 and sess["error"] == "claude timed out"
    assert process_session(transcript_path, fake([]), conn=conn).status == "processed"
    assert db.get_session(conn, "11111111-2222-3333-4444-555555555555")["error"] is None


def test_short_and_extractor_sessions_are_skipped(env, tmp_path, conn):
    from tests.conftest import write_transcript

    short = write_transcript(tmp_path / "short.jsonl", [("user", "hi"), ("assistant", "hello")], session_id="short")
    calls = fake([])
    assert process_session(short, calls, conn=conn).status == "skipped_short"
    assert db.get_session(conn, "short")["transcript_mtime"] > 0
    assert process_session(short, calls, conn=conn).status == "unchanged"
    own = write_transcript(tmp_path / "own.jsonl", [("user", "a"), ("assistant", "b"), ("user", "c")], cwd=str(config.WORK_DIR))
    assert process_session(own, calls, conn=conn).status == "skipped_extractor"
    assert process_session(tmp_path / "nope.jsonl", calls, conn=conn).status == "missing"
    assert calls.calls == []


def test_parse_output_tolerates_fences_and_junk(env):
    resp = ExtractorResponse(text='Sure!\n```json\n{"items":[{"title":"  Do   X ","details":"why"},"bare string",7],"resolved_ids":[3,"4","x"]}\n```')
    items, resolved = parse_output(resp)
    assert items == [{"title": "Do X", "details": "why", "topic": ""}, {"title": "bare string", "details": "", "topic": ""}]
    assert resolved == [3, 4]
    assert parse_output(ExtractorResponse(structured={"items": [], "resolved_ids": []})) == ([], [])
    with pytest.raises(ExtractionError):
        parse_output(ExtractorResponse(text="no json here"))
    many = {"items": [{"title": f"t{i}", "details": ""} for i in range(20)], "resolved_ids": []}
    assert len(parse_output(ExtractorResponse(structured=many))[0]) == config.MAX_ITEMS


def test_claude_command_has_every_guard(env):
    cmd = extract.claude_command()
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--json-schema" in cmd and "--max-budget-usd" in cmd


def test_categorize_labels_open_items_only(env, conn):
    a = db.insert_item(conn, "demo", "Wire Centrifugo publish on recon")
    b = db.insert_item(conn, "demo", "Review PR 6771", topic="Code reviews")
    c = db.insert_item(conn, "demo", "Old thing")
    db.apply_action(conn, c, "done")
    prompts = []

    def ex(prompt):
        prompts.append(prompt)
        return {"topics": [{"id": a, "topic": " Centrifugo "}, {"id": c, "topic": "Nope"}, {"id": 999, "topic": "X"}, "junk"]}

    assert extract.categorize(conn, ex) == {a: "Centrifugo"}
    assert "Existing topics: Code reviews" in prompts[0] and f"[{a}] (demo) Wire Centrifugo" in prompts[0] and "Review PR" not in prompts[0]
    assert db.get_item(conn, c)["topic"] == ""
    assert db.topics(conn) == ["Centrifugo", "Code reviews"]
    assert extract.categorize(conn, ex) == {}  # nothing left without a topic
    assert extract.categorize(conn, ex, only_missing=False) == {a: "Centrifugo"}
    assert "--json-schema" in extract.claude_command(extract.TOPIC_SCHEMA)


def test_topic_column_is_added_to_old_databases(env):
    import sqlite3

    raw = sqlite3.connect(str(config.DB_PATH))
    raw.executescript(db.SCHEMA.replace("    topic TEXT NOT NULL DEFAULT '',\n", ""))
    raw.close()
    conn = db.connect()
    assert "topic" in {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    conn.close()
