"""The revision studio.

The rules worth protecting: the shipped folder is untouched until sync, a draft
that does not build costs the preview and not the CV, and a refusal to fabricate
is an outcome rather than an error.
"""
from __future__ import annotations

import pathlib

import pytest

from jobhunt.web import revise as revise_module

TEX = "\\documentclass{article}\\begin{document}Kadircan\\end{document}\n"


class FakeLatex:
    """A toolchain that builds unless the tex says otherwise."""

    def __init__(self, pages: int = 2) -> None:
        self.pages = pages
        self.builds = 0

    def build(self, tex: pathlib.Path):
        self.builds += 1
        source = tex.read_text(encoding="utf-8")
        if "BREAK" in source:
            return False, "! Missing } inserted.\nl.148 \\end{itemize}", b""
        pdf = b"%PDF-1.4\n" + b"/Type /Page\n" * self.pages
        return True, f"Output written on cv.pdf ({self.pages} pages, 1234 bytes).", pdf


class FakeAgent:
    """An agent whose answers the test dictates, and which edits like the real
    one does: by writing to cv.tex in the draft folder."""

    def __init__(self, answers=None) -> None:
        self.answers = list(answers or [])
        self.seen: list[str] = []
        self.masters: list[str] = []

    def revise(self, *, draft, message, master):
        self.seen.append(message)
        self.masters.append(master)
        answer = self.answers.pop(0) if self.answers else {
            "summary": "Done.", "changes": ["SUMMARY > rewrote the opening"],
        }
        edit = answer.pop("_writes", None)
        if edit is not None:
            (pathlib.Path(draft) / "cv.tex").write_text(edit, encoding="utf-8")
        return answer


@pytest.fixture
def shipped(tmp_path) -> pathlib.Path:
    folder = tmp_path / "Tailored CVs" / "Arc Bank - Backend Engineer"
    folder.mkdir(parents=True)
    (folder / "cv.tex").write_text(TEX, encoding="utf-8")
    (folder / "cv.pdf").write_bytes(b"%PDF-old")
    (folder / "Kadircan_Kara-CV.pdf").write_bytes(b"%PDF-old")
    (folder / "jd.txt").write_text("the posting", encoding="utf-8")
    return folder


def desk(cfg, agent=None, latex=None) -> revise_module.ReviseDesk:
    return revise_module.ReviseDesk(
        cfg, agent=agent or FakeAgent(), latex=latex or FakeLatex()
    )


def opened(cfg, shipped, agent=None, latex=None):
    board = desk(cfg, agent, latex)
    session = board.open(7, folder=str(shipped), title="Backend Engineer", company="Arc Bank")
    return board, session


def finish(board, job_id=7):
    """Wait out the background revision the way the browser polls for it."""
    for _ in range(200):
        session = board.get(job_id)
        if not session.thinking:
            return session
        import time

        time.sleep(0.01)
    raise AssertionError("the revision never finished")


# --- opening ------------------------------------------------------------------


def test_opening_copies_the_folder_and_leaves_it_alone(cfg, shipped) -> None:
    board, session = opened(cfg, shipped)

    assert pathlib.Path(session.draft) != shipped
    assert (pathlib.Path(session.draft) / "cv.tex").read_text() == TEX
    assert (shipped / "cv.pdf").read_bytes() == b"%PDF-old", "the folder is untouched"


def test_opening_twice_resumes_the_same_conversation(cfg, shipped) -> None:
    board, first = opened(cfg, shipped)
    board.send(7, "make it shorter")
    finish(board)

    again = board.open(7, folder=str(shipped))

    assert again is first
    assert len(again.turns) > 1, "reopening must not wipe the thread"


def test_a_folder_with_no_cv_is_refused_in_words(cfg, tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(revise_module.ReviseError, match="nothing to revise"):
        desk(cfg).open(7, folder=str(empty))


def test_the_studio_opens_on_a_greeting_that_states_the_build(cfg, shipped) -> None:
    _, session = opened(cfg, shipped)

    assert session.turns[0].role == "agent"
    assert session.turns[0].build == "ok"
    assert session.pages == 2


# --- revising -----------------------------------------------------------------


def test_a_revision_records_what_changed_and_cuts_a_version(cfg, shipped) -> None:
    agent = FakeAgent([{
        "summary": "Rewritten.",
        "changes": ["SUMMARY > led on the LLM work"],
        "_writes": TEX.replace("Kadircan", "Kadircan Kara"),
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "lead with the LLM work")
    session = finish(board)

    last = session.turns[-1]
    assert last.role == "agent" and last.build == "ok"
    assert last.changes == ["SUMMARY > led on the LLM work"]
    assert session.version == 2 and last.version == 2


def test_your_own_words_are_kept_in_the_thread(cfg, shipped) -> None:
    board, _ = opened(cfg, shipped)

    board.send(7, "drop the intern role")
    session = finish(board)

    yours = [turn for turn in session.turns if turn.role == "you"]
    assert [turn.text for turn in yours] == ["drop the intern role"]


def test_an_empty_message_is_refused_before_the_agent_is_called(cfg, shipped) -> None:
    agent = FakeAgent()
    board, _ = opened(cfg, shipped, agent=agent)

    with pytest.raises(revise_module.ReviseError, match="what you would like changed"):
        board.send(7, "   ")
    assert agent.seen == []


def test_a_second_message_is_refused_while_the_agent_is_working(cfg, shipped) -> None:
    board, session = opened(cfg, shipped)
    session.thinking = True

    with pytest.raises(revise_module.ReviseError, match="still working"):
        board.send(7, "and another thing")


def test_a_refusal_to_fabricate_is_an_answer_not_a_failure(cfg, shipped) -> None:
    """The reviewer catches invented claims after the fact. Saying so when it is
    asked for is cheaper, and it must not look like the run broke."""
    agent = FakeAgent([{
        "summary": "Kubernetes is not in your master CV, so I have not added it.",
        "changes": [],
        "refused": True,
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "add Kubernetes to the skills line")
    session = finish(board)

    last = session.turns[-1]
    assert last.refused and last.changes == []
    assert session.version == 1, "a refusal cuts no version"
    assert session.error is None


# --- when the build breaks ------------------------------------------------------


def test_a_broken_edit_is_rolled_back_and_the_preview_survives(cfg, shipped) -> None:
    agent = FakeAgent([{
        "summary": "Tightened the skills block.",
        "changes": ["SKILLS > tightened"],
        "_writes": TEX + "BREAK",
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "tighten the skills block")
    session = finish(board)

    last = session.turns[-1]
    assert last.build == "failed"
    assert "Missing }" in last.log
    assert session.version == 1, "a build that failed is not a version"
    assert (pathlib.Path(session.draft) / "cv.tex").read_text() == TEX, "rolled back"


def test_a_broken_edit_leaves_a_preview_that_still_builds(cfg, shipped) -> None:
    agent = FakeAgent([{"summary": "x", "changes": ["a"], "_writes": TEX + "BREAK"}])
    board, _ = opened(cfg, shipped, agent=agent)
    board.send(7, "break it")
    finish(board)

    preview = board.preview(7)

    assert preview["ok"] and preview["pdf"]


def test_an_agent_that_dies_says_so_without_losing_the_session(cfg, shipped) -> None:
    class Exploding:
        def revise(self, **kwargs):
            raise RuntimeError("claude exited 1")

    board, _ = opened(cfg, shipped, agent=Exploding())

    board.send(7, "anything")
    session = finish(board)

    assert "claude exited 1" in session.error
    assert session.turns[-1].role == "agent"
    assert not session.thinking


# --- syncing --------------------------------------------------------------------


def test_the_folder_only_changes_when_you_sync(cfg, shipped) -> None:
    agent = FakeAgent([{"summary": "ok", "changes": ["a"], "_writes": TEX.replace("Kadircan", "Ada")}])
    board, _ = opened(cfg, shipped, agent=agent)
    board.send(7, "rename")
    finish(board)

    assert (shipped / "cv.tex").read_text() == TEX, "still what the batch shipped"

    board.sync(7)

    assert "Ada" in (shipped / "cv.tex").read_text()


def test_syncing_refreshes_the_pdf_that_actually_gets_sent(cfg, shipped) -> None:
    """The skill leaves a PDF named for the person, and that is the file an
    employer receives. A sync that left it stale would be the worst half-write."""
    agent = FakeAgent([{"summary": "ok", "changes": ["a"], "_writes": TEX.replace("Kadircan", "Ada")}])
    board, _ = opened(cfg, shipped, agent=agent)
    board.send(7, "rename")
    finish(board)

    board.sync(7)

    assert (shipped / "cv.pdf").read_bytes().startswith(b"%PDF-1.4")
    assert (shipped / "Kadircan_Kara-CV.pdf").read_bytes().startswith(b"%PDF-1.4")


def test_syncing_clears_the_drift_and_then_refuses_to_repeat(cfg, shipped) -> None:
    agent = FakeAgent([{"summary": "ok", "changes": ["a"], "_writes": TEX + "% edit\n"}])
    board, _ = opened(cfg, shipped, agent=agent)
    board.send(7, "edit")
    finish(board)
    assert board.get(7).ahead == 1

    board.sync(7)

    assert board.get(7).ahead == 0
    with pytest.raises(revise_module.ReviseError, match="nothing to sync"):
        board.sync(7)


def test_a_draft_that_does_not_build_is_never_written_to_the_folder(cfg, shipped) -> None:
    board, session = opened(cfg, shipped)
    session.version = 2  # a revision landed
    (pathlib.Path(session.draft) / "cv.tex").write_text(TEX + "BREAK", encoding="utf-8")

    with pytest.raises(revise_module.ReviseError, match="does not build"):
        board.sync(7)
    assert (shipped / "cv.tex").read_text() == TEX


def test_discarding_goes_back_to_what_the_batch_shipped(cfg, shipped) -> None:
    agent = FakeAgent([{"summary": "ok", "changes": ["a"], "_writes": TEX.replace("Kadircan", "Ada")}])
    board, _ = opened(cfg, shipped, agent=agent)
    board.send(7, "rename")
    finish(board)

    session = board.discard(7)

    assert (pathlib.Path(session.draft) / "cv.tex").read_text() == TEX
    assert session.ahead == 0
    assert len(session.turns) == 1, "a discarded draft starts a fresh thread"


# --- what the browser reads -----------------------------------------------------


def test_over_length_is_reported_but_never_blocks_a_sync(cfg, shipped) -> None:
    """Two pages is enforced when the batch cuts a CV. Past that it is the
    person's own document."""
    agent = FakeAgent([{"summary": "ok", "changes": ["a"], "_writes": TEX + "% long\n"}])
    board, _ = opened(cfg, shipped, agent=agent, latex=FakeLatex(pages=3))
    board.send(7, "add a section")
    finish(board)

    assert board.get(7).as_dict()["over_length"] is True
    board.sync(7)
    assert board.get(7).ahead == 0


def test_the_payload_never_carries_the_tex(cfg, shipped) -> None:
    """The file is not shown and not editable, so it has no business crossing
    the wire on every poll."""
    board, _ = opened(cfg, shipped)

    body = board.get(7).as_dict()

    assert "documentclass" not in repr(body)


# --- questions, not just edits --------------------------------------------------


def test_a_question_is_answered_without_touching_the_cv(cfg, shipped) -> None:
    """Not every message is an edit. Asking what a posting wants should cost a
    reply, not a rewrite."""
    agent = FakeAgent([{
        "kind": "answer",
        "text": "It asks for working German, which the master does not claim.",
        "changes": [],
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "does this posting need German?")
    session = finish(board)

    last = session.turns[-1]
    assert last.kind == "answer"
    assert not last.refused, "a question is not a refusal"
    assert last.build is None and last.changes == []
    assert session.version == 1, "an answer cuts no version"
    assert session.ahead == 0, "and leaves nothing to sync"


def test_an_answer_that_edited_anyway_is_put_back(cfg, shipped) -> None:
    """The agent's own account of a turn is the one thing that cannot be
    checked, so the file is compared rather than believed."""
    agent = FakeAgent([{
        "kind": "answer",
        "text": "It wants German.",
        "changes": [],
        "_writes": TEX.replace("Kadircan", "Ada"),
    }])
    board, session = opened(cfg, shipped, agent=agent)

    board.send(7, "does this posting need German?")
    session = finish(board)

    assert (pathlib.Path(session.draft) / "cv.tex").read_text() == TEX
    assert "put the file back" in session.turns[-1].text
    assert session.version == 1


def test_an_edit_is_still_an_edit(cfg, shipped) -> None:
    agent = FakeAgent([{
        "kind": "edit",
        "text": "Rewritten.",
        "changes": ["SUMMARY > led on the LLM work"],
        "_writes": TEX + "% edited\n",
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "lead with the LLM work")
    session = finish(board)

    assert session.turns[-1].kind == "edit"
    assert session.version == 2


def test_a_refusal_is_told_apart_from_an_answer(cfg, shipped) -> None:
    agent = FakeAgent([{
        "kind": "refusal",
        "text": "Kubernetes is not in your master CV.",
        "changes": [],
    }])
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "add Kubernetes")
    session = finish(board)

    assert session.turns[-1].kind == "refusal"
    assert session.turns[-1].refused is True


def test_an_edit_that_edited_nothing_is_read_as_an_answer() -> None:
    """Trust the artefact over the label: no changes means nothing changed."""
    assert revise_module._kind_of({"kind": "edit"}, []) == "answer"
    assert revise_module._kind_of({"kind": "edit"}, ["a"]) == "edit"


def test_a_missing_kind_is_inferred_from_what_came_back() -> None:
    """The contract is a model's output, so it has to survive the model
    ignoring it."""
    assert revise_module._kind_of({}, ["SUMMARY > rewrote"]) == "edit"
    assert revise_module._kind_of({}, []) == "answer"
    assert revise_module._kind_of({"refused": True}, []) == "refusal"
    assert revise_module._kind_of({"kind": "chat"}, []) == "answer"


def test_the_older_summary_field_is_still_read() -> None:
    assert revise_module._said({"summary": "old shape"}) == "old shape"
    assert revise_module._said({"text": "new shape"}) == "new shape"
    assert revise_module._said({}) == "Done."


def test_the_studio_agent_runs_on_the_revise_phase(cfg, shipped, monkeypatch) -> None:
    """The studio is its own phase: a question about a posting need not run on
    the model that rewrites LaTeX."""
    from jobhunt.web import agent as agent_module

    cfg.raw["models"] = {"revise": {"model": "claude-sonnet-5"}}
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout=None):
        seen.update(phase=phase, tools=tools, flags=agent_module.flags(config, phase))
        return '{"kind": "answer", "text": "it does not", "changes": []}'

    monkeypatch.setattr(agent_module, "run", fake_run)
    board = revise_module.ReviseDesk(cfg, latex=FakeLatex())
    board.open(7, folder=str(shipped))

    board.send(7, "does this posting need German?")
    finish(board)

    assert seen["phase"] == "revise"
    assert seen["flags"] == ["--model", "claude-sonnet-5"]
    assert "Write" in seen["tools"] and "Edit" in seen["tools"]


# --- waiting for the CV to exist ------------------------------------------------


def test_a_folder_without_a_cv_is_not_ready(cfg, tmp_path) -> None:
    """The batch records the folder as its first step, long before the agent
    writes anything into it."""
    folder = tmp_path / "Arc Bank - Backend Engineer"
    folder.mkdir()

    assert revise_module.has_cv(str(folder)) is False

    (folder / "cv.tex").write_text(TEX, encoding="utf-8")
    assert revise_module.has_cv(str(folder)) is True


def test_no_folder_at_all_is_not_ready() -> None:
    assert revise_module.has_cv(None) is False
    assert revise_module.has_cv("") is False


def test_a_half_written_cv_is_not_ready(cfg, tmp_path) -> None:
    """The agent writes cv.tex gradually. Copying it mid-write produced a draft
    that could never build: "no legal \\end found", on every attempt, forever."""
    folder = tmp_path / "Alpaca - Software Engineer @ Remote"
    folder.mkdir()
    half = "\\documentclass{article}\n\\usepackage{xcolor}\n\\color{text-grey}\n"
    (folder / "cv.tex").write_text(half, encoding="utf-8")

    assert revise_module.has_cv(str(folder)) is False

    (folder / "cv.tex").write_text(half + "\\begin{document}x\\end{document}\n", encoding="utf-8")
    assert revise_module.has_cv(str(folder)) is True


def test_reopening_replaces_a_draft_that_can_never_build(cfg, shipped) -> None:
    """Once a truncated CV is copied in, the session caches it and every build
    fails. Reopening has to notice and take the finished CV instead."""
    board, session = opened(cfg, shipped)
    draft = pathlib.Path(session.draft)
    draft.joinpath("cv.tex").write_text("\\documentclass{article}\n", encoding="utf-8")

    again = board.open(7, folder=str(shipped))

    assert again is not session, "the unusable session was thrown away"
    assert (pathlib.Path(again.draft) / "cv.tex").read_text() == TEX
    assert len(again.turns) == 1


def test_a_healthy_session_is_still_resumed(cfg, shipped) -> None:
    board, session = opened(cfg, shipped)
    board.send(7, "make it shorter")
    finish(board)

    assert board.open(7, folder=str(shipped)) is session


def test_the_log_shown_starts_at_the_error_not_the_memory_dump() -> None:
    """pdflatex signs off with a page of statistics that say nothing about what
    went wrong. Leading with those buries the one line that matters."""
    log = "\n".join(
        ["This is pdfTeX", "(loading fonts)", "! Undefined control sequence.",
         "l.42 \\badmacro", "Here is how much of TeX's memory you used:",
         *[f" {n} words out of many" for n in range(10)]]
    )

    shown = revise_module._tail(log)

    assert shown.startswith("! Undefined control sequence.")
    assert "l.42" in shown


# --- which master ---------------------------------------------------------------


def test_a_cv_cut_in_a_template_is_revised_against_that_master(cfg, shipped) -> None:
    (shipped / "master.tex").write_text(TEX, encoding="utf-8")
    agent = FakeAgent()
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "tighten the summary")
    finish(board)

    assert agent.masters == [str(shipped / "master.tex")]


def test_a_cv_from_before_templates_is_revised_against_the_global_master(cfg, shipped) -> None:
    cfg.raw.setdefault("tailoring", {})["master_tex"] = "/somewhere/CV_Source/master.tex"
    agent = FakeAgent()
    board, _ = opened(cfg, shipped, agent=agent)

    board.send(7, "tighten the summary")
    finish(board)

    assert agent.masters == ["/somewhere/CV_Source/master.tex"]


def test_the_studio_builds_drafts_in_the_sandbox(cfg, shipped) -> None:
    calls = []

    def run(argv, cwd):
        calls.append(argv)
        (cwd / "cv.pdf").write_bytes(b"%PDF-1.7 fake")
        return 0, "Output written on cv.pdf (1 page, 13 bytes)."

    ok, log, pdf = revise_module.TailoredLatex(cfg, runner=run).build(shipped / "cv.tex")

    assert ok and pdf.startswith(b"%PDF") and "1 page" in log
    assert calls and all(argv[0].endswith("sandbox-exec") for argv in calls)
