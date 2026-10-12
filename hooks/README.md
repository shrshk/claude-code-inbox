# Hook install

`ccinbox install-hooks` prints `settings.snippet.json` with `__CCINBOX_BIN__` replaced by the
absolute path of the installed `ccinbox` script (Claude Code's hook environment has a minimal PATH).
`ccinbox install-hooks --write` merges it into `~/.claude/settings.json` (backup written first).

Add `--with-stop` to also hook `Stop`. That fires after every turn, so the worker is debounced by
`CCINBOX_DEBOUNCE_SEC` (30 min). Use it only if you rarely exit sessions; the 30-minute sweep is the
cheaper backstop.

Payload verified against Claude Code 2.1.296 (`hooks.md`, 2026-10-10): SessionEnd delivers
`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `reason` on stdin. SessionEnd hooks share
a 1.5 s budget unless a per-hook `timeout` raises it; the hook only spawns a detached worker.
