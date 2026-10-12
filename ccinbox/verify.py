"""Check open items against GitHub: a PR that is merged or closed settles the item that was about it.

Deterministic and cheap (one ``gh pr view`` per distinct PR, no model call). Runs at the end of every
sweep and on ``ccinbox verify``. Best effort: without ``gh`` or a resolvable repo it does nothing.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ccinbox import db

log = logging.getLogger("ccinbox.verify")

# "mgs-devops#459", "owner/repo#12", "PR #312", "PR 452", "pull/88"; bare "#88" only when a repo hint is nearby
_PR_RE = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)#(?P<n1>\d+))"
    r"|(?:\bPR\s*#?(?P<n2>\d+))"
    r"|(?:pull/(?P<n3>\d+))"
)
# items whose whole point is the PR itself; once it is merged or closed they are done
_SETTLED_BY_MERGE = re.compile(
    r"^(merge|land|ship|watch|monitor|re-?request review|request review|reply|respond|address|take .* out of draft|"
    r"mark .* ready|get .* (approved|reviewed)|follow up on|check on)\b",
    re.I,
)
_HAS_FOLLOW_UP = re.compile(r"\b(then|after(wards)?|and (sync|deploy|verify|run|update|post|promote)|before)\b", re.I)


@dataclass
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass
class VerifyResult:
    checked: int = 0
    resolved: list[int] = field(default_factory=list)
    annotated: list[int] = field(default_factory=list)
    errors: int = 0

    def as_dict(self) -> dict:
        return {"checked": self.checked, "resolved": len(self.resolved), "annotated": len(self.annotated), "errors": self.errors}


def _git_origin(path: Path) -> tuple[str, str] | None:
    try:
        out = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$", out.stdout.strip())
    return (m.group(1), m.group(2)) if out.returncode == 0 and m else None


def _origin_owner_repo(cwd: str | None, cache: dict) -> tuple[str, str] | None:
    """owner/repo of the git remote at cwd, cached per cwd.

    The session's directory may be gone (a deleted worktree), so walk up to the nearest directory that
    still exists; a parent checkout usually has the same origin. With no git answer at all, borrow the
    owner from any cwd resolved earlier, since one user's repos mostly share an owner.
    """
    if not cwd:
        return None
    if cwd in cache:
        return cache[cwd]
    result = None
    path = Path(cwd)
    while True:
        if path.is_dir():
            result = _git_origin(path)
            if result or path == path.parent:
                break
        if path == path.parent:
            break
        path = path.parent
    if result is None:
        known = [v for v in cache.values() if v]
        if known:
            result = (known[0][0], "")  # owner only; a bare "#12" still cannot be resolved
    cache[cwd] = result
    return result


def pr_refs(text: str, cwd: str | None, cache: dict) -> list[PRRef]:
    refs: list[PRRef] = []
    origin = _origin_owner_repo(cwd, cache)
    for m in _PR_RE.finditer(text):
        if m.group("repo"):
            repo, num = m.group("repo"), int(m.group("n1"))
            if "/" in repo:
                owner, repo = repo.split("/", 1)
            elif origin:
                owner = origin[0]
            else:
                continue
        else:
            num = int(m.group("n2") or m.group("n3"))
            if not origin or not origin[1]:
                continue
            owner, repo = origin
        ref = PRRef(owner, repo, num)
        if ref not in refs:
            refs.append(ref)
    return refs


def gh_pr_state(ref: PRRef) -> dict | None:
    """{'state': 'OPEN'|'MERGED'|'CLOSED', 'title': ...} or None when gh cannot answer."""
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", "state,title,mergedAt"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def verify(conn, lookup: Callable[[PRRef], dict | None] | None = None) -> VerifyResult:
    lookup = lookup or gh_pr_state
    res = VerifyResult()
    cache: dict = {}
    states: dict[str, dict | None] = {}
    for row in db.open_items(conn):
        refs = pr_refs(f"{row['title']} {row['details'] or ''}", row["cwd"], cache)
        if not refs:
            continue
        res.checked += 1
        settled = []
        for ref in refs:
            if ref.slug not in states:
                states[ref.slug] = lookup(ref)
                if states[ref.slug] is None:
                    res.errors += 1
            st = states[ref.slug]
            if st and st.get("state") in ("MERGED", "CLOSED"):
                settled.append((ref, st["state"].lower()))
        if not settled:
            continue
        title = row["title"]
        all_settled = len(settled) == len(refs)
        if all_settled and _SETTLED_BY_MERGE.search(title) and not _HAS_FOLLOW_UP.search(title):
            if db.auto_resolve(conn, [row["id"]], "gh:" + ",".join(f"{r.slug} {s}" for r, s in settled)):
                res.resolved.append(row["id"])
        else:
            note = "(" + "; ".join(f"{r.slug} is {s}" for r, s in settled) + ")"
            if db.append_note(conn, row["id"], note):
                res.annotated.append(row["id"])
    log.info("verify_done checked=%d resolved=%d annotated=%d errors=%d", res.checked, len(res.resolved), len(res.annotated), res.errors)
    return res
