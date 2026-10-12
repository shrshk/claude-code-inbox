"""Defensive JSONL transcript parser and head/tail trimming.

Verified against real Claude Code 2.1.x transcripts (2026-10-10). Record types seen:
``user``, ``assistant``, ``attachment``, ``system``, ``custom-title``, ``cost-state``,
``last-prompt``, ``continued-in`` and a dozen bookkeeping types. Only ``user`` /
``assistant`` carry conversation; ``isSidechain`` marks subagent chatter and
``isMeta`` marks injected context (skill bodies, system reminders).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ccinbox import config

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_SKIP_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
)
_MESSAGE_TYPES = {"user", "assistant"}
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>.*?<command-args>(.*?)</command-args>", re.S)


@dataclass
class Turn:
    role: str  # "user" | "assistant" | "tool"
    text: str
    timestamp: str = ""


@dataclass
class Transcript:
    path: Path
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    title: str = ""
    version: str = ""
    started_at: str = ""
    ended_at: str = ""
    cost_usd: float = 0.0
    continued_in: str = ""
    bad_lines: int = 0
    turns: list[Turn] = field(default_factory=list)

    @property
    def project(self) -> str:
        if self.cwd:
            return Path(self.cwd).name or self.cwd
        return self.path.parent.name

    @property
    def user_turns(self) -> int:
        return sum(1 for t in self.turns if t.role == "user")

    @property
    def is_extractor_session(self) -> bool:
        return bool(self.cwd) and _same_path(Path(self.cwd), config.WORK_DIR)

    def rendered_turns(self) -> list[str]:
        out: list[str] = []
        for t in self.turns:
            text = clip(t.text, config.MAX_TURN_CHARS)
            if t.role == "tool":
                if out and out[-1].startswith("TOOLS:"):
                    out[-1] += "\n" + text
                else:
                    out.append("TOOLS:\n" + text)
            else:
                out.append(f"{t.role.upper()}:\n{text}")
        return out

    def render(self, max_chars: int | None = None, head_ratio: float | None = None) -> str:
        return trim(
            self.rendered_turns(),
            max_chars if max_chars is not None else config.MAX_CHARS,
            head_ratio if head_ratio is not None else config.HEAD_RATIO,
        )


def load(path: str | Path) -> Transcript:
    path = Path(path)
    t = Transcript(path=path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                t.bad_lines += 1
                continue
            if not isinstance(rec, dict):
                t.bad_lines += 1
                continue
            _ingest(t, rec)
    return t


def clean_text(text: str) -> str:
    text = _REMINDER_RE.sub("", text).strip()
    if not text or text.startswith(_SKIP_PREFIXES):
        return ""
    return text


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f" […{len(text) - limit} chars clipped]"


def trim(parts: list[str], max_chars: int, head_ratio: float) -> str:
    """Keep ``head_ratio`` of the budget from the start and the rest from the end."""
    sep = 2
    total = sum(len(p) + sep for p in parts)
    if total <= max_chars:
        return "\n\n".join(parts)
    head_budget = int(max_chars * head_ratio)
    tail_budget = max_chars - head_budget
    head: list[str] = []
    used = 0
    for p in parts:
        if used + len(p) + sep > head_budget:
            break
        head.append(p)
        used += len(p) + sep
    tail: list[str] = []
    used = 0
    for p in reversed(parts):
        if used + len(p) + sep > tail_budget:
            break
        tail.append(p)
        used += len(p) + sep
    tail.reverse()
    if len(head) + len(tail) >= len(parts):
        return "\n\n".join(parts)
    dropped = parts[len(head) : len(parts) - len(tail)]
    marker = f"[… {len(dropped)} turns / {sum(len(p) for p in dropped)} chars trimmed from the middle …]"
    return "\n\n".join(head + [marker] + tail)


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except OSError:
        return False


def _ingest(t: Transcript, rec: dict) -> None:
    kind = rec.get("type")
    if not t.session_id and isinstance(rec.get("sessionId"), str):
        t.session_id = rec["sessionId"]
    if kind == "custom-title":
        title = rec.get("customTitle")
        if isinstance(title, str) and title.strip():
            t.title = title.strip()
        return
    if kind == "cost-state":
        cost = rec.get("totalCostUSD")
        if isinstance(cost, (int, float)):
            t.cost_usd = float(cost)
        return
    if kind == "continued-in":
        t.continued_in = str(rec.get("continuedInSessionId") or "")
        return
    if kind == "system" and rec.get("subtype") == "local_command":
        # a slash command the user typed, e.g. "/code-review 5834"; argument-less ones (/model, /clear) are noise
        _absorb_metadata(t, rec)
        m = _COMMAND_RE.search(str(rec.get("content") or ""))
        if m and m.group(2).strip():
            t.turns.append(Turn("user", f"{m.group(1).strip()} {m.group(2).strip()}", str(rec.get("timestamp") or "")))
        return
    if kind not in _MESSAGE_TYPES:
        return
    _absorb_metadata(t, rec)
    if rec.get("isSidechain") or rec.get("isMeta"):
        return
    message = rec.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    ts = str(rec.get("timestamp") or "")
    if kind == "user":
        _ingest_user(t, content, ts)
    else:
        _ingest_assistant(t, content, ts)


def _absorb_metadata(t: Transcript, rec: dict) -> None:
    if not t.session_id and isinstance(rec.get("sessionId"), str):
        t.session_id = rec["sessionId"]
    for attr, key in (("session_id", "sessionId"), ("cwd", "cwd"), ("git_branch", "gitBranch"), ("version", "version")):
        if not getattr(t, attr):
            val = rec.get(key)
            if isinstance(val, str) and val:
                setattr(t, attr, val)
    ts = rec.get("timestamp")
    if isinstance(ts, str) and ts:
        if not t.started_at:
            t.started_at = ts
        t.ended_at = ts


def _ingest_user(t: Transcript, content, ts: str) -> None:
    if isinstance(content, str):
        text = clean_text(content)
        if text:
            t.turns.append(Turn("user", text, ts))
        return
    if not isinstance(content, list):
        return
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            s = clean_text(str(block.get("text") or ""))
            if s:
                texts.append(s)
        elif btype == "tool_result":
            s = _tool_result_text(block.get("content"))
            if s:
                label = "RESULT (error)" if block.get("is_error") else "RESULT"
                t.turns.append(Turn("tool", f"{label}: {clip(s, config.TOOL_RESULT_CHARS)}", ts))
    if texts:
        t.turns.append(Turn("user", "\n".join(texts), ts))


def _ingest_assistant(t: Transcript, content, ts: str) -> None:
    if isinstance(content, str):
        s = content.strip()
        if s:
            t.turns.append(Turn("assistant", s, ts))
        return
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            s = str(block.get("text") or "").strip()
            if s:
                t.turns.append(Turn("assistant", s, ts))
        elif btype == "tool_use":
            name = str(block.get("name") or "tool")
            t.turns.append(Turn("tool", f"TOOL {name}: {clip(_tool_use_brief(block.get('input')), config.TOOL_USE_CHARS)}", ts))


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts).strip()
    return ""


def _tool_use_brief(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp or "")
    for key in ("description", "command", "file_path", "pattern", "query", "prompt", "url"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())
    try:
        return json.dumps(inp, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(inp)
