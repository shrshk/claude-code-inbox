"""Find things in an item that deserve a link: URLs, GitHub PRs, Jira keys."""

from __future__ import annotations

import re

from ccinbox import config
from ccinbox.verify import PR_RE, pr_refs

_URL_RE = re.compile(r"https?://[^\s<>()\"']+[^\s<>()\"'.,;:!?]")
_JIRA_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
_PR_TEXT_RE = PR_RE


def links_for(text: str, cwd: str | None, cache: dict) -> list[dict]:
    """Distinct links as {"text", "url", "kind"}; ``text`` is the exact substring to replace in the UI."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(t: str, url: str, kind: str) -> None:
        if t and t not in seen:
            seen.add(t)
            out.append({"text": t, "url": url, "kind": kind})

    for m in _URL_RE.finditer(text):
        add(m.group(0), m.group(0), "url")
    stripped = _URL_RE.sub(" ", text)  # so a PR number inside a URL is not matched twice
    jira_spans = [m.span(1) for m in _JIRA_RE.finditer(stripped)]
    for m in _PR_TEXT_RE.finditer(stripped):
        if any(a <= m.start() < b for a, b in jira_spans):
            continue  # "DART-9403" is not "repo DART-9403"
        refs = pr_refs(m.group(0), cwd, cache)
        if refs:
            r = refs[0]
            add(m.group(0), f"{config.GITHUB_URL}/{r.owner}/{r.repo}/pull/{r.number}", "pr")
    if config.JIRA_URL:
        for m in _JIRA_RE.finditer(stripped):
            add(m.group(1), f"{config.JIRA_URL}/browse/{m.group(1)}", "jira")
    return out


def attach_links(items: list[dict]) -> list[dict]:
    cache: dict = {}
    for it in items:
        it["links"] = links_for(f"{it.get('title') or ''} {it.get('details') or ''}", it.get("cwd"), cache)
    return items
