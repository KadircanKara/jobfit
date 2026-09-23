"""Turning today's master.tex into a profile, once.

A model reads LaTeX better than a parser written for one resume would, but it
is still a model rewriting the document every tailored CV is checked against.
So nothing it produces is trusted on its word: the profile is rendered back
through Classic and compared with the master word by word, visible text and
comments separately, and the person accepts that comparison rather than the
model's output. The master itself is never modified here.
"""
from __future__ import annotations

import collections
import dataclasses
import json
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any

from jobhunt.config import Config
from jobhunt.cv import latex, model, render, templates
from jobhunt.cv import store as cvstore
from jobhunt.web import agent as agent_module

AGENT_TIMEOUT = 900.0

# The prompt in, the answer text out.
Agent = Callable[[str], str]


class ImportFailed(RuntimeError):
    """Why the import did not produce a profile, phrased for the person."""


@dataclasses.dataclass(frozen=True)
class Report:
    preamble_identical: bool
    missing: list[str]
    added: list[str]
    comments_missing: list[str]
    comments_added: list[str]

    @property
    def faithful(self) -> bool:
        """Every visible word came through, and nothing was invented. Comments
        are reported but do not count: decorative rules are expected to differ."""
        return self.preamble_identical and not self.missing and not self.added

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"faithful": self.faithful}


PROMPT = """\
You are converting a LaTeX resume into a JSON profile. This is a one-time
migration of the only document every tailored CV is checked against, so the
profile must say exactly what the LaTeX says: never invent, never drop, never
reword.

Return one JSON object and nothing else. It must validate against this JSON
Schema:

<<SCHEMA>>

Mapping rules:
- The header goes to basics. Labelled lines such as "MILITARY STATUS : Completed"
  go to extras. PROFESSIONAL SUMMARY goes to summary.text. EXPERIENCE, EDUCATION
  and PROJECTS are entries. SKILLS is one skills group per category. LANGUAGES is
  languages. A section that fits none of these becomes a custom section.
- An entry's title is the organisation, school or project name; subtitle is the
  role or degree; start and end are the date range split at the dash, spelled
  exactly as written ("April 2026", "Present").
- A GPA written into a degree line goes in gpa and leaves the subtitle:
  "Master of Science in X (GPA: 3.67/4.00)" is subtitle "Master of Science in X"
  and gpa "3.67/4.00".
- Header: phone, email and location have their own fields. Every other link is
  {label, url}, in the order it appears.
- Inline formatting: \\textbf{x} is **x**, \\textit{x} or \\emph{x} is *x*,
  \\href{url}{text} is [text](url). Unescape LaTeX: \\& is &, \\% is %, \\$ is $,
  \\_ is _, $\\times$ is ×. Drop spacing and layout commands.
- Comments matter. A commented-out entry or bullet (a line starting with % that
  is LaTeX for an entry or an item) is kept, with "hidden": true. Commented
  prose above an entry or bullet ("% NOTE: ...", "% ACTION REQUIRED: ...") goes
  into that item's notes, without the leading %. Commented alternative
  headlines go to basics.headline_variants and commented alternative summaries
  to summary.variants, each with the label written above it. Decorative rules
  such as "%-----------EXPERIENCE-----------" are dropped.
- Keep every entry, bullet and skill in the order the resume has them. layout
  lists the sections in the order they appear, with each title exactly as
  printed.
- Leave out every "id" field except on custom sections: give those ids
  custom-1, custom-2, ... and refer to them in layout as custom:custom-1.

The resume:

<<MASTER>>
"""


def extract(master_text: str, agent: Agent) -> model.Profile:
    prompt = PROMPT.replace("<<SCHEMA>>", json.dumps(model.Profile.model_json_schema())).replace(
        "<<MASTER>>", master_text
    )
    data = _json_object(agent(prompt))
    try:
        return model.parse(_with_ids(data))
    except model.ProfileInvalid as exc:
        raise ImportFailed(f"the profile the agent wrote was refused at {exc.field}: {exc.message}") from exc


def _json_object(raw: str) -> Any:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ImportFailed("the agent did not return a profile")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ImportFailed("the agent did not return a profile it could be read as") from exc


def _with_ids(data: Any) -> Any:
    """Fill every id the agent was told to leave out. Ids only have to be unique
    and stable from here on, and asking a model to invent them invites repeats."""
    if not isinstance(data, dict):
        return data

    def fill(items: Any) -> None:
        for item in items or []:
            if isinstance(item, dict):
                if not item.get("id"):
                    item["id"] = uuid.uuid4().hex[:12]
                fill(item.get("bullets"))
                fill(item.get("entries"))

    fill((data.get("basics") or {}).get("headline_variants"))
    fill((data.get("summary") or {}).get("variants"))
    for key in ("extras", "experience", "education", "projects", "skills", "languages", "custom_sections"):
        fill(data.get(key))
    return data


_COMMENT = re.compile(r"(?<!\\)%.*$")
_COMMAND = re.compile(r"\\[a-zA-Z]+\*?")
_NOISE = re.compile(r"[{}$~^&%\\\[\]()|:,;]")
_LENGTH = re.compile(r"^-?[\d.]+(pt|em|ex|in|cm|mm)$")


def compare(old: str, new: str) -> Report:
    old_preamble, old_body = latex.split_preamble(old)
    new_preamble, new_body = latex.split_preamble(new)
    old_text, old_notes = _split_comments(old_body)
    new_text, new_notes = _split_comments(new_body)
    missing, added = _difference(_words(old_text), _words(new_text))
    notes_missing, notes_added = _difference(_words(old_notes), _words(new_notes))
    return Report(old_preamble == new_preamble, missing, added, notes_missing, notes_added)


def _split_comments(body: str) -> tuple[str, str]:
    visible, comments = [], []
    for line in body.splitlines():
        match = _COMMENT.search(line)
        if match:
            comments.append(match.group()[1:])
            line = line[: match.start()]
        visible.append(line)
    return "\n".join(visible), "\n".join(comments)


def _words(text: str) -> collections.Counter[str]:
    text = _NOISE.sub(" ", _COMMAND.sub(" ", text))
    words = (word.strip(".'`\"-").lower() for word in text.split())
    return collections.Counter(word for word in words if word and not _LENGTH.match(word))


def _difference(old: collections.Counter[str], new: collections.Counter[str]) -> tuple[list[str], list[str]]:
    return sorted((old - new).elements()), sorted((new - old).elements())


class ImportDesk:
    """One import at a time, off the request thread: the agent takes minutes,
    so the page polls instead of holding a request open."""

    def __init__(
        self,
        config: Config,
        *,
        agent: Agent | None = None,
        runner: latex.Runner | None = None,
        background: bool = True,
    ) -> None:
        self.config = config
        self.agent = agent or (
            lambda prompt: agent_module.run(config, "import", prompt, tools="Read", timeout=AGENT_TIMEOUT)
        )
        self.runner = runner
        self.background = background
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"  # idle | running | done | failed
        self.error: str | None = None
        self.profile: model.Profile | None = None
        self.report: Report | None = None
        self.pdf = b""
        self.log = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "error": self.error,
            "report": self.report.as_dict() if self.report else None,
            "profile": self.profile.model_dump(mode="json") if self.profile else None,
            "has_preview": bool(self.pdf),
            "log": self.log,
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self.state == "running":
                raise ImportFailed("an import is already running")
            if cvstore.profile_path(self.config).exists():
                raise ImportFailed("a profile already exists. restore a backup rather than importing again")
            master = cvstore.master_path(self.config)
            if not master.exists():
                raise ImportFailed(f"there is no master.tex at {master} to import")
            self._reset()
            self.state = "running"
        if self.background:
            threading.Thread(target=self.run, daemon=True, name="jobhunt-cv-import").start()
        else:
            self.run()
        return self.snapshot()

    def run(self) -> None:
        try:
            old = cvstore.master_path(self.config).read_text(encoding="utf-8")
            profile = extract(old, self.agent)
            classic = templates.get(self.config, templates.DEFAULT_ID)
            new = render.render(profile, classic.text())
            report = compare(old, new)
            built = latex.build(new, engine=classic.engine, runner=self.runner)
        except Exception as exc:  # a background thread has nobody else to tell
            self.state, self.error = "failed", str(exc)
            return
        self.profile, self.report = profile, report
        self.pdf, self.log = built.pdf, "" if built.ok else built.log
        self.state = "done"

    def accept(self) -> cvstore.Backup | None:
        if self.state != "done" or self.profile is None:
            raise ImportFailed("there is no finished import to accept")
        backup = cvstore.write(self.config, self.profile)
        self._reset()
        return backup

    def discard(self) -> None:
        if self.state == "running":
            raise ImportFailed("the import is still running")
        self._reset()
