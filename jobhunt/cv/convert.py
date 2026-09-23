"""Turning an uploaded .tex into a template.

Two kinds of file arrive. One is already a template (it uses \\VAR and \\BLOCK)
and is only checked. The other is a finished CV someone wrote for themselves,
and an agent rewrites its body into a template.

The preamble of a converted file is kept byte for byte and is never the agent's
to change. The look is the upload's own, and nothing the agent adds can reach
the part of the document that defines commands and loads packages. Every
candidate goes through validation, and the agent sees what was wrong and tries
again, three rounds at most.
"""
from __future__ import annotations

import pathlib
import re
from collections.abc import Callable
from typing import Any

from jobhunt.config import Config
from jobhunt.cv import ats, desk, latex, preview, templates, validate
from jobhunt.web import agent as agent_module

UPLOAD_LIMIT = 200_000
ROUNDS = 3
AGENT_TIMEOUT = 900.0

# The prompt in, the answer text out.
Agent = Callable[[str], str]
Check = Callable[[str], validate.Findings]

# A body starts at a \\begin{document} on a line of its own and ends at the first
# \\end{document} after it. Prose that mentions both ("the body, from
# \\begin{document} to \\end{document}") must not be read as the document.
_BEGIN = re.compile(r"^[ \t]*\\begin\{document\}", re.MULTILINE)
_END = "\\end{document}"


class UploadRefused(ValueError):
    """A file that cannot become a template, phrased for the person."""


class UploadFailed(RuntimeError):
    """An upload that cannot go on right now, phrased for the person."""


def read_upload(filename: str, data: bytes) -> str:
    if not filename.lower().endswith(".tex"):
        raise UploadRefused("only .tex files can become templates")
    if data.lstrip().startswith(b"%PDF"):
        raise UploadRefused("that is a PDF. templates are made from .tex files")
    if len(data) > UPLOAD_LIMIT:
        raise UploadRefused("that file is over 200 KB, which no CV template needs")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadRefused("the file is not UTF-8 text") from exc
    if "\\documentclass" not in text or "\\begin{document}" not in text:
        raise UploadRefused(
            "that is not a complete LaTeX document: it needs \\documentclass and \\begin{document}"
        )
    return text.replace("\r\n", "\n")


def is_template(source: str) -> bool:
    return "\\VAR{" in source or "\\BLOCK{" in source


PROMPT = """\
You are turning a finished CV, written in LaTeX, into a template for a CV builder.
The builder fills the template with a person's profile. Keep the look of the
original exactly: the same commands, layout, spacing and section style. Replace
the person's content with placeholders, and print every section through the
`sections` loop so the person's own order and titles are kept.

Answer with the document body only, from \\begin{document} to \\end{document}.
The preamble stays exactly as it is and is not yours to change. Do not load
packages, define commands, or use \\directlua, \\input, \\write or \\openin.

The template language:

<<CONTRACT>>

A complete example body, written for a different design:

<<EXAMPLE>>

The CV to convert:

<<SOURCE>>
"""


def convert(source: str, agent: Agent, check: Check) -> tuple[str, validate.Findings, int]:
    """The best candidate, its findings, and how many rounds it took."""
    preamble, _ = latex.split_preamble(source)
    feedback: list[str] = []
    candidate = ""
    findings = validate.Findings(["the agent never produced a template"], [])
    for round_ in range(1, ROUNDS + 1):
        body = _body(agent(_prompt(source, feedback)))
        if body is None:
            feedback = ["answer with the body only, from \\begin{document} to \\end{document}"]
            continue
        candidate = preamble + body
        findings = check(candidate)
        if findings.ok:
            return candidate, findings, round_
        feedback = findings.problems
    return candidate, findings, ROUNDS


def _prompt(source: str, feedback: list[str]) -> str:
    example = latex.split_preamble(templates.get_builtin("classic").text())[1]
    prompt = (
        PROMPT.replace("<<CONTRACT>>", templates.CONTRACT_PATH.read_text(encoding="utf-8"))
        .replace("<<EXAMPLE>>", example)
        .replace("<<SOURCE>>", source)
    )
    if feedback:
        prompt += "\nYour previous answer was rejected. Fix each of these:\n- " + "\n- ".join(feedback) + "\n"
    return prompt


def _body(answer: str) -> str | None:
    starts = list(_BEGIN.finditer(answer))
    if not starts:
        return None
    start = starts[-1].start()
    end = answer.find(_END, start)
    if end < 0:
        return None
    return answer[start : end + len(_END)].lstrip(" \t") + "\n"


class UploadDesk(desk.Desk):
    """One upload at a time, checked or converted off the request thread."""

    Failure = UploadFailed
    running_message = "the template is still being checked"

    def __init__(
        self,
        config: Config,
        *,
        agent: Agent | None = None,
        runner: latex.Runner | None = None,
        ats_runner: ats.Runner | None = None,
        background: bool = True,
    ) -> None:
        self.config = config
        # No tools: the whole file is in the prompt.
        self.agent = agent or (
            lambda prompt: agent_module.run(config, "template", prompt, tools="", timeout=AGENT_TIMEOUT)
        )
        self.runner = runner
        self.ats_runner = ats_runner
        super().__init__(background=background)

    def _clear(self) -> None:
        self.filename = ""
        self.mode = ""  # template | convert
        self.source = ""
        self.candidate = ""
        self.findings: validate.Findings | None = None
        self.rounds = 0

    @property
    def pdf(self) -> bytes:
        findings = self.findings
        return findings.pdf if findings else b""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            findings = self.findings
            report = findings.ats if findings else None
            return {
                "state": self.state,
                "error": self.error,
                "filename": self.filename,
                "suggested_name": pathlib.Path(self.filename).stem[:templates.NAME_LIMIT],
                "mode": self.mode,
                "rounds": self.rounds,
                "acceptable": bool(findings and findings.ok and self.state == "done"),
                "problems": findings.problems if findings else [],
                "warnings": findings.warnings if findings else [],
                "ats_ran": bool(report and report.ran),
                "ats_note": report.note if report else "",
                "has_preview": bool(findings and findings.pdf),
                "engine": validate.engine_of(self.candidate) if self.candidate else "",
            }

    def start(self, filename: str, data: bytes) -> dict[str, Any]:
        with self._lock:
            if self.state == "running":
                raise UploadFailed("a template is already being checked")
            text = read_upload(filename, data)
            self._reset()
            self.state, self.filename, self.source = "running", filename, text
            self.mode = "template" if is_template(text) else "convert"
        self._launch(lambda: self._work(text), "jobhunt-cv-upload")
        return self.snapshot()

    def _work(self, text: str) -> None:
        profile = preview.profile_for(self.config)

        def check(candidate: str) -> validate.Findings:
            return validate.validate(
                self.config, candidate, profile=profile, runner=self.runner, ats_runner=self.ats_runner
            )

        if self.mode == "template":
            candidate, findings, rounds = text, check(text), 0
        else:
            candidate, findings, rounds = convert(text, self.agent, check)
        with self._lock:
            self.candidate, self.findings, self.rounds = candidate, findings, rounds
            self.state = "done"

    def accept(self, name: str) -> templates.Template:
        with self._lock:
            findings = self.findings
            if self.state != "done" or not findings or not findings.ok:
                raise UploadFailed("there is no checked template to accept")
            added = templates.add(
                self.config,
                name,
                self.candidate,
                engine=validate.engine_of(self.candidate),
                original=self.source if self.mode == "convert" else None,
            )
            self._reset()
            return added
