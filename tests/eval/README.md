# Prompt eval

Judged real sessions. `cases/*.txt` (the rendered transcripts) are gitignored because they can
contain private content; `cases/*.json` hold the project, title and your verdict per extracted
title (`act` = you would act on it, `noise` = you would not). `runs/*.json` are the last outputs.

    python tests/eval/run_eval.py --add ~/.claude/projects/<dir>/<session>.jsonl
    python tests/eval/run_eval.py            # calls claude, prints precision
    python tests/eval/run_eval.py --offline  # rescore saved outputs after editing judgements
