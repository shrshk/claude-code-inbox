import importlib

from fastapi.testclient import TestClient

from ccinbox import config, db


def make_client():
    from ccinbox import server

    importlib.reload(server)
    return TestClient(server.create_app())


def test_items_and_actions(env, conn):
    a = db.insert_item(conn, "demo", "First", "details here", session_title="sesh")
    b = db.insert_item(conn, "demo", "Second")
    db.auto_resolve(conn, [b], "s2")
    client = make_client()
    data = client.get("/api/items").json()
    assert [i["id"] for i in data["items"]] == [a]
    assert data["items"][0]["session_title"] == "sesh"
    assert [r["id"] for r in data["resolved"]] == [b]
    assert data["counts"] == {"open": 1, "snoozed": 0, "resolved": 1}

    r = client.post(f"/api/items/{a}", json={"action": "snooze"})
    assert r.status_code == 200 and r.json()["status"] == "snoozed"
    data = client.get("/api/items").json()
    assert data["items"] == [] and data["counts"]["snoozed"] == 1
    assert client.post(f"/api/items/{a}", json={"action": "undo"}).json()["status"] == "open"
    assert client.post(f"/api/items/{a}", json={"action": "done"}).json()["status"] == "done"
    assert client.post("/api/items/999", json={"action": "done"}).status_code == 404
    assert client.post(f"/api/items/{a}", json={"action": "nuke"}).status_code == 400
    assert client.get("/api/sessions").json()["stats"]["items"] == {"done": 1, "auto_resolved": 1}
    assert client.get("/").status_code == 200 and "Claude Code Inbox" in client.get("/").text


def test_token_required_when_set(env, monkeypatch):
    monkeypatch.setenv("CCINBOX_TOKEN", "s3cret")
    importlib.reload(config)
    client = make_client()
    assert client.get("/api/items").status_code == 401
    assert client.get("/api/items?token=s3cret").status_code == 200
    assert client.get("/api/items", headers={"X-Token": "s3cret"}).status_code == 200
    assert client.get("/api/items", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/").status_code == 200  # the shell is public, the data is not
