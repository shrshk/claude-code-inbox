"""ntfy digest, stdlib only."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections import defaultdict

from ccinbox import config, db


def build_digest(conn) -> str:
    items = db.inbox(conn)
    if not items:
        return ""
    by_project: dict[str, list] = defaultdict(list)
    for row in items:
        by_project[row["project"]].append(row)
    lines = []
    for project in sorted(by_project):
        rows = sorted(by_project[project], key=lambda r: r["id"])
        lines.append(f"{project} ({len(rows)})")
        lines.extend(f"  - {r['title']}" for r in rows[:8])
        if len(rows) > 8:
            lines.append(f"  … and {len(rows) - 8} more")
    return "\n".join(lines)


def send_digest(conn, *, topic: str | None = None, url: str | None = None, dry_run: bool = False) -> str:
    body = build_digest(conn)
    topic = topic or config.NTFY_TOPIC
    url = (url or config.NTFY_URL).rstrip("/")
    if not body:
        return ""
    if dry_run:
        return body
    if not topic:
        raise RuntimeError("CCINBOX_NTFY_TOPIC is not set")
    count = len(db.inbox(conn))
    req = urllib.request.Request(
        f"{url}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"Claude Code Inbox: {count} open item{'s' if count != 1 else ''}",
            "Tags": "inbox_tray",
            "Priority": "default",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ntfy post failed: {exc}") from exc
    return body
