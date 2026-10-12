# `claude -p` hangs when started by launchd

Observed on macOS 26 (Darwin 25.6) with Claude Code 2.1.296, 2026-10-12. Reproduced many times; never reproduced from a terminal.

## Symptom

A launchd user agent (`gui/<uid>`) runs the extractor command:

```
claude -p --output-format json --model sonnet --tools "" --strict-mcp-config \
  --setting-sources "" --no-session-persistence --disable-slash-commands "say hi"
```

The process authenticates, opens HTTPS connections to the API, then stops. `sample` shows the main thread parked in `openat$NOCANCEL` at 0% CPU for as long as you wait. `claude --version` from the same agent returns normally. The identical command from a terminal, a Python `subprocess` with a minimal environment, or a `setsid` child completes in about two seconds.

## Ruled out

Each variant was run under launchd and still hung unless noted.

| Variant | Result |
|---|---|
| no `USER` in the environment | fails fast with "Not logged in" (so `USER` is required, but not sufficient) |
| `USER`, `LOGNAME`, `SHELL`, `LANG`, `TMPDIR` set | hang |
| `CLAUDE_CODE_PLUGIN_DIRS=` (no plugins) | hang |
| `--disable-slash-commands` (no skills) | hang |
| prompt on stdin, as an argument, with stdin `/dev/null` | hang |
| under `script -q /dev/null` (a pty) | hang |
| `TMPDIR=/tmp`, cwd `/tmp` | hang |
| `ProcessType Interactive`, `Nice 0`, `LimitLoadToSessionType Aqua` | hang |
| `NumberOfFiles 4096` | hang |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | hang |
| `security find-generic-password -s "Claude Code-credentials" -w` from launchd | works in 1 s |
| scratch `HOME` (to bisect `~/.claude`) | fails fast with "Not logged in", so cannot bisect |

`lldb` attach is refused by the hardened runtime. `fs_usage` needs root and was not run.

## Observations

- A launchd `cat` of a file under `~/Documents` returns `Operation not permitted` at once, not a block. Whether a signed binary gets a consent prompt instead was not determined.
- The unified log showed nothing from `tccd`, `securityd` or `SecurityAgent` about the process.

## Workaround

Extraction runs from the `SessionEnd` hook, which inherits the terminal's context. The hook worker also sweeps idle transcripts (`ccinbox process <path> --then-sweep`). Keep the `serve` and `digest` agents loaded; leave `sweep` unloaded.

## If you want to dig

With an extraction hung, `sudo fs_usage -w -f filesys -p <pid>` will name the path the process is trying to open. Please open an issue with that line.
