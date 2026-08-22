"""Stage 2 scoring, handed to `claude -p` because a worker has no session.

The rules that matter: the subprocess is given no tools, a bad answer is
retried once and then left ungated rather than guessed at, and jobs that were
never scored stay in the corpus for the next run.
"""
from __future__ import annotations

import json

import pytest

from jobhunt.web import gate as gate_module


def runner_returning(*payloads):
    """A fake `claude -p` that answers with each payload in turn."""
    answers = list(payloads)
    calls: list[list[str]] = []

    def run(argv, prompt, timeout):
        calls.append(argv)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    run.calls = calls
    return run


VERDICTS = json.dumps(
    {"result": json.dumps([{"job_id": 12, "score": 0.81, "reasoning": "close match", "red_flags": []}])}
)


def test_a_good_answer_becomes_verdicts():
    gate = gate_module.Gate(runner=runner_returning(VERDICTS))

    verdicts = gate.score(prompt="score these", batch_size=1)

    assert verdicts[0]["job_id"] == 12
    assert verdicts[0]["score"] == 0.81


def test_the_subprocess_is_given_no_tools():
    run = runner_returning(VERDICTS)
    gate = gate_module.Gate(runner=run)

    gate.score(prompt="score these", batch_size=1)

    argv = run.calls[0]
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""


def test_a_malformed_answer_is_retried_once():
    run = runner_returning("not json at all", VERDICTS)
    gate = gate_module.Gate(runner=run)

    verdicts = gate.score(prompt="score these", batch_size=1)

    assert len(run.calls) == 2
    assert verdicts[0]["job_id"] == 12


def test_a_batch_that_stays_malformed_is_left_ungated():
    run = runner_returning("nope", "still nope")
    gate = gate_module.Gate(runner=run)

    verdicts = gate.score(prompt="score these", batch_size=1)

    assert verdicts == []


def test_a_timeout_is_retried_then_given_up_on():
    run = runner_returning(TimeoutError("slow"), TimeoutError("slow again"))
    gate = gate_module.Gate(runner=run)

    verdicts = gate.score(prompt="score these", batch_size=1)

    assert verdicts == []
    assert len(run.calls) == 2


def test_a_verdict_missing_its_job_id_is_dropped_not_guessed():
    payload = json.dumps({"result": json.dumps([{"score": 0.9}, {"job_id": 3, "score": 0.4}])})
    gate = gate_module.Gate(runner=runner_returning(payload))

    verdicts = gate.score(prompt="score these", batch_size=2)

    assert [v["job_id"] for v in verdicts] == [3]


def test_a_score_outside_zero_to_one_is_rejected():
    payload = json.dumps({"result": json.dumps([{"job_id": 3, "score": 4.2}])})
    gate = gate_module.Gate(runner=runner_returning(payload))

    assert gate.score(prompt="score these", batch_size=1) == []


def test_answers_wrapped_in_prose_still_parse():
    """Claude sometimes explains itself around the array. Take the array."""
    inner = 'Here you go:\n```json\n[{"job_id": 5, "score": 0.7}]\n```\n'
    gate = gate_module.Gate(runner=runner_returning(json.dumps({"result": inner})))

    verdicts = gate.score(prompt="score these", batch_size=1)

    assert verdicts[0]["job_id"] == 5


@pytest.mark.parametrize("missing", ["", "   "])
def test_an_empty_answer_is_treated_as_a_failure(missing):
    gate = gate_module.Gate(runner=runner_returning(missing, missing))

    assert gate.score(prompt="score these", batch_size=1) == []


def test_the_gate_runs_on_the_model_its_phase_names(cfg):
    cfg.raw["models"] = {"gate": {"model": "claude-sonnet-5", "effort": "medium"}}

    argv = gate_module.Gate(config=cfg).argv()

    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--allowedTools") + 1] == "", "still no tools"


def test_a_gate_with_no_config_named_still_inherits(cfg):
    argv = gate_module.Gate(config=cfg).argv()

    assert "--model" not in argv and "--effort" not in argv
