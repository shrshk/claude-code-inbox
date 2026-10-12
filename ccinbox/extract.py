"""Prompt, ``claude -p`` call, JSON parsing, dedupe and auto-resolve.

:func:`process_session` is the core. The extractor is injectable so tests never
call the CLI.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ccinbox import config, db
from ccinbox import transcript as transcript_mod

log = logging.getLogger("ccinbox.extract")


class ExtractionError(RuntimeError):
    pass


@dataclass
class ExtractorResponse:
    text: str = ""
    structured: dict | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict | None = None


Extractor = Callable[[str], "ExtractorResponse | str | dict"]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "details": {"type": "string"}, "topic": {"type": "string"}},
                "required": ["title", "details", "topic"],
            },
        },
        "resolved_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["items", "resolved_ids"],
}

PROMPT_HEADER = """You extract follow-up action items from the transcript of a finished Claude Code session.

Return ONLY a JSON object of this shape, nothing else:
{"items": [{"title": "...", "details": "...", "topic": "..."}], "resolved_ids": [1, 2]}

What counts as an action item: something the USER still has to do after this session, that nobody did during it:
- a step the user explicitly deferred ("later", "tomorrow", "after lunch", "next session", "remind me")
- work the assistant left undone and handed to the user: merge or mark a PR ready, run a migration, ask a teammate, verify in an environment, rotate a secret, reply to a thread
- a decision or question the user said they would come back to
- a bug, risk or gap the assistant flagged and the user acknowledged but nobody fixed

Precision over recall. An empty list is a good answer. Do NOT include:
- anything completed within the session (commits, pushed branches, posted comments, written files)
- the assistant's routine closing suggestions ("you could also...", "next steps:") unless the user agreed to do them
- hypotheticals, options that were considered and rejected, or research the assistant did for itself
- things only the assistant wanted; the user is the person in USER turns, system reminders are not the user
- long-standing project facts or memories; only actions

Each item: "title" is imperative, specific and at most 80 characters (name the ticket, PR, file or env when known).
"details" is at most 300 characters: why it matters and where to look. "topic" is a 1-3 word label for the
workstream the item belongs to (for example "Centrifugo", "Snapshots v2", "Code reviews", "Prod release");
reuse a topic from the open items list below whenever one fits, so related items group together.
Return at most 5 items, best first.

The project's already-open items are listed below. Never re-add one, even reworded. If the transcript clearly shows
one was completed or no longer applies, put its id in "resolved_ids". Use only ids from that list.
"""


def build_prompt(t: transcript_mod.Transcript, open_items) -> str:
    listing = "\n".join(f"- [{r['id']}] ({r['topic'] or 'no topic'}) {r['title']}" for r in open_items) or "(none)"
    return (
        PROMPT_HEADER
        + "\nOpen items:\n"
        + listing
        + "\n\n---\nProject: "
        + t.project
        + "\nDirectory: "
        + (t.cwd or "?")
        + "\nBranch: "
        + (t.git_branch or "?")
        + "\nSession title: "
        + (t.title or "(untitled)")
        + "\nSession ended: "
        + (t.ended_at or "?")
        + "\n\nTranscript (tool output abbreviated; may be trimmed in the middle):\n\n"
        + t.render()
        + "\n"
    )


def claude_command(schema: dict | None = None) -> list[str]:
    binary = shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN
    return [
        binary,
        "-p",
        "--output-format",
        "json",
        "--model",
        config.MODEL,
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--max-budget-usd",
        str(config.MAX_BUDGET_USD),
        "--json-schema",
        json.dumps(schema or OUTPUT_SCHEMA),
    ]


def run_claude(prompt: str, schema: dict | None = None) -> ExtractorResponse:
    """Call ``claude -p`` with every recursion guard on: env flag, work-dir cwd, no hooks, no persistence."""
    config.ensure_dirs()
    env = {**os.environ, "CCINBOX_EXTRACTING": "1"}
    try:
        proc = subprocess.run(
            claude_command(schema),
            input=prompt,
            cwd=str(config.WORK_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=config.CLAUDE_TIMEOUT_SEC,
        )
    except FileNotFoundError as exc:
        raise ExtractionError(f"claude binary not found: {config.CLAUDE_BIN}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"claude timed out after {config.CLAUDE_TIMEOUT_SEC}s") from exc
    stdout = proc.stdout.strip()
    if not stdout:
        raise ExtractionError(f"claude exited {proc.returncode} with no output: {proc.stderr.strip()[:500]}")
    try:
        payload = json.loads(stdout)
    except ValueError as exc:
        raise ExtractionError(f"claude output was not JSON: {stdout[:300]!r}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError(f"claude output was not an object: {stdout[:300]!r}")
    if payload.get("is_error"):
        raise ExtractionError(f"claude reported an error: {str(payload.get('result'))[:300]}")
    usage = payload.get("usage") or {}
    structured = payload.get("structured_output")
    return ExtractorResponse(
        text=str(payload.get("result") or ""),
        structured=structured if isinstance(structured, dict) else None,
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        raw=payload,
    )


def parse_output(resp: ExtractorResponse) -> tuple[list[dict], list[int]]:
    data = resp.structured if isinstance(resp.structured, dict) else _loads_lenient(resp.text)
    if not isinstance(data, dict):
        raise ExtractionError("extractor output was not an object")
    items: list[dict] = []
    raw_items = data.get("items")
    for it in (raw_items if isinstance(raw_items, list) else [])[: config.MAX_ITEMS]:
        if isinstance(it, str):
            it = {"title": it}
        if not isinstance(it, dict):
            continue
        title = " ".join(str(it.get("title") or "").split())[:200]
        if not title:
            continue
        details = " ".join(str(it.get("details") or "").split())[:1000]
        items.append({"title": title, "details": details, "topic": db.clean_topic(it.get("topic"))})
    resolved: list[int] = []
    raw_ids = data.get("resolved_ids")
    for rid in raw_ids if isinstance(raw_ids, list) else []:
        try:
            resolved.append(int(rid))
        except (TypeError, ValueError):
            continue
    return items, resolved


def _loads_lenient(text: str):
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except ValueError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except ValueError:
            pass
    raise ExtractionError(f"could not parse extractor output: {text[:200]!r}")


def _coerce(result) -> ExtractorResponse:
    if isinstance(result, ExtractorResponse):
        return result
    if isinstance(result, dict):
        return ExtractorResponse(text=json.dumps(result), structured=result)
    if isinstance(result, str):
        return ExtractorResponse(text=result)
    raise ExtractionError(f"extractor returned unsupported type {type(result).__name__}")


@dataclass
class ProcessResult:
    path: str
    session_id: str = ""
    project: str = ""
    status: str = ""  # processed | unchanged | debounced | skipped_short | skipped_extractor | missing | failed
    items_added: list[int] = field(default_factory=list)
    items_resolved: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str = ""

    @property
    def summary(self) -> str:
        extra = f" +{len(self.items_added)} -{len(self.items_resolved)} ${self.cost_usd:.3f}" if self.status == "processed" else ""
        err = f" ({self.error})" if self.error else ""
        return f"{self.status:<17} {self.project or '?':<28} {self.session_id[:8]}{extra}{err}"


@contextlib.contextmanager
def _lock():
    config.ensure_dirs()
    with open(config.LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _age_seconds(iso: str) -> float:
    return (datetime.now(timezone.utc) - db.parse_iso(iso)).total_seconds()


def process_session(
    path: str | Path,
    extractor: Extractor | None = None,
    *,
    force: bool = False,
    debounce_sec: int = 0,
    conn=None,
) -> ProcessResult:
    path = Path(path)
    res = ProcessResult(path=str(path))
    if not path.is_file():
        res.status = "missing"
        return res
    mtime = path.stat().st_mtime
    t = transcript_mod.load(path)
    res.session_id = t.session_id or path.stem
    res.project = t.project
    if t.is_extractor_session:
        res.status = "skipped_extractor"
        return res

    own_conn = conn is None
    conn = conn or db.connect()
    try:
        with _lock():
            prev = db.get_session(conn, res.session_id)
            if prev is not None and not force:
                if prev["transcript_mtime"] and abs(prev["transcript_mtime"] - mtime) < 1e-6:
                    res.status = "unchanged"
                    return res
                if debounce_sec and prev["processed_at"] and _age_seconds(prev["processed_at"]) < debounce_sec:
                    res.status = "debounced"
                    return res
            common = dict(session_id=res.session_id, transcript_path=str(path), project=t.project)
            if t.user_turns < config.MIN_USER_TURNS:
                db.record_session(conn, transcript_mtime=mtime, **common)
                res.status = "skipped_short"
                return res

            open_items = db.open_items(conn, t.project)
            prompt = build_prompt(t, open_items)
            try:
                resp = _coerce((extractor or run_claude)(prompt))
                items, resolved = parse_output(resp)
            except ExtractionError as exc:
                db.record_session(conn, transcript_mtime=0, error=str(exc)[:500], **common)
                res.status, res.error = "failed", str(exc)
                log.error("extraction_failed session=%s error=%s", res.session_id, exc)
                return res

            valid_ids = {r["id"] for r in open_items}
            for it in items:
                item_id = db.insert_item(
                    conn,
                    t.project,
                    it["title"],
                    it["details"],
                    topic=it["topic"],
                    session_id=res.session_id,
                    session_title=t.title,
                    cwd=t.cwd,
                )
                if item_id:
                    res.items_added.append(item_id)
            res.items_resolved = db.auto_resolve(conn, [i for i in resolved if i in valid_ids], res.session_id)
            res.cost_usd = resp.cost_usd
            db.record_session(
                conn,
                transcript_mtime=mtime,
                items_added=len(res.items_added),
                items_resolved=len(res.items_resolved),
                cost_usd=resp.cost_usd,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                **common,
            )
            res.status = "processed"
            log.info(
                "processed session=%s project=%s added=%d resolved=%d cost=%.4f",
                res.session_id,
                t.project,
                len(res.items_added),
                len(res.items_resolved),
                resp.cost_usd,
            )
            return res
    finally:
        if own_conn:
            conn.close()


# ---- topics ---------------------------------------------------------------------

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "topic": {"type": "string"}},
                "required": ["id", "topic"],
            },
        }
    },
    "required": ["topics"],
}

TOPIC_PROMPT = """You are labelling a developer's open action items with a short topic, so related items group together.

Return ONLY JSON of this shape: {"topics": [{"id": 12, "topic": "Centrifugo"}, ...]}

Rules:
- A topic is a 1-3 word name for the workstream: the system, feature, ticket family or activity the item belongs to
  (examples: "Centrifugo", "Snapshots v2", "Code reviews", "Prod release", "MCP auth", "Local tooling").
- Use as few topics as the items honestly allow, at most 8, and reuse the existing topics listed below when they fit.
- Every id must get exactly one topic. Do not invent ids.
"""


def _topic_extractor(prompt: str) -> ExtractorResponse:
    return run_claude(prompt, TOPIC_SCHEMA)


def categorize(conn, extractor: Extractor | None = None, *, only_missing: bool = True) -> dict[int, str]:
    """Assign topics to open and snoozed items in one model call. Returns {id: topic} for rows updated."""
    rows = [r for r in db.open_items(conn) if not only_missing or not r["topic"]]
    if not rows:
        return {}
    existing = db.topics(conn)
    listing = "\n".join(
        f"[{r['id']}] ({r['project']}) {r['title']}" + (f" — {r['details'][:200]}" if r["details"] else "") for r in rows
    )
    prompt = (
        TOPIC_PROMPT
        + "\nExisting topics: "
        + (", ".join(existing) or "(none)")
        + "\n\nItems:\n"
        + listing
        + "\n"
    )
    resp = _coerce((extractor or _topic_extractor)(prompt))
    data = resp.structured if isinstance(resp.structured, dict) else _loads_lenient(resp.text)
    valid = {r["id"] for r in rows}
    out: dict[int, str] = {}
    raw = data.get("topics") if isinstance(data, dict) else None
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            item_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        topic = db.clean_topic(entry.get("topic"))
        if item_id in valid and topic and db.set_topic(conn, item_id, topic):
            out[item_id] = topic
    log.info("categorized items=%d updated=%d cost=%.4f", len(rows), len(out), resp.cost_usd)
    return out
