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


def test_bulk_actions_and_undo(env, conn):
    ids = [db.insert_item(conn, "demo", f"Item {i}", topic="T") for i in range(3)]
    client = make_client()
    r = client.post("/api/bulk", json={"action": "snooze", "ids": ids + [999]})
    assert r.status_code == 200
    body = r.json()
    assert [u["id"] for u in body["updated"]] == ids and body["missing"] == [999]
    assert all(u["status"] == "snoozed" for u in body["updated"])
    assert client.get("/api/items").json()["counts"] == {"open": 0, "snoozed": 3, "resolved": 0}
    r = client.post("/api/bulk", json={"action": "undo", "ids": ids})
    assert [u["status"] for u in r.json()["updated"]] == ["open"] * 3
    assert client.post("/api/bulk", json={"action": "nuke", "ids": ids}).status_code == 400
    assert client.post("/api/bulk", json={"action": "done", "ids": []}).status_code == 400


def test_token_required_when_set(env, monkeypatch):
    monkeypatch.setenv("CCINBOX_TOKEN", "s3cret")
    importlib.reload(config)
    client = make_client()
    assert client.get("/api/items").status_code == 401
    assert client.get("/api/items?token=s3cret").status_code == 200
    assert client.get("/api/items", headers={"X-Token": "s3cret"}).status_code == 200
    assert client.get("/api/items", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/").status_code == 200  # the shell is public, the data is not


class FakeProc:
    def __init__(self, out, rc=0, err=""):
        self.out, self.rc, self.err, self.returncode, self._done = out, rc, err, None, False

    def poll(self):
        if self._done:
            self.returncode = self.rc
        return self.returncode

    def communicate(self):
        return self.out, self.err

    def finish(self):
        self._done = True


def test_refresh_sweep_runs_once_and_reports(env):
    from ccinbox import server

    procs = []

    def spawn():
        p = FakeProc('{"candidates": 3, "processed": 2, "failed": 0, "added": 4, "resolved": 1, "cost_usd": 0.3, "sessions": []}')
        procs.append(p)
        return p

    client = TestClient(server.create_app(server.SweepRunner(spawn)))
    st = client.post("/api/sweep").json()
    assert st["running"] is True and st["result"] is None
    assert client.post("/api/sweep").json()["running"] is True and len(procs) == 1  # no second process while running
    procs[0].finish()
    st = client.get("/api/sweep").json()
    assert st["running"] is False and st["result"]["added"] == 4 and st["error"] is None
    assert client.post("/api/sweep").json()["running"] is True and len(procs) == 2  # can run again afterwards

    bad = server.SweepRunner(lambda: FakeProc("not json", rc=1, err="boom"))
    bad.start()
    bad.proc.finish()
    st = bad.state()
    assert st["running"] is False and st["result"] is None and "boom" in st["error"]


def test_ensure_serve_helpers(env, tmp_path):
    import socket

    from ccinbox import cli

    (env["home"] / "env").write_text("export CCINBOX_TOKEN='abc'\nCCINBOX_HOST=0.0.0.0\n# comment\nbroken line\n")
    assert config.load_env_file() == {"CCINBOX_TOKEN": "abc", "CCINBOX_HOST": "0.0.0.0"}
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(5)
    port = srv.getsockname()[1]
    assert cli.port_open("0.0.0.0", port) and cli.port_open("127.0.0.1", port)
    srv.close()
    assert not cli.port_open("127.0.0.1", port)
    snippet = cli.hooks_snippet(with_stop=True)
    assert set(snippet["hooks"]) == {"SessionEnd", "Stop", "SessionStart"}
    assert snippet["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("ccinbox ensure-serve")
    assert "SessionStart" not in cli.hooks_snippet(with_serve=False)["hooks"]
