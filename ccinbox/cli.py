"""``ccinbox`` command line. Heavy imports stay inside the handlers so ``ccinbox hook`` starts fast."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from ccinbox import config

LAUNCHD_LABELS = {"serve": "com.ccinbox.serve", "sweep": "com.ccinbox.sweep", "digest": "com.ccinbox.digest"}


def _setup_logging(to_file: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        config.ensure_dirs()
        handlers.append(logging.FileHandler(config.LOG_PATH))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)


def _ccinbox_bin() -> str:
    # Keep the PATH entry (e.g. ~/.local/bin/ccinbox) rather than resolving it: a uv tool reinstall
    # moves the real file, and hooks and plists that point at the old location would break.
    found = shutil.which("ccinbox")
    if found:
        return str(Path(found).absolute())
    return str(Path(sys.argv[0]).absolute())


def _claude_bin() -> str:
    return shutil.which(config.CLAUDE_BIN) or config.CLAUDE_BIN


# ---- commands -------------------------------------------------------------------


def cmd_hook(args) -> int:
    from ccinbox.hook import main as hook_main

    return hook_main()


def cmd_process(args) -> int:
    _setup_logging()
    from ccinbox.extract import process_session

    res = process_session(args.path, force=args.force, debounce_sec=args.debounce)
    print(res.summary)
    if args.then_sweep:
        from ccinbox.sweep import sweep

        for r in sweep(verify_prs=True, live_only=True):
            if r.status in ("processed", "failed"):
                print(r.summary)
    return 0 if res.status != "failed" else 1


def cmd_sweep(args) -> int:
    _setup_logging()
    from ccinbox.sweep import candidate_transcripts, sweep

    if args.dry_run:
        if args.live:
            from ccinbox.live import live_transcripts

            for p in live_transcripts():
                print(p)
        else:
            for p in candidate_transcripts(args.since_days, args.idle_min):
                print(p)
        return 0
    results = sweep(args.since_days, args.idle_min, limit=args.limit, force=args.force, debounce_sec=args.debounce, verify_prs=args.verify, live_only=args.live)
    if args.json:
        from ccinbox.sweep import summary

        print(json.dumps(summary(results)))
        return 0
    for r in results:
        print(r.summary)
    cost = sum(r.cost_usd for r in results)
    print(f"{len(results)} candidates, {sum(r.status == 'processed' for r in results)} processed, ${cost:.3f}")
    if results.verify:
        print(f"verify: {results.verify.as_dict()}")
    return 0


def cmd_live(args) -> int:
    from ccinbox.live import live_sessions

    rows = live_sessions()
    for s in rows:
        print(f"{s.pid:<7} {s.session_id[:8]} {s.status:<8} {Path(s.cwd).name:<28} {s.name[:40]}{'' if s.exists else '  (no transcript yet)'}")
    print(f"{len(rows)} live session{'s' if len(rows) != 1 else ''}")
    return 0


def cmd_verify(args) -> int:
    _setup_logging()
    from ccinbox import db
    from ccinbox.verify import verify

    conn = db.connect()
    try:
        res = verify(conn)
        for item_id in res.resolved:
            print(f"closed    #{item_id:<4} {db.get_item(conn, item_id)['title']}")
        for item_id in res.annotated:
            print(f"annotated #{item_id:<4} {db.get_item(conn, item_id)['title']}")
    finally:
        conn.close()
    print(f"{res.checked} items mention a PR, {len(res.resolved)} closed, {len(res.annotated)} annotated, {res.errors} lookups failed")
    return 0


def cmd_render(args) -> int:
    from ccinbox import transcript as transcript_mod

    t = transcript_mod.load(args.path)
    print(
        f"# project={t.project} session={t.session_id} title={t.title!r} branch={t.git_branch} "
        f"user_turns={t.user_turns} turns={len(t.turns)} bad_lines={t.bad_lines} extractor={t.is_extractor_session}",
        file=sys.stderr,
    )
    text = t.render()
    print(text[: args.limit] if args.limit else text)
    return 0


def cmd_prompt(args) -> int:
    from ccinbox import db
    from ccinbox import transcript as transcript_mod
    from ccinbox.extract import build_prompt

    t = transcript_mod.load(args.path)
    conn = db.connect()
    try:
        print(build_prompt(t, db.open_items(conn, t.project)))
    finally:
        conn.close()
    return 0


def cmd_list(args) -> int:
    from ccinbox import db

    conn = db.connect()
    try:
        db.wake_snoozed(conn)
        rows = db.list_items(conn, status=args.status) if args.all or args.status else db.list_items(conn, status="open")
        if args.json:
            print(json.dumps([db.row_to_dict(r) for r in rows], indent=2))
            return 0
        project = None
        for r in rows:
            if r["project"] != project:
                project = r["project"]
                print(f"\n{project}")
            flag = "" if r["status"] == "open" else f" [{r['status']}]"
            topic = f" ({r['topic']})" if r["topic"] else ""
            print(f"  #{r['id']:<4} {r['title']}{topic}{flag}")
            if args.verbose and r["details"]:
                print(f"        {r['details']}")
        if not rows:
            print("(no items)")
    finally:
        conn.close()
    return 0


def cmd_act(args) -> int:
    from ccinbox import db

    conn = db.connect()
    try:
        row = db.apply_action(conn, args.id, args.action, args.until)
    finally:
        conn.close()
    if row is None:
        print("no such item", file=sys.stderr)
        return 1
    print(f"#{row['id']} {row['status']} {row['title']}")
    return 0


def cmd_categorize(args) -> int:
    _setup_logging()
    from ccinbox import db
    from ccinbox.extract import ExtractionError, categorize

    conn = db.connect()
    try:
        try:
            updated = categorize(conn, only_missing=not args.all)
        except ExtractionError as exc:
            print(f"categorize failed: {exc}", file=sys.stderr)
            return 1
        for item_id, topic in updated.items():
            print(f"#{item_id:<4} {topic}")
        print(f"{len(updated)} items labelled" if updated else "nothing to label")
    finally:
        conn.close()
    return 0


def cmd_stats(args) -> int:
    from ccinbox import db

    conn = db.connect()
    try:
        s = db.stats(conn)
        print(json.dumps(s, indent=2))
        if args.sessions:
            for r in db.sessions(conn, args.sessions):
                err = f" ERROR {r['error']}" if r["error"] else ""
                print(
                    f"{r['processed_at']} {r['project']:<28} {r['session_id'][:8]} +{r['items_added']} -{r['items_resolved']}"
                    f" ${r['cost_usd']:.3f} {r['input_tokens']}in/{r['output_tokens']}out{err}"
                )
    finally:
        conn.close()
    return 0


def cmd_serve(args) -> int:
    _setup_logging()
    import uvicorn

    host = args.host or config.HOST
    port = args.port or config.PORT
    if host not in ("127.0.0.1", "localhost", "::1") and not config.TOKEN:
        print("refusing to bind a non-loopback address without CCINBOX_TOKEN", file=sys.stderr)
        return 2
    uvicorn.run("ccinbox.server:app", host=host, port=port, log_level="info")
    return 0


def port_open(host: str, port: int) -> bool:
    import socket

    probe = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with socket.create_connection((probe, port), timeout=0.5):
            return True
    except OSError:
        return False


def cmd_ensure_serve(args) -> int:
    """Start ``ccinbox serve`` detached, inheriting this process's context (a terminal's, when run as a
    Claude Code hook), so the server can spawn the extractor. launchd agents cannot on some Macs."""
    import subprocess

    env = {**os.environ, **config.load_env_file()}
    host = args.host or env.get("CCINBOX_HOST") or config.HOST
    port = args.port or int(env.get("CCINBOX_PORT") or config.PORT)
    if port_open(host, port):
        return 0
    config.ensure_dirs()
    with open(config.HOME / "serve.log", "ab") as logfh:
        proc = subprocess.Popen(
            [_ccinbox_bin(), "serve", "--host", host, "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=logfh,
            stderr=logfh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    print(f"started ccinbox serve on {host}:{port} (pid {proc.pid})")
    return 0


def cmd_digest(args) -> int:
    _setup_logging()
    from ccinbox import db
    from ccinbox.notify import send_digest

    conn = db.connect()
    try:
        body = send_digest(conn, topic=args.topic, dry_run=args.dry_run)
    finally:
        conn.close()
    if not body:
        print("inbox empty, nothing sent")
    elif args.dry_run:
        print(body)
    else:
        print(f"sent digest ({body.count(chr(10)) + 1} lines)")
    return 0


def cmd_config(args) -> int:
    for k, v in config.as_dict().items():
        print(f"CCINBOX_{k}={v}")
    return 0


def hooks_snippet(with_stop: bool = False, with_serve: bool = True) -> dict:
    cmd = f"{_ccinbox_bin()} hook"
    entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
    hooks = {"SessionEnd": [entry]}
    if with_stop:
        hooks["Stop"] = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]
    if with_serve:
        hooks["SessionStart"] = [{"hooks": [{"type": "command", "command": f"{_ccinbox_bin()} ensure-serve", "timeout": 5}]}]
    return {"hooks": hooks}


def cmd_install_hooks(args) -> int:
    snippet = hooks_snippet(args.with_stop, not args.no_serve)
    if not args.write:
        print(json.dumps(snippet, indent=2))
        print("\nMerge this into ~/.claude/settings.json, or rerun with --write.", file=sys.stderr)
        return 0
    settings_path = Path(args.settings).expanduser()
    settings = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text() or "{}")
        shutil.copy2(settings_path, settings_path.with_suffix(".json.bak"))
    hooks = settings.setdefault("hooks", {})
    changed = []
    for event, entries in snippet["hooks"].items():
        existing = hooks.setdefault(event, [])
        kept = [e for e in existing if not any("ccinbox" in h.get("command", "") for h in e.get("hooks", []))]
        if kept != existing or len(kept) == len(existing):
            changed.append(event)
        hooks[event] = kept + entries  # replace any earlier ccinbox entry so path changes take effect
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"updated {settings_path} ({', '.join(changed) or 'already installed'}); backup at {settings_path}.bak")
    return 0


def render_plists(out_dir: Path, *, templates: Path | None = None) -> list[Path]:
    templates = templates or Path(__file__).resolve().parent.parent / "deploy"
    home = str(Path.home())
    env_lines = []
    for key in ("CCINBOX_TOKEN", "CCINBOX_NTFY_TOPIC", "CCINBOX_HOST", "CCINBOX_PORT", "CCINBOX_MODEL", "CCINBOX_HOME", "CCINBOX_SWEEP_IDLE_MIN", "CCINBOX_SWEEP_DAYS"):
        val = os.environ.get(key)
        if val:
            env_lines.append(f"        <key>{key}</key>\n        <string>{val}</string>")
    # launchd gives agents a bare environment; claude needs USER to find its keychain login (verified 2.1.296:
    # without it, -p reports "Not logged in" or hangs until the timeout).
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or Path(home).name
    for key, val in (("USER", user), ("LOGNAME", user), ("SHELL", os.environ.get("SHELL", "/bin/zsh")), ("LANG", os.environ.get("LANG", "en_US.UTF-8"))):
        env_lines.append(f"        <key>{key}</key>\n        <string>{val}</string>")
    subs = {
        "__CCINBOX_BIN__": _ccinbox_bin(),
        "__CLAUDE_BIN__": _claude_bin(),
        "__HOME__": home,
        "__LOG_DIR__": str(config.HOME),
        "__PATH__": f"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:{home}/.local/bin",
        "__EXTRA_ENV__": "\n".join(env_lines),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, label in LAUNCHD_LABELS.items():
        text = (templates / f"{label}.plist.template").read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        target = out_dir / f"{label}.plist"
        target.write_text(text)
        written.append(target)
    return written


def cmd_install_launchd(args) -> int:
    config.ensure_dirs()
    out_dir = Path(args.dir).expanduser()
    written = render_plists(out_dir)
    for p in written:
        print(f"wrote {p}")
    uid = os.getuid()
    if args.load:
        import subprocess

        for p in written:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(p)], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], check=True)
            print(f"loaded {p.name}")
    else:
        print("\nTo load them:")
        for p in written:
            print(f"  launchctl bootstrap gui/{uid} {p}")
    return 0


# ---- parser ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ccinbox", description="Action items from finished Claude Code sessions.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("hook", help="SessionEnd/Stop hook entry point (reads the hook payload on stdin)")
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("process", help="extract action items from one transcript")
    s.add_argument("path")
    s.add_argument("--force", action="store_true", help="reprocess even if the transcript is unchanged")
    s.add_argument("--debounce", type=int, default=0, help="skip if processed within N seconds")
    s.add_argument("--then-sweep", action="store_true", help="afterwards process other idle transcripts too")
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("sweep", help="process recent idle transcripts under ~/.claude/projects")
    s.add_argument("--since-days", type=int, default=None)
    s.add_argument("--idle-min", type=int, default=None, help="only transcripts idle this long (default from config)")
    s.add_argument("--limit", type=int, default=None, help="stop after N extractions")
    s.add_argument("--force", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="list candidates only")
    s.add_argument("--debounce", type=int, default=0, help="skip sessions processed within N seconds")
    s.add_argument("--json", action="store_true", help="print a JSON summary instead of lines")
    s.add_argument("--verify", action="store_true", help="afterwards close items whose PR is merged or closed (needs gh)")
    s.add_argument("--live", action="store_true", help="only sessions running right now (~/.claude/sessions), ignoring the window and idle gate")
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("live", help="list the Claude Code sessions running right now")
    s.set_defaults(fn=cmd_live)

    s = sub.add_parser("verify", help="close open items whose PR GitHub says is merged or closed (needs gh)")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("ensure-serve", help="start the server detached if nothing listens on its port (SessionStart hook)")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_ensure_serve)

    s = sub.add_parser("render", help="print the trimmed transcript the extractor would see")
    s.add_argument("path")
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser("prompt", help="print the full prompt for a transcript")
    s.add_argument("path")
    s.set_defaults(fn=cmd_prompt)

    s = sub.add_parser("list", help="list items")
    s.add_argument("--all", action="store_true")
    s.add_argument("--status", choices=["open", "snoozed", "done", "dismissed", "auto_resolved"])
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("act", help="apply an action to an item")
    s.add_argument("action", choices=["done", "dismiss", "snooze", "undo", "reopen"])
    s.add_argument("id", type=int)
    s.add_argument("--until", help="ISO time for snooze")
    s.set_defaults(fn=cmd_act)

    s = sub.add_parser("categorize", help="label open items with a topic (one model call)")
    s.add_argument("--all", action="store_true", help="relabel items that already have a topic")
    s.set_defaults(fn=cmd_categorize)

    s = sub.add_parser("stats", help="item counts and extraction cost log")
    s.add_argument("--sessions", type=int, default=0, help="also print the last N sessions")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("serve", help="run the API + PWA")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("digest", help="post the open-items digest to ntfy")
    s.add_argument("--topic", default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("config", help="print the effective configuration")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("install-hooks", help="print or write the Claude Code hooks entry")
    s.add_argument("--write", action="store_true", help="merge into the settings file")
    s.add_argument("--with-stop", action="store_true", help="also hook Stop (end of every turn, debounced) so long-lived sessions update too")
    s.add_argument("--no-serve", action="store_true", help="do not add the SessionStart hook that keeps the server running")
    s.add_argument("--settings", default="~/.claude/settings.json")
    s.set_defaults(fn=cmd_install_hooks)

    s = sub.add_parser("install-launchd", help="render launchd plists for serve, sweep and digest")
    s.add_argument("--dir", default="~/Library/LaunchAgents")
    s.add_argument("--load", action="store_true", help="launchctl bootstrap them too")
    s.set_defaults(fn=cmd_install_launchd)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
