from pathlib import Path

from ccinbox import config, transcript


def test_parse_real_shape(env, transcript_path):
    t = transcript.load(transcript_path)
    assert t.session_id == "11111111-2222-3333-4444-555555555555"
    assert t.project == "demo-backend"
    assert t.cwd == "/Users/me/projects/demo-backend"
    assert t.git_branch == "fix/DEMO-42-balance-tests"
    assert t.title == "balance tests"
    assert t.cost_usd == 0.42
    assert t.bad_lines == 1
    assert t.user_turns == 3  # the meta reminder, the slash-command stdout and the tool_result are not user turns
    roles = [turn.role for turn in t.turns]
    assert roles == ["user", "tool", "tool", "assistant", "user", "assistant", "user"]
    text = t.render()
    assert "system-reminder" not in text
    assert "injected context" not in text
    assert "SIDECHAIN" not in text
    assert "private reasoning" not in text
    assert "Set model to Opus" not in text
    assert "TOOL Bash: Run the balance tests" in text
    assert "RESULT (error): FAILED tests/test_balance.py" in text
    assert "remind me to open the PR for DEMO-42" in text
    assert t.started_at.startswith("2026-10-09T14:00") and t.ended_at.startswith("2026-10-09T14:02")


def test_extractor_session_detection(env, tmp_path):
    p = tmp_path / "x.jsonl"
    from tests.conftest import write_transcript

    write_transcript(p, [("user", "hi"), ("assistant", "yo")], cwd=str(config.WORK_DIR))
    assert transcript.load(p).is_extractor_session
    write_transcript(p, [("user", "hi"), ("assistant", "yo")], cwd="/elsewhere")
    assert not transcript.load(p).is_extractor_session


def test_trim_keeps_head_and_tail():
    parts = [f"USER:\nturn {i} " + ("x" * 100) for i in range(50)]
    out = transcript.trim(parts, max_chars=1500, head_ratio=0.2)
    assert out.startswith("USER:\nturn 0 ")
    assert "turn 49 " in out
    assert "trimmed from the middle" in out
    assert "turn 25 " not in out
    head_part = out.split("[…")[0]
    assert len(head_part) <= 1500 * 0.2 + 2
    assert transcript.trim(parts[:3], max_chars=100_000, head_ratio=0.2) == "\n\n".join(parts[:3])


def test_huge_turn_is_clipped(env, tmp_path):
    from tests.conftest import write_transcript

    p = write_transcript(tmp_path / "big.jsonl", [("user", "a" * 20_000), ("assistant", "ok"), ("user", "bye")])
    text = transcript.load(p).render()
    assert "chars clipped]" in text
    assert len(text) < 8_000


def test_missing_or_odd_records_do_not_crash(env, tmp_path):
    p = tmp_path / "odd.jsonl"
    p.write_text(
        '{"type":"user","message":"not a dict"}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"X","input":"raw"}, 7]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result","content":"plain string result"}]}}\n'
        "[1,2,3]\n"
        '{"type":"weird"}\n'
    )
    t = transcript.load(p)
    assert t.bad_lines == 1
    assert [x.role for x in t.turns] == ["tool", "tool"]
    assert t.project == Path(p).parent.name


def test_slash_commands_with_args_become_user_turns(env, tmp_path):
    p = tmp_path / "cmd.jsonl"
    p.write_text(
        '{"type":"custom-title","customTitle":"t","sessionId":"sid-1"}\n'
        '{"type":"system","subtype":"local_command","content":"<command-name>/code-review</command-name>\\n<command-message>code-review</command-message>\\n<command-args>5834 --fix</command-args>","cwd":"/x/y"}\n'
        '{"type":"system","subtype":"local_command","content":"<command-name>/model</command-name>\\n<command-message>model</command-message>\\n<command-args></command-args>"}\n'
    )
    t = transcript.load(p)
    assert t.session_id == "sid-1" and t.project == "y"
    assert [(x.role, x.text) for x in t.turns] == [("user", "/code-review 5834 --fix")]
