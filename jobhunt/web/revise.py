"""Revising a tailored CV by talking to the agent that cut it.

The batch ships a CV without asking anything. This is the optional step after
it: one conversation per job, where the agent edits `cv.tex` and nothing else
does. There is no text editor here on purpose — every change is asked for, so
every change has a reason in the thread above it.

Three rules shape the whole module.

A revision never touches the shipped folder. Edits land in a draft copy, and
`sync` is the only thing that writes back, so until it is pressed the folder is
exactly what the batch produced.

A draft that does not build costs you the preview, not the CV. The same rule the
profile editor already follows: a failed compile rolls `cv.tex` back to the last
version that built, and the preview keeps showing it.

The agent refuses to invent. The reviewer already rejects fabrication after the
fact; saying so at the moment it is asked for is cheaper and clearer.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import pathlib
import re
import shutil
import threading
from typing import Any, Protocol

from jobhunt.config import Config
from jobhunt.web import agent as agent_module
from jobhunt.web.tailor import TailorError

# Two pages is what the batch cuts to. Past that in the studio it is the user's
# document, so this only ever warns.
MAX_PAGES = 2

# The compiled artefacts a sync replaces in the shipped folder. The named PDF is
# what the tailoring skill hands to an employer, so a sync that refreshed cv.pdf
# and left it stale would be the worst possible half-write.
TEX_NAME = "cv.tex"
PDF_NAME = "cv.pdf"

_PAGES_IN_LOG = re.compile(r"Output written on .*?\((\d+) pages?", re.S)
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page[^s]")


class ReviseError(RuntimeError):
    """Something the person revising needs to read, not a stack trace."""


# A LaTeX document is only usable once it is closed. Anything short of this is
# a file the agent is still writing, and compiling it dies on "no legal \end
# found" rather than on anything the person did.
_CLOSED = "\\end{document}"


def has_cv(folder: str | None) -> bool:
    """Whether this folder holds a *finished* CV.

    The batch records a job's folder as its first step, well before the agent
    has written anything into it, and the agent writes the file gradually. Both
    stages have to be excluded: existence alone was enough to copy a truncated
    preamble into a draft that could never build again.
    """
    if not folder:
        return False
    return is_complete(pathlib.Path(folder).expanduser() / TEX_NAME)


def is_complete(tex: pathlib.Path) -> bool:
    try:
        return _CLOSED in tex.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


@dataclasses.dataclass
class Turn:
    """One exchange. `changes` is what the agent did to cv.tex, never the tex."""

    role: str  # you | agent
    text: str
    # What the agent decided this message was. Not every message is an edit:
    # "does this posting want German?" deserves an answer, not a rewrite.
    kind: str = "edit"  # answer | edit | refusal
    changes: list[str] = dataclasses.field(default_factory=list)
    build: str | None = None  # ok | failed
    pages: int | None = None
    version: int | None = None
    log: str | None = None
    refused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Session:
    job_id: int
    folder: str
    draft: str
    title: str = ""
    company: str = ""
    fit: float | None = None
    turns: list[Turn] = dataclasses.field(default_factory=list)
    version: int = 1
    synced_version: int = 1
    pages: int | None = None
    thinking: bool = False
    error: str | None = None

    @property
    def ahead(self) -> int:
        """Revisions the draft is ahead of the shipped folder."""
        return max(0, self.version - self.synced_version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "folder": self.folder,
            "title": self.title,
            "company": self.company,
            "fit": self.fit,
            "turns": [turn.as_dict() for turn in self.turns],
            "version": self.version,
            "synced_version": self.synced_version,
            "ahead": self.ahead,
            "pages": self.pages,
            "over_length": bool(self.pages and self.pages > MAX_PAGES),
            "thinking": self.thinking,
            "error": self.error,
        }


class Agent(Protocol):
    def revise(self, *, draft: str, message: str, master: str) -> dict[str, Any]: ...


class Latex(Protocol):
    def build(self, tex: pathlib.Path) -> tuple[bool, str, bytes]: ...


# --- the studio -------------------------------------------------------------


class ReviseDesk:
    """Every open revision session, one per job.

    Sessions live as long as the process, the same way a run does, so closing
    the tab does not lose a conversation.
    """

    def __init__(
        self,
        config: Config,
        *,
        agent: Agent | None = None,
        latex: Latex | None = None,
    ) -> None:
        self.config = config
        self.agent = agent or ClaudeAgent(config)
        self.latex = latex or PdfLatex()
        self.master = str(config.get("tailoring", "master_tex") or "")
        self._root = pathlib.Path(str(config.data_dir)) / "revise"
        self._sessions: dict[int, Session] = {}
        self._lock = threading.Lock()

    # --- opening ---------------------------------------------------------

    def open(
        self,
        job_id: int,
        *,
        folder: str,
        title: str = "",
        company: str = "",
        fit: float | None = None,
    ) -> Session:
        """The session for this job, resuming the conversation if there is one."""
        source = pathlib.Path(folder).expanduser()
        with self._lock:
            existing = self._sessions.get(job_id)
            if existing is not None and is_complete(pathlib.Path(existing.draft) / TEX_NAME):
                return existing
            if existing is not None:
                # The draft was copied from a CV that was still being written.
                # Nothing in it is worth keeping, and keeping it means every
                # build fails from here on.
                self._sessions.pop(job_id, None)

        if not (source / TEX_NAME).exists():
            raise ReviseError(f"no {TEX_NAME} in {folder} — this job has nothing to revise yet")

        draft = self._draft_dir(job_id)
        _copy_tree(source, draft)
        session = Session(
            job_id=job_id,
            folder=str(source),
            draft=str(draft),
            title=title,
            company=company,
            fit=fit,
        )
        _keep_version(draft, 1)
        session.pages = self._pages_of(draft)
        session.turns.append(
            Turn(
                role="agent",
                kind="answer",
                text=(
                    f"This CV is cut against the {company or 'job'} posting. Ask me about the "
                    f"posting or the CV, or tell me what to change and I will edit "
                    f"{TEX_NAME} and rebuild."
                ),
                build="ok",
                pages=session.pages,
                version=1,
            )
        )
        with self._lock:
            self._sessions[job_id] = session
        return session

    def get(self, job_id: int) -> Session | None:
        with self._lock:
            return self._sessions.get(job_id)

    def sessions(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    # --- the conversation ------------------------------------------------

    def send(self, job_id: int, message: str) -> Session:
        """Queue one revision. Runs in the background: an edit plus a LaTeX
        build is far longer than a request should be held open for."""
        session = self._require(job_id)
        if session.thinking:
            raise ReviseError("the agent is still working on your last message")
        text = message.strip()
        if not text:
            raise ReviseError("say what you would like changed")

        session.turns.append(Turn(role="you", text=text))
        session.thinking = True
        session.error = None
        thread = threading.Thread(
            target=self._work, args=(session, text), daemon=True, name=f"jobhunt-revise-{job_id}"
        )
        thread.start()
        return session

    def _work(self, session: Session, message: str) -> None:
        try:
            self._revise(session, message)
        except Exception as exc:  # the thread is the end of the line
            session.error = str(exc)
            session.turns.append(
                Turn(role="agent", text=f"That did not go through: {exc}", build=None)
            )
        finally:
            session.thinking = False

    def _revise(self, session: Session, message: str) -> None:
        draft = pathlib.Path(session.draft)
        tex = draft / TEX_NAME
        before = tex.read_text(encoding="utf-8")

        answer = self.agent.revise(draft=session.draft, message=message, master=self.master)
        text = _said(answer)
        changes = [str(item) for item in (answer.get("changes") or [])]
        kind = _kind_of(answer, changes)

        if kind in ("answer", "refusal"):
            # A question and a refusal have the same shape here: the CV did not
            # change, so there is nothing to build and no version to cut. Saying
            # "still v3" is more honest than inventing a new one.
            if tex.read_text(encoding="utf-8") != before:
                # The agent said it was only talking, and edited anyway. Its own
                # account of the turn is the one thing that cannot be checked,
                # so the file is put back and the discrepancy is stated.
                tex.write_text(before, encoding="utf-8")
                text += (
                    "\n\n(That touched the CV even though it was not an edit, "
                    "so I put the file back.)"
                )
            session.turns.append(
                Turn(
                    role="agent",
                    kind=kind,
                    text=text,
                    refused=kind == "refusal",
                    version=session.version,
                )
            )
            return

        ok, log, pdf = self.latex.build(draft / TEX_NAME)
        if not ok:
            # Roll back to the last version that built. The preview never blanks.
            (draft / TEX_NAME).write_text(before, encoding="utf-8")
            session.turns.append(
                Turn(
                    role="agent",
                    text=(
                        f"{text}\n\nThat edit broke the build, so I reverted it. "
                        f"The preview is still v{session.version}."
                    ),
                    changes=changes,
                    build="failed",
                    version=session.version,
                    log=_tail(log),
                )
            )
            return

        session.version += 1
        _keep_version(draft, session.version)
        session.pages = _page_count(log, pdf)
        session.turns.append(
            Turn(
                role="agent",
                text=text,
                changes=changes,
                build="ok",
                pages=session.pages,
                version=session.version,
            )
        )

    # --- writing back ----------------------------------------------------

    def sync(self, job_id: int) -> Session:
        """Promote the draft into the shipped folder. The only writer there."""
        session = self._require(job_id)
        if session.thinking:
            raise ReviseError("wait for the current revision to finish")
        if not session.ahead:
            raise ReviseError("nothing to sync — the folder already matches")

        draft = pathlib.Path(session.draft)
        folder = pathlib.Path(session.folder)
        ok, log, pdf = self.latex.build(draft / TEX_NAME)
        if not ok:
            raise ReviseError("the draft does not build, so it was not written to the folder")

        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(draft / TEX_NAME, folder / TEX_NAME)
        (folder / PDF_NAME).write_bytes(pdf)
        # The skill also leaves a PDF named for the person, and that is the file
        # that actually gets sent. Leaving it stale would be worse than the CV
        # never having been revised at all.
        for named in folder.glob("*-CV.pdf"):
            named.write_bytes(pdf)

        session.synced_version = session.version
        session.pages = _page_count(log, pdf)
        return session

    def discard(self, job_id: int) -> Session:
        """Throw the draft away and start again from the shipped folder."""
        session = self._require(job_id)
        if session.thinking:
            raise ReviseError("wait for the current revision to finish")
        with self._lock:
            self._sessions.pop(job_id, None)
        shutil.rmtree(self._draft_dir(job_id), ignore_errors=True)
        return self.open(
            job_id,
            folder=session.folder,
            title=session.title,
            company=session.company,
            fit=session.fit,
        )

    # --- the preview -----------------------------------------------------

    def preview(self, job_id: int) -> dict[str, Any]:
        """The current draft as a PDF the browser can show."""
        session = self._require(job_id)
        ok, log, pdf = self.latex.build(pathlib.Path(session.draft) / TEX_NAME)
        if not ok:
            return {"ok": False, "log": _tail(log), "pdf": None, "pages": session.pages}
        session.pages = _page_count(log, pdf)
        return {
            "ok": True,
            "log": "",
            "pdf": base64.b64encode(pdf).decode(),
            "pages": session.pages,
        }

    # --- plumbing --------------------------------------------------------

    def _require(self, job_id: int) -> Session:
        session = self.get(job_id)
        if session is None:
            raise ReviseError(f"no revision open for job {job_id}")
        return session

    def _draft_dir(self, job_id: int) -> pathlib.Path:
        return self._root / str(job_id)

    def _pages_of(self, draft: pathlib.Path) -> int | None:
        ok, log, pdf = self.latex.build(draft / TEX_NAME)
        return _page_count(log, pdf) if ok else None


# --- helpers ----------------------------------------------------------------


def _copy_tree(source: pathlib.Path, target: pathlib.Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target)


def _keep_version(draft: pathlib.Path, version: int) -> None:
    """Snapshot the tex so a broken build has something to fall back to."""
    versions = draft / ".versions"
    versions.mkdir(exist_ok=True)
    shutil.copy2(draft / TEX_NAME, versions / f"v{version}.tex")


KINDS = ("answer", "edit", "refusal")


def _said(answer: dict[str, Any]) -> str:
    """What the agent wants shown. `summary` is accepted as the older name."""
    for key in ("text", "summary", "answer"):
        said = str(answer.get(key) or "").strip()
        if said:
            return said
    return "Done."


def _kind_of(answer: dict[str, Any], changes: list[str]) -> str:
    """Which of the three this turn was.

    The agent states it. When it does not, or states something unrecognised, the
    turn is classified by what it actually produced: edits mean an edit, and
    nothing produced means nothing was changed, whatever the prose claims.
    """
    stated = str(answer.get("kind") or "").strip().lower()
    if stated in KINDS:
        # An "edit" that edited nothing is not an edit. Trust the artefact.
        if stated == "edit" and not changes:
            return "answer"
        return stated
    if answer.get("refused"):
        return "refusal"
    return "edit" if changes else "answer"


def _page_count(log: str, pdf: bytes) -> int | None:
    """Pages in the built PDF, from the log if it says, else from the file."""
    found = _PAGES_IN_LOG.search(log or "")
    if found:
        return int(found.group(1))
    pages = len(_PAGE_OBJECT.findall(pdf or b""))
    return pages or None


def _tail(log: str, lines: int = 12) -> str:
    """The part of a LaTeX log worth reading.

    That is the first line starting with "!" and what follows it, not the end of
    the file: pdflatex signs off with a page of memory statistics that say
    nothing about what went wrong.
    """
    kept = [line for line in (log or "").splitlines() if line.strip()]
    for index, line in enumerate(kept):
        if line.startswith("!"):
            return "\n".join(kept[index : index + lines])
    return "\n".join(kept[-lines:])


# --- the real agent and the real toolchain ----------------------------------


PROMPT = """\
You are working on one tailored CV with the person it belongs to. The folder is:

{draft}

It holds {tex} (the CV), and usually jd.txt (the posting it was cut against).
The master CV is at {master}, and it is the only source of fact about this
person.

## Decide what this message is

Not every message is an edit. Read what they said and pick one:

- **answer** — they asked a question. About the posting, the company, the CV, or
  what you changed earlier. Answer it from the files. Change nothing: do not
  write, do not edit, do not reformat. "Does this role want German?" and "why
  did you drop the intern job?" are answers, not edits.
- **edit** — they asked for the CV to change. Make the change in {tex}.
- **refusal** — the change would put a claim on the CV that the master does not
  support. Do not make it up. Say which claim is unsupported and suggest adding
  it to the master first. A caught fabrication is worth more than a satisfied
  instruction.

If a message both asks and instructs, treat it as an edit and answer inside the
same reply.

Do not create files, do not touch any other folder, and do not run the LaTeX
compiler — that is done for you. Keep the document to {max_pages} pages unless
they explicitly ask for more room.

## What they said

{message}

## Answer with

Only a JSON object, no prose around it:

{{"kind": "answer" | "edit" | "refusal",
  "text": "what you say to them, written to them",
  "changes": ["section > what changed", "..."]}}

`changes` names where in the CV each edit landed, one short line each. It is
empty for an answer and for a refusal, because neither one changed the CV.
"""


class ClaudeAgent:
    """The real agent: the tailoring skill's own toolset, pointed at a draft."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config

    def revise(self, *, draft: str, message: str, master: str) -> dict[str, Any]:
        prompt = PROMPT.format(
            draft=draft, tex=TEX_NAME, master=master, max_pages=MAX_PAGES, message=message
        )
        raw = agent_module.run(
            self.config, "revise", prompt, tools="Read Write Edit Glob Grep"
        )
        return _json_object(raw)


def _json_object(raw: str) -> dict[str, Any]:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ReviseError("the agent did not say what it changed")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReviseError("the agent's answer was not readable") from exc


class PdfLatex:
    """Builds in place, because a CV folder carries its own assets."""

    def build(self, tex: pathlib.Path) -> tuple[bool, str, bytes]:
        import subprocess

        binary = shutil.which("pdflatex") or shutil.which("xelatex")
        if binary is None:
            raise TailorError("no LaTeX toolchain found. install MacTeX or TeX Live.")
        done = subprocess.run(
            [binary, "-interaction=nonstopmode", "-halt-on-error",
             f"-output-directory={tex.parent}", str(tex)],
            capture_output=True, text=True, timeout=180.0, check=False,
            cwd=str(tex.parent),
        )
        pdf = tex.with_suffix(".pdf")
        log = done.stdout + done.stderr
        if done.returncode != 0 or not pdf.exists():
            return False, log, b""
        return True, log, pdf.read_bytes()
