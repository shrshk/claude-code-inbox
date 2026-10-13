"""Every tunable. Override any value with a ``CCINBOX_<NAME>`` environment variable.

Values are read at import and again on :func:`reload`, so tests can point
``CCINBOX_HOME`` at a temp dir. Other modules must read ``config.X`` at call
time, never ``from ccinbox.config import X``.
"""

from __future__ import annotations

import os
from pathlib import Path


def _get(name: str, default) -> str:
    return os.environ.get(f"CCINBOX_{name}", str(default))


def _int(name: str, default: int) -> int:
    return int(_get(name, default))


def _float(name: str, default: float) -> float:
    return float(_get(name, default))


def reload() -> None:
    g = globals()
    home = Path(_get("HOME", Path.home() / ".cc-inbox")).expanduser()
    g["HOME"] = home
    g["DB_PATH"] = Path(_get("DB", home / "ccinbox.db")).expanduser()
    g["WORK_DIR"] = Path(_get("WORK_DIR", home / "work")).expanduser()
    g["LOG_PATH"] = Path(_get("LOG", home / "ccinbox.log")).expanduser()
    g["LOCK_PATH"] = home / "extract.lock"
    g["PROJECTS_DIR"] = Path(_get("PROJECTS_DIR", Path.home() / ".claude" / "projects")).expanduser()

    # extractor
    g["CLAUDE_BIN"] = _get("CLAUDE_BIN", "claude")
    g["MODEL"] = _get("MODEL", "sonnet")
    g["MAX_BUDGET_USD"] = _float("MAX_BUDGET_USD", 0.50)
    g["CLAUDE_TIMEOUT_SEC"] = _int("CLAUDE_TIMEOUT_SEC", 300)
    g["MAX_ITEMS"] = _int("MAX_ITEMS", 5)

    # transcript rendering / trimming
    g["MAX_CHARS"] = _int("MAX_CHARS", 80_000)
    g["HEAD_RATIO"] = _float("HEAD_RATIO", 0.2)
    g["MAX_TURN_CHARS"] = _int("MAX_TURN_CHARS", 6_000)
    g["TOOL_RESULT_CHARS"] = _int("TOOL_RESULT_CHARS", 300)
    g["TOOL_USE_CHARS"] = _int("TOOL_USE_CHARS", 200)
    g["MIN_USER_TURNS"] = _int("MIN_USER_TURNS", 2)

    # scheduling
    g["SWEEP_DAYS"] = _int("SWEEP_DAYS", 1)
    g["SWEEP_IDLE_MIN"] = _int("SWEEP_IDLE_MIN", 20)
    g["DEBOUNCE_SEC"] = _int("DEBOUNCE_SEC", 900)
    g["REFRESH_DEBOUNCE_SEC"] = _int("REFRESH_DEBOUNCE_SEC", 300)
    g["SNOOZE_HOUR"] = _int("SNOOZE_HOUR", 9)
    g["RESOLVED_STRIP_DAYS"] = _int("RESOLVED_STRIP_DAYS", 7)

    # server / notifications
    g["HOST"] = _get("HOST", "127.0.0.1")
    g["PORT"] = _int("PORT", 8787)
    g["TOKEN"] = _get("TOKEN", "")
    g["NTFY_URL"] = _get("NTFY_URL", "https://ntfy.sh").rstrip("/")
    g["NTFY_TOPIC"] = _get("NTFY_TOPIC", "")
    g["JIRA_URL"] = _get("JIRA_URL", "").rstrip("/")  # e.g. https://yourco.atlassian.net; empty = no Jira links
    g["GITHUB_URL"] = _get("GITHUB_URL", "https://github.com").rstrip("/")
    g["WEB_DIR"] = Path(_get("WEB_DIR", Path(__file__).resolve().parent.parent / "web")).expanduser()


def ensure_dirs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def as_dict() -> dict[str, str]:
    keys = [k for k, v in globals().items() if k.isupper() and not k.startswith("_")]
    return {k: str(globals()[k]) for k in sorted(keys)}


reload()


def load_env_file(path=None) -> dict[str, str]:
    """Read ``export KEY=value`` lines from ~/.cc-inbox/env (the file install-launchd and ensure-serve use)."""
    path = Path(path) if path else HOME / "env"
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line or line.startswith("#"):
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out
