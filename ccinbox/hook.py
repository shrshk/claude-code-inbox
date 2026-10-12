"""SessionEnd / Stop hook entry point.

Must return within the SessionEnd budget (1.5 s by default), so it only spawns a
detached ``ccinbox process <transcript>`` worker and exits. Nothing is imported
here that is slow to load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ccinbox import config


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("CCINBOX_EXTRACTING"):
        return 0  # our own claude -p call ending; never recurse
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        return 0
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return 0
    event = str(payload.get("hook_event_name") or "")
    args = [sys.executable, "-m", "ccinbox.cli", "process", path]
    if event == "Stop":
        args += ["--debounce", str(config.DEBOUNCE_SEC)]
    else:
        # The worker inherits the terminal's context (login, file access), which a launchd agent may lack,
        # so every session end also picks up other idle transcripts the sweep agent could not.
        args.append("--then-sweep")
    config.ensure_dirs()
    with open(config.LOG_PATH, "ab") as logfh:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=logfh,
            stderr=logfh,
            start_new_session=True,
            close_fds=True,
        )
        logfh.write(
            f"{_now()} hook event={event or '?'} reason={payload.get('reason', '')} spawned pid={proc.pid} path={path}\n".encode()
        )
    return 0


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
