from datetime import datetime, timedelta, timezone

from ccinbox import db


def test_insert_dedupes_on_normalized_title(conn):
    a = db.insert_item(conn, "demo", "Open the PR for DEMO-42")
    b = db.insert_item(conn, "demo", "open the PR, for demo-42!")
    c = db.insert_item(conn, "other", "Open the PR for DEMO-42")
    assert a and c and b is None
    assert db.insert_item(conn, "demo", "   ") is None
    assert [r["id"] for r in db.open_items(conn, "demo")] == [a]


def test_actions_undo_and_snooze_wake(conn):
    i = db.insert_item(conn, "demo", "Ask reviewer about rounding")
    row = db.apply_action(conn, i, "done")
    assert row["status"] == "done" and row["prev_status"] == "open"
    row = db.apply_action(conn, i, "undo")
    assert row["status"] == "open" and row["prev_status"] is None
    assert db.apply_action(conn, i, "undo")["status"] == "open"  # nothing to undo is a no-op

    row = db.apply_action(conn, i, "snooze")
    assert row["status"] == "snoozed" and row["snoozed_until"] > db.now_iso()
    assert [r["id"] for r in db.inbox(conn)] == []
    assert [r["id"] for r in db.open_items(conn)] == [i]  # still owed, so the extractor sees it

    past = db.to_iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    db.apply_action(conn, i, "snooze", until=past)
    assert [r["id"] for r in db.inbox(conn)] == [i]
    assert db.get_item(conn, i)["status"] == "open"

    assert db.apply_action(conn, 999, "done") is None
    try:
        db.apply_action(conn, i, "explode")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown action accepted")


def test_auto_resolve_only_touches_open_items(conn):
    a = db.insert_item(conn, "demo", "A")
    b = db.insert_item(conn, "demo", "B")
    db.apply_action(conn, b, "dismiss")
    assert db.auto_resolve(conn, [a, b, 12345], "sess-2") == [a]
    assert db.get_item(conn, a)["resolved_by"] == "sess-2"
    assert [r["id"] for r in db.recently_resolved(conn)] == [a]
    assert db.apply_action(conn, a, "undo")["status"] == "open"


def test_sessions_and_stats(conn):
    db.record_session(conn, session_id="s", transcript_path="/p", project="demo", transcript_mtime=1.0, cost_usd=0.1)
    db.record_session(conn, session_id="s", transcript_path="/p", project="demo", transcript_mtime=2.0, cost_usd=0.2, error="x")
    s = db.get_session(conn, "s")
    assert s["transcript_mtime"] == 2.0 and s["error"] == "x"
    db.insert_item(conn, "demo", "T")
    st = db.stats(conn)
    assert st["sessions"] == 1 and st["failed"] == 1 and st["items"] == {"open": 1}
    db.set_meta(conn, "k", "v")
    assert db.get_meta(conn, "k") == "v"
