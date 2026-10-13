from ccinbox import db
from ccinbox.verify import PRRef, pr_refs, verify


def test_pr_refs_parse_forms():
    cache = {"/x": ("acme", "api")}
    refs = pr_refs("Merge mgs-devops#459, then PR #12 and pull/7 and other/repo#3", "/x", cache)
    assert [r.slug for r in refs] == ["acme/mgs-devops#459", "acme/api#12", "acme/api#7", "other/repo#3"]
    assert pr_refs("PR #12", None, {}) == []  # no repo to resolve a bare number against
    assert pr_refs("tools/foo#5", None, {}) == [PRRef("tools", "foo", 5)]
    more = pr_refs("approve #6717 and mgs-cicd #482; ticket DART-9403; item #3 of 5", "/x", cache)
    assert [r.slug for r in more] == ["acme/api#6717", "acme/mgs-cicd#482"]  # #3 is too short, DART-9403 is Jira


def test_verify_closes_settled_items_and_annotates_follow_ups(env, conn):
    cwd = "/Users/me/projects/acme-api"
    merged = db.insert_item(conn, "acme-api", "Merge mgs-devops#459 so main matches the applied secret", cwd=cwd)
    chained = db.insert_item(conn, "acme-api", "Merge mgs-cicd#462 and sync prod Centrifugo Argo apps", cwd=cwd)
    open_pr = db.insert_item(conn, "acme-api", "Merge PR #88 after review", cwd=cwd)
    unrelated = db.insert_item(conn, "acme-api", "Rotate the webhook secret", cwd=cwd)
    decide = db.insert_item(conn, "acme-api", "Decide whether to post the review results on PR #91", cwd=cwd)
    from ccinbox import verify as v

    v._origin_owner_repo = lambda c, cache: ("acme", "api")  # no git call in tests
    states = {"acme/mgs-devops#459": {"state": "MERGED"}, "acme/mgs-cicd#462": {"state": "MERGED"}, "acme/api#88": {"state": "OPEN"}, "acme/api#91": {"state": "CLOSED"}}
    looked = []

    def lookup(ref):
        looked.append(ref.slug)
        return states.get(ref.slug)

    res = verify(conn, lookup)
    assert res.checked == 4 and res.errors == 0
    assert res.resolved == [merged]
    assert set(res.annotated) == {chained, decide}
    assert db.get_item(conn, merged)["status"] == "auto_resolved" and db.get_item(conn, merged)["resolved_by"].startswith("gh:")
    assert db.get_item(conn, chained)["status"] == "open" and "acme/mgs-cicd#462 is merged" in db.get_item(conn, chained)["details"]
    assert db.get_item(conn, open_pr)["status"] == "open" and db.get_item(conn, unrelated)["status"] == "open"
    assert len(looked) == len(set(looked))  # each PR looked up once
    assert verify(conn, lookup).annotated == []  # notes are not appended twice
