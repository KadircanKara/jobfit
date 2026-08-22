"""A compact candidate summary, derived from master.tex.

The LLM gate needs to know who it is scoring for. The source of truth is the
same master.tex the tailoring skill uses, so there is one place to update when
the candidate's experience changes.

This is a summary, not the CV. The gate reads it once per batch and it competes
for context with the jobs themselves, so it is deliberately capped.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

from jobhunt.config import Config
from jobhunt.rank import regions

MAX_CHARS = 2500

# Where an explicitly stated location lives. Set it and nothing is inferred.
# This is the seam: if the Profile screen ever becomes structured fields rather
# than a LaTeX document, it writes here and every reader below is unaffected.
LOCATION_KEY = ("ranking", "candidate_location")

# The name to show for a country code. COUNTRY_NAMES maps many names onto one
# code, so the first spelling of each is taken as the one to print.
_DISPLAY_NAMES: dict[str, str] = {}
for _name, _code in regions.COUNTRY_NAMES.items():
    _DISPLAY_NAMES.setdefault(_code, _name.title())

# A line long enough to be prose is a sentence that mentions a country, not an
# address. Only short lines are quoted back verbatim.
_LOCATION_LINE_CHARS = 60

# Facts about the candidate that master.tex does not carry, because they are
# preferences and constraints rather than CV content. Appended to every gate
# prompt, in every market, so a single edit here reaches all three.
CONSTRAINTS_KEY = ("ranking", "constraints")

_COMMENT = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
# \begin{center} must lose "center" too, or the environment names end up in the
# summary as if they were content.
_ENVIRONMENT = re.compile(r"\\(?:begin|end)\s*\{[^}]*\}")
# Leftovers that survive command stripping but say nothing: "5pt", "$|$", "1pt".
# The gate scores a fit. It has no use for contact details, and the emitted
# batch file is written to disk and read by another tool, so there is no reason
# to put a phone number and a home email in it.
_CONTACT = re.compile(
    r"(@[\w.-]+\.\w+|https?://|mailto:|linkedin\.com|github\.com|\+\d[\d\s().-]{7,})",
    re.I,
)
_ARTIFACT = re.compile(r"^(?:[\d.]+\s*(?:pt|em|ex|cm|mm|in|px)|\$[^\w]*\$|[|$&~^_\\/*+-]+)$", re.I)
_COMMAND_WITH_ARG = re.compile(
    r"\\(?:textbf|textit|emph|href|url|texttt|underline|section|subsection|item)\s*"
)
_BRACED = re.compile(r"[{}]")
_COMMAND = re.compile(r"\\[a-zA-Z@]+\s*(\[[^\]]*\])?")
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")

FALLBACK = (
    "Senior AI/backend engineer based in Istanbul (GMT+3). Python, FastAPI, "
    "Postgres, Redis, httpx. Builds custom LLM orchestration rather than using "
    "heavy frameworks. Open to remote roles worldwide and to roles in Turkey."
)


def load(config: Config) -> str:
    """Candidate summary for the gate prompt.

    A missing master.tex is not an error. Ranking has to work on a machine where
    the CV lives somewhere else, so it falls back to a short stated profile and
    says so rather than refusing to score anything.
    """
    override = config.get("ranking", "profile_summary")
    if override:
        return str(override).strip()[:MAX_CHARS]

    path_value = config.get("tailoring", "master_tex")
    summary = FALLBACK
    if path_value:
        path = pathlib.Path(str(path_value)).expanduser()
        if path.exists():
            try:
                summary = summarize(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                summary = FALLBACK
    return _with_constraints(summary, config)


@dataclasses.dataclass(frozen=True)
class Location:
    """Where the candidate is, as the gate needs to read it.

    `text` is for the prompt, `country` is the ISO code the same rules elsewhere
    already speak in. Either may be absent: an unknown location is a reason to
    say nothing about location, never a reason to guess.
    """

    text: str
    country: str | None = None

    @property
    def country_name(self) -> str:
        """The country as a prompt should say it.

        The rules that turn on location are about countries, not cities: "a
        country other than Istanbul, Turkey" is not a sentence a model can apply.
        """
        if self.country and self.country in _DISPLAY_NAMES:
            return _DISPLAY_NAMES[self.country]
        return self.country or self.text


def location(config: Config) -> Location | None:
    """The candidate's own location, stated or inferred.

    Stated wins outright. Otherwise the profile is read for the first line that
    names a country the project already knows, which on a CV is the address
    under the name, and failing that the stated constraints, which is where
    someone writes "Based in Istanbul" when the CV does not say.
    """
    stated = config.get(*LOCATION_KEY)
    if stated:
        text = str(stated).strip()
        codes, _ = regions.resolve([part.strip() for part in text.split(",")])
        return Location(text=text, country=codes[-1] if codes else None)
    return _location_in(load(config))


def _location_in(profile_text: str) -> Location | None:
    for line in profile_text.splitlines():
        line = line.strip().lstrip("- ").strip()
        if not line:
            continue
        code = _country_named_in(line)
        if not code:
            continue
        if len(line) <= _LOCATION_LINE_CHARS:
            return Location(text=line, country=code)
        # A whole sentence would drag its own claims into the prompt, so only
        # the country it named is kept.
        return Location(text=_DISPLAY_NAMES.get(code, code), country=code)
    return None


def _country_named_in(line: str) -> str | None:
    lowered = line.lower()
    for name, code in regions.COUNTRY_NAMES.items():
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            return code
    return None


def _with_constraints(summary: str, config: Config) -> str:
    """Append the stated constraints. A CV says what someone has done; it does
    not say what they will accept, and the gate needs both."""
    lines = config.get(*CONSTRAINTS_KEY) or []
    if not lines:
        return summary
    rendered = "\n".join(f"- {str(line).strip()}" for line in lines if str(line).strip())
    return f"{summary}\n\nStated constraints and preferences:\n{rendered}"


def summarize(latex: str, max_chars: int = MAX_CHARS) -> str:
    """Strip LaTeX to readable prose and truncate on a line boundary."""
    text = _COMMENT.sub("", latex)
    # Drop the preamble: everything a gate cares about is inside the document.
    if "\\begin{document}" in text:
        text = text.split("\\begin{document}", 1)[1]
    text = text.split("\\end{document}", 1)[0]

    text = _ENVIRONMENT.sub("\n", text)
    text = _COMMAND_WITH_ARG.sub(" ", text)
    text = _COMMAND.sub(" ", text)
    text = _BRACED.sub(" ", text)
    text = text.replace("\\&", "&").replace("~", " ").replace("\\\\", "\n")
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)

    lines = [line.strip() for line in text.splitlines()]
    kept: list[str] = []
    total = 0
    for line in lines:
        if not line or _ARTIFACT.match(line):
            continue
        # Strip trailing length units and bare separators left inside a line.
        line = re.sub(r"\s*\$\s*\|\s*\$\s*", " | ", line)
        line = re.sub(r"\b[\d.]+\s*(?:pt|em|ex)\b", "", line).strip()
        if len(line) < 3 or _CONTACT.search(line):
            continue
        if total + len(line) + 1 > max_chars:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept).strip() or FALLBACK
