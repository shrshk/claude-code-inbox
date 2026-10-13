from ccinbox import config, links


def test_links_for_urls_prs_and_jira(env, monkeypatch):
    monkeypatch.setenv("CCINBOX_JIRA_URL", "https://acme.atlassian.net/")
    import importlib

    importlib.reload(config)
    from ccinbox import verify as v

    monkeypatch.setattr(v, "_origin_owner_repo", lambda c, cache: ("acme", "api"))
    text = "Merge mgs-devops#459 and PR #12 for DART-9403, see https://github.com/acme/api/pull/7, then other/repo#3."
    out = links.links_for(text, "/x", {})
    by_text = {o["text"]: o for o in out}
    assert by_text["https://github.com/acme/api/pull/7"]["kind"] == "url"
    assert by_text["mgs-devops#459"]["url"] == "https://github.com/acme/mgs-devops/pull/459"
    assert by_text["PR #12"]["url"] == "https://github.com/acme/api/pull/12"
    assert by_text["other/repo#3"]["url"] == "https://github.com/other/repo/pull/3"
    assert by_text["DART-9403"]["url"] == "https://acme.atlassian.net/browse/DART-9403"
    assert len(out) == 5  # the PR number inside the URL is not linked twice
    monkeypatch.setenv("CCINBOX_JIRA_URL", "")
    importlib.reload(config)
    assert all(o["kind"] != "jira" for o in links.links_for(text, "/x", {}))


def test_items_api_carries_links(env, conn, monkeypatch):
    from fastapi.testclient import TestClient

    from ccinbox import db, server
    from ccinbox import verify as v

    monkeypatch.setattr(v, "_origin_owner_repo", lambda c, cache: ("acme", "api"))
    db.insert_item(conn, "api", "Merge acme/api#9 after review", "see https://example.com/x", cwd="/x")
    data = TestClient(server.create_app()).get("/api/items").json()
    assert [l["url"] for l in data["items"][0]["links"]] == ["https://example.com/x", "https://github.com/acme/api/pull/9"]
