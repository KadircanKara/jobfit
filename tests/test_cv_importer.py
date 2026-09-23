"""Turning today's master.tex into a profile.

The agent's output is never trusted on its word. The profile is rendered back
through Classic and compared with the master word by word, and the person
accepts that comparison, not the agent's JSON.
"""
from __future__ import annotations

import copy
import json

import pytest
from conftest import FIXTURES, load_fixture, passing

from jobhunt.config import DEFAULT_CONFIG
from jobhunt.cv import importer, model, render, templates
from jobhunt.cv import store as cvstore
from jobhunt.web import agent

PRE = (FIXTURES / "cv" / "master_preamble.tex").read_text(encoding="utf-8")
OLD = (
    PRE
    + "\\begin{document}\n\\vspace{-4pt}Hello \\textbf{World}, 10\\% faster\n% a note\n\\end{document}\n"
)


def without_ids(data):
    """What the agent is told to return: no ids, except on custom sections."""
    data = copy.deepcopy(data)

    def strip(value):
        if isinstance(value, dict):
            value.pop("id", None)
            for inner in value.values():
                strip(inner)
        elif isinstance(value, list):
            for inner in value:
                strip(inner)

    customs = data.pop("custom_sections")
    strip(data)
    for custom in customs:
        strip(custom["entries"])
    data["custom_sections"] = customs
    return data


def chatty_agent(prompt: str) -> str:
    return "Here is the profile:\n" + json.dumps(without_ids(load_fixture("cv/profile.json"))) + "\nDone."


def write_master(cfg, folder) -> str:
    """A master that the fixture profile reproduces exactly, as a real import should."""
    fixture = model.parse(copy.deepcopy(load_fixture("cv/profile.json")))
    text = render.render(fixture, templates.get(cfg, "classic").text())
    (folder / "master.tex").write_text(text, encoding="utf-8")
    return text


def desk(cfg, agent_fn=chatty_agent) -> importer.ImportDesk:
    return importer.ImportDesk(cfg, agent=agent_fn, runner=passing, background=False)


def test_import_is_a_model_phase_with_its_own_setting():
    assert "import" in agent.PHASES
    assert DEFAULT_CONFIG["models"]["import"] == {"model": None, "effort": None}


def test_extract_fills_the_ids_the_agent_left_out():
    profile = importer.extract("\\begin{document}\\end{document}", chatty_agent)

    assert profile.basics.name == "Ada Lovelace"
    assert all(entry.id for entry in profile.experience)
    assert len({b.id for e in profile.experience for b in e.bullets}) == 3
    assert profile.custom_sections[0].id == "custom-1"


def test_the_prompt_carries_the_schema_and_the_master():
    seen = []

    def spy(prompt):
        seen.append(prompt)
        return chatty_agent(prompt)

    importer.extract("THE-MASTER-TEXT", spy)

    assert "THE-MASTER-TEXT" in seen[0] and '"headline_variants"' in seen[0]


def test_an_answer_with_no_json_is_a_failed_import():
    with pytest.raises(importer.ImportFailed) as caught:
        importer.extract("x", lambda prompt: "I could not read that.")
    assert "did not return" in str(caught.value)


def test_an_invalid_profile_names_the_field():
    bad = without_ids(load_fixture("cv/profile.json"))
    bad["basics"]["name"] = ""

    with pytest.raises(importer.ImportFailed) as caught:
        importer.extract("x", lambda prompt: json.dumps(bad))
    assert "basics.name" in str(caught.value)


def test_identical_documents_are_faithful():
    report = importer.compare(OLD, OLD)

    assert report.faithful and report.missing == [] and report.comments_missing == []


def test_a_dropped_word_is_reported_missing():
    report = importer.compare(OLD, OLD.replace("World", ""))

    assert report.missing == ["world"] and not report.faithful


def test_an_added_word_is_reported():
    report = importer.compare(OLD, OLD.replace("Hello", "Hello there"))

    assert report.added == ["there"]


def test_a_dropped_comment_does_not_fail_but_is_listed():
    report = importer.compare(OLD, OLD.replace("% a note\n", ""))

    assert report.faithful and report.comments_missing == ["a", "note"]


def test_an_escaped_percent_is_text_not_a_comment():
    report = importer.compare(OLD, OLD.replace("10\\% faster", "10\\%"))

    assert report.missing == ["faster"]


def test_layout_lengths_are_not_words():
    report = importer.compare(OLD, OLD.replace("\\vspace{-4pt}Hello", "\\vspace{2pt}Hello"))

    assert report.faithful


def test_a_different_preamble_is_flagged():
    report = importer.compare(OLD, "\\documentclass{article}\n" + OLD[len(PRE):])

    assert not report.preamble_identical and not report.faithful


def test_an_import_renders_through_classic_and_is_faithful(cfg, cv_source):
    write_master(cfg, cv_source)

    snapshot = desk(cfg).start()

    assert snapshot["state"] == "done", snapshot["error"]
    assert snapshot["report"]["faithful"] and snapshot["has_preview"]


def test_accepting_writes_the_profile(cfg, cv_source):
    write_master(cfg, cv_source)
    importing = desk(cfg)
    importing.start()

    importing.accept()

    assert cvstore.read(cfg).profile.basics.name == "Ada Lovelace"
    assert importing.snapshot()["state"] == "idle"


def test_discarding_leaves_nothing_behind(cfg, cv_source):
    write_master(cfg, cv_source)
    importing = desk(cfg)
    importing.start()

    importing.discard()

    assert cvstore.read(cfg) is None and importing.snapshot()["state"] == "idle"


def test_an_import_is_refused_once_a_profile_exists(cfg, cv_source):
    write_master(cfg, cv_source)
    cvstore.write(cfg, model.parse(copy.deepcopy(load_fixture("cv/profile.json"))))

    with pytest.raises(importer.ImportFailed):
        desk(cfg).start()


def test_an_import_with_no_master_is_refused(cfg, cv_source):
    with pytest.raises(importer.ImportFailed) as caught:
        desk(cfg).start()
    assert "master.tex" in str(caught.value)


def test_accepting_before_it_finished_is_refused(cfg, cv_source):
    with pytest.raises(importer.ImportFailed):
        desk(cfg).accept()


def test_an_agent_that_fails_leaves_a_failed_import_with_the_reason(cfg, cv_source):
    write_master(cfg, cv_source)

    def broken(prompt):
        raise agent.AgentError("claude exited 1")

    snapshot = desk(cfg, broken).start()

    assert snapshot["state"] == "failed" and "claude exited 1" in snapshot["error"]


def test_the_import_agent_gets_no_tools(cfg, cv_source, monkeypatch):
    write_master(cfg, cv_source)
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout):
        seen.update(phase=phase, tools=tools)
        return chatty_agent(prompt)

    monkeypatch.setattr(agent, "run", fake_run)
    importing = importer.ImportDesk(cfg, runner=passing, background=False)

    assert importing.start()["state"] == "done"
    assert seen == {"phase": "import", "tools": ""}
