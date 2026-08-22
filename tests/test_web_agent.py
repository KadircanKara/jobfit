"""Which model runs which phase.

Unset means inherit, which is what every phase did before this existed. The
rule worth protecting is that naming nothing changes nothing.
"""
from __future__ import annotations

import pytest

from jobhunt.web import agent as agent_module


def configured(cfg, **phases):
    cfg.raw["models"] = {phase: dict(values) for phase, values in phases.items()}
    return cfg


def test_a_project_that_names_nothing_passes_no_flags(cfg) -> None:
    """The whole point of the default: behaviour is unchanged until asked."""
    assert agent_module.flags(cfg, "gate") == []


def test_a_named_model_reaches_the_command_line(cfg) -> None:
    configured(cfg, revise={"model": "sonnet"})

    assert agent_module.flags(cfg, "revise") == ["--model", "sonnet"]


def test_model_and_effort_are_set_independently(cfg) -> None:
    configured(cfg, gate={"model": "claude-sonnet-5", "effort": "medium"},
               tailor={"effort": "max"})

    assert agent_module.flags(cfg, "gate") == [
        "--model", "claude-sonnet-5", "--effort", "medium",
    ]
    assert agent_module.flags(cfg, "tailor") == ["--effort", "max"]


def test_each_phase_is_configured_on_its_own(cfg) -> None:
    """Scoring a batch and rewriting a CV are not the same task and must not be
    forced onto one setting."""
    configured(cfg, revise={"model": "haiku"}, tailor={"model": "opus"})

    assert agent_module.flags(cfg, "revise") == ["--model", "haiku"]
    assert agent_module.flags(cfg, "tailor") == ["--model", "opus"]
    assert agent_module.flags(cfg, "review") == []


def test_an_unknown_effort_is_dropped_rather_than_passed_through(cfg) -> None:
    """Passing it on would make the CLI reject every call in that phase."""
    configured(cfg, gate={"effort": "ludicrous"})

    assert agent_module.flags(cfg, "gate") == []


def test_a_bad_effort_stops_the_app_starting_and_names_the_key(cfg) -> None:
    configured(cfg, gate={"effort": "ludicrous"})

    with pytest.raises(ValueError, match="models.gate.effort"):
        agent_module.check(cfg)


def test_a_config_that_names_nothing_passes_the_check(cfg) -> None:
    agent_module.check(cfg)


def test_the_argv_keeps_the_tool_grant_the_phase_was_given(cfg) -> None:
    """--allowedTools "" is the gate's safety property, not a nicety."""
    argv = agent_module.argv_for(cfg, "gate", tools="")

    assert argv[argv.index("--allowedTools") + 1] == ""
    assert "-p" in argv and "--output-format" in argv


def test_the_envelope_is_unwrapped_and_raw_output_survives() -> None:
    assert agent_module.text_of('{"result": "the answer"}') == "the answer"
    assert agent_module.text_of("not json at all") == "not json at all"
    assert agent_module.text_of('{"no_result": 1}') == '{"no_result": 1}'
