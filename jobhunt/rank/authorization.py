"""Work authorization and visa sponsorship: what a posting demands, and the call.

Two facts matter about a posting, and the rules turn on nothing else:

- where it *requires* the applicant to already be authorized to work ("must be
  authorized to work in the United States"), and
- whether it offers visa sponsorship, refuses it, or says nothing.

They come from two places. This module reads the plain wordings with patterns,
for free, over every job. The fit gate reads the whole posting for the jobs it
scores and returns the same two facts, which then take precedence: a model
that read the posting beats a pattern that read a sentence of it.

The decision, as the user set it:

- sponsorship offered: keep, whatever the candidate is authorized for;
- a hard authorization requirement: keep only if the candidate is authorized
  in one of the countries it names;
- sponsorship refused with no hard requirement: drop only if the candidate
  needs sponsorship and is not authorized in the job's country;
- otherwise: keep.

Everything here fails open. A sentence the patterns cannot place is ignored,
never guessed at, so a misread can only fail to drop a job - the gate then has
the rest of the posting to catch it.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from jobhunt.rank import regions

OFFERED = "offered"
REFUSED = "refused"
SPONSORSHIP_VALUES = (OFFERED, REFUSED)

# How much of a description to read. Authorization lines sit in the
# requirements list or the footer, so the whole body is read, not the opening
# slice the other rules use - but capped, so a pathological page stays cheap.
SCAN_CHARS = 30_000

# A place a pattern captured: letters, dots and spaces, up to where the
# sentence moves on. Kept short so a capture cannot swallow a clause.
_PLACE = (
    r"(?:the\s+)?([a-z][a-z. ]{1,30}?)"
    r"(?=[,.;:()\n/]|\s+(?:and|or|without|with|for|at|to|on|who|by|as|is|are)\b|$)"
)

_REQUIRED_PATTERNS = (
    # "must be (legally) authorized to work in the US"
    re.compile(
        r"(?<!\w)(?:must|need to|needs to|required to|have to|should)\s+(?:already\s+)?"
        r"(?:be\s+|have\s+)?(?:legally\s+|currently\s+|fully\s+)?"
        r"(?:authori[sz]ed|eligible|permitted|entitled|able)\s+to\s+work\s+in\s+" + _PLACE
    ),
    # "(valid|existing) work authorization / right to work / work permit for the UK"
    re.compile(
        r"(?<!\w)(?:valid|existing|current|full|unrestricted|legal)\s+"
        r"(?:work\s+authori[sz]ation|right\s+to\s+work|work\s+permit|work\s+visa|work\s+rights)"
        r"\s+(?:in|for)\s+" + _PLACE
    ),
    # "right to work in the UK is required"
    re.compile(
        r"(?<!\w)(?:the\s+)?right\s+to\s+work\s+in\s+" + _PLACE
        + r"[^.\n]{0,40}?\b(?:required|essential|mandatory|a\s+must)\b"
    ),
    # "US work authorization required", "EU work permit is required"
    re.compile(
        r"(?<!\w)([a-z][a-z.]{1,20}(?:\s+[a-z.]{2,15})?)\s+"
        r"(?:work\s+authori[sz]ation|work\s+permit|right\s+to\s+work)\s+(?:is\s+)?"
        r"(?:required|mandatory|essential)"
    ),
    # "open to US citizens and permanent residents only", "must be a US citizen"
    re.compile(
        r"(?<!\w)(?:must\s+be\s+(?:a\s+)?|only\s+(?:open\s+)?to\s+|open\s+(?:only\s+)?to\s+|restricted\s+to\s+)"
        r"([a-z][a-z.]{1,20}(?:\s+[a-z.]{2,15})?)\s+(?:citizens?|nationals?|permanent\s+residents?)"
    ),
    # "green card holders" implies US status on its own.
    re.compile(r"(?<!\w)(green\s+card)\s+(?:holders?|required)"),
)

_REFUSED = re.compile(
    r"(?<!\w)(?:"
    r"(?:unable|not\s+able|cannot|can\s*not|can't|do\s+not|don't|does\s+not|doesn't|"
    r"will\s+not|won't|are\s+not\s+able\s+to|is\s+not\s+able\s+to)\s+"
    r"(?:to\s+)?(?:currently\s+)?(?:offer|provide|support|sponsor)(?:\s+(?:visa|work\s+permit|immigration))?"
    r"(?:\s+sponsorship)?"
    r"|(?:no|without)\s+(?:visa\s+|immigration\s+)?sponsorship"
    r"|sponsorship\s+(?:is\s+)?(?:not\s+(?:available|offered|provided|possible)|unavailable)"
    r"|not\s+(?:offering|providing)\s+(?:visa\s+)?sponsorship"
    r")"
)

_OFFERED = re.compile(
    r"(?<!\w)(?:"
    r"(?:visa|immigration|work\s+permit)\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided|possible)"
    r"|(?:we|will|can|happy\s+to|able\s+to)\s+(?:offer\s+|provide\s+)?(?:to\s+)?sponsor\s+(?:your\s+|a\s+|the\s+)?(?:visa|work\s+permit)"
    r"|(?:offer|provide|including)\s+(?:full\s+)?(?:visa|work\s+permit)\s+(?:sponsorship|support)"
    r"|relocation\s+and\s+visa\s+(?:support|sponsorship|assistance)"
    r"|visa\s+(?:support|sponsorship)\s+(?:and|&)\s+relocation"
    r")"
)

# Short forms a posting writes that the region table does not spell.
_PLACE_ALIASES = {
    "us": "US", "u.s.": "US", "u.s": "US", "usa": "US", "u.s.a.": "US", "u.s.a": "US",
    "united states of america": "US", "american": "US", "green card": "US",
    "uk": "GB", "u.k.": "GB", "u.k": "GB", "british": "GB",
    "canada": "CA", "canadian": "CA", "german": "DE", "dutch": "NL", "french": "FR",
    "australian": "AU", "irish": "IE", "turkish": "TR",
}

# Words that close a "the ... only" capture but are never a place.
_NOT_PLACES = frozenset({"the", "country", "region", "location", "same", "any", "our", "this"})


@dataclasses.dataclass(frozen=True)
class Facts:
    """What a posting says. `required` empty and `sponsorship` None: nothing."""

    required: frozenset[str] = frozenset()
    sponsorship: str | None = None


NOTHING = Facts()


def codes_for(place: str) -> set[str]:
    """ISO codes a place name covers, or nothing if it is not a place.

    A capture can carry words from before the place ("this role is US"), so
    its trailing words are tried longest first and the first place wins.
    """
    words = place.strip(" .").lower().split()
    for start in range(len(words)):
        name = " ".join(words[start:])
        if name in _NOT_PLACES or name in regions.WORLDWIDE_NAMES:
            continue
        if name in _PLACE_ALIASES:
            return {_PLACE_ALIASES[name]}
        if len(name) == 2 and name not in regions.REGIONS:
            # A bare ISO code in prose is usually an English word: "it", "in",
            # "no", "is". Only the spellings listed above count as places.
            continue
        codes, _ = regions.resolve([name])
        if codes:
            return set(codes)
    return set()


def extract(text: str | None) -> Facts:
    """The two facts, read from a posting's text by pattern alone."""
    if not text:
        return NOTHING
    lowered = re.sub(r"[ \t]+", " ", text[:SCAN_CHARS].lower())

    required: set[str] = set()
    for pattern in _REQUIRED_PATTERNS:
        for match in pattern.finditer(lowered):
            required |= codes_for(match.group(1))

    refused = bool(_REFUSED.search(lowered))
    offered = bool(_OFFERED.search(lowered))
    # Both at once is usually "we sponsor for X but not for Y", which only a
    # reader can untangle. Left to the gate rather than decided either way.
    sponsorship = None if refused == offered else (REFUSED if refused else OFFERED)
    return Facts(required=frozenset(required), sponsorship=sponsorship)


def merge(first: Facts | None, second: Facts) -> Facts:
    """Both readings at once. The gate read at most the opening of a long
    posting, and the footer is where authorization lines often sit, so what the
    patterns found there is kept rather than overruled; the gate's sponsorship
    answer wins where it gave one."""
    if first is None:
        return second
    return Facts(
        required=first.required | second.required,
        sponsorship=first.sponsorship or second.sponsorship,
    )


# Words that mark a sentence worth showing the gate even past its reading limit.
_MENTIONS = re.compile(
    r"authori[sz]|sponsor|visa|work permit|right to work|citizen|green card|eligible to work",
    re.I,
)


def relevant_lines(text: str | None, skip: int, limit: int = 1200) -> str:
    """Sentences past the first `skip` characters that talk about authorization.

    The gate reads a truncated description; these ride along so a requirement
    stated in the footer still reaches it.
    """
    if not text or len(text) <= skip:
        return ""
    found: list[str] = []
    total = 0
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text[skip:SCAN_CHARS]):
        sentence = sentence.strip()
        if sentence and _MENTIONS.search(sentence):
            if total + len(sentence) > limit:
                break
            found.append(sentence)
            total += len(sentence)
    return " ".join(found)


def from_gate(verdict: dict[str, Any]) -> Facts | None:
    """The same facts from a gate verdict, or None if it did not report them."""
    places = verdict.get("work_authorization_required")
    sponsorship = verdict.get("visa_sponsorship")
    if places is None and sponsorship is None:
        return None
    required: set[str] = set()
    if isinstance(places, list):
        for place in places:
            required |= codes_for(str(place))
    value = str(sponsorship or "").strip().lower()
    return Facts(
        required=frozenset(required),
        sponsorship=value if value in SPONSORSHIP_VALUES else None,
    )


@dataclasses.dataclass(frozen=True)
class Candidate:
    """What the user ticked. Built once per pass from the filter rules."""

    authorized: frozenset[str]
    anywhere: bool
    needs_sponsorship: bool

    @classmethod
    def from_rules(cls, rules: dict[str, Any]) -> Candidate:
        return cls(
            authorized=frozenset(rules.get("work_authorization") or ()),
            anywhere=bool(rules.get("authorized_anywhere")),
            needs_sponsorship=bool(rules.get("sponsorship_required")),
        )


def decide(facts: Facts, country: str | None, who: Candidate) -> tuple[str, str] | None:
    """(code, reason) if the job should go, None if it stays."""
    if facts.sponsorship == OFFERED or who.anywhere:
        return None
    if facts.required:
        if facts.required & who.authorized:
            return None
        return "auth_required", f"requires work authorization in {_names(facts.required)}"
    if facts.sponsorship == REFUSED and who.needs_sponsorship:
        if not country or country in who.authorized:
            return None
        return "no_sponsorship", f"no visa sponsorship, and you are not authorized in {_names({country})}"
    return None


def _names(codes: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(regions.country_name(code) or code for code in codes))
