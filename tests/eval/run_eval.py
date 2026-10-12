"""Rerun the extractor over judged real sessions and print precision.

Layout (``tests/eval/``):
    cases/<id>.txt   rendered, trimmed transcript (gitignored: may contain private content)
    cases/<id>.json  {"project", "title", "judged": {"<item title>": "act" | "noise"}}

    python tests/eval/run_eval.py                # rerun every case with the real claude CLI
    python tests/eval/run_eval.py --add <jsonl>  # render a transcript into cases/ for judging
    python tests/eval/run_eval.py --offline      # score the saved outputs instead of calling claude

An extracted title counts as judged when its fingerprint equals a judged title's fingerprint, or
one contains the other. Precision = act / (act + noise) over judged items; unjudged items are
printed so you can add them to the json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ccinbox import db, transcript  # noqa: E402
from ccinbox.extract import PROMPT_HEADER, parse_output, run_claude  # noqa: E402

CASES = Path(__file__).parent / "cases"
RUNS = Path(__file__).parent / "runs"


def add_case(path: str) -> Path:
    t = transcript.load(path)
    CASES.mkdir(exist_ok=True)
    stem = t.session_id[:8] or Path(path).stem[:8]
    (CASES / f"{stem}.txt").write_text(t.render())
    meta = CASES / f"{stem}.json"
    if not meta.exists():
        meta.write_text(json.dumps({"project": t.project, "title": t.title, "branch": t.git_branch, "judged": {}}, indent=2))
    return meta


def prompt_for(case: dict, body: str) -> str:
    return (
        PROMPT_HEADER
        + "\nOpen items:\n(none)\n\n---\nProject: "
        + case.get("project", "?")
        + "\nBranch: "
        + case.get("branch", "?")
        + "\nSession title: "
        + (case.get("title") or "(untitled)")
        + "\n\nTranscript (tool output abbreviated; may be trimmed in the middle):\n\n"
        + body
        + "\n"
    )


def match(title: str, judged: dict) -> str | None:
    fp = db.fingerprint(title)
    for jt, verdict in judged.items():
        jfp = db.fingerprint(jt)
        if fp == jfp or (len(fp) > 12 and (fp in jfp or jfp in fp)):
            return verdict
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", help="render this transcript into cases/")
    ap.add_argument("--offline", action="store_true", help="score runs/ instead of calling claude")
    ap.add_argument("--only", help="run a single case stem")
    args = ap.parse_args()
    if args.add:
        print(f"added {add_case(args.add)}; fill in 'judged' after the first run")
        return 0

    RUNS.mkdir(exist_ok=True)
    act = noise = unjudged = 0
    cost = 0.0
    for meta in sorted(CASES.glob("*.json")):
        stem = meta.stem
        if args.only and stem != args.only:
            continue
        case = json.loads(meta.read_text())
        body_path = CASES / f"{stem}.txt"
        run_path = RUNS / f"{stem}.json"
        if args.offline:
            if not run_path.exists():
                continue
            out = json.loads(run_path.read_text())
        else:
            if not body_path.exists():
                print(f"{stem}: no rendered transcript (gitignored); skipping")
                continue
            resp = run_claude(prompt_for(case, body_path.read_text()))
            items, _ = parse_output(resp)
            out = {"items": items, "cost_usd": resp.cost_usd, "input_tokens": resp.input_tokens}
            run_path.write_text(json.dumps(out, indent=2))
        cost += out.get("cost_usd", 0)
        print(f"\n{stem}  {case.get('title') or ''}  ${out.get('cost_usd', 0):.3f}")
        for it in out["items"]:
            verdict = match(it["title"], case.get("judged", {}))
            if verdict == "act":
                act += 1
            elif verdict == "noise":
                noise += 1
            else:
                unjudged += 1
            print(f"  [{verdict or '?':5}] {it['title']}")
        if not out["items"]:
            print("  (no items)")
    judged = act + noise
    print(f"\nprecision {act}/{judged} = {act / judged:.0%}" if judged else "\nno judged items yet")
    print(f"unjudged {unjudged}, cost ${cost:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
