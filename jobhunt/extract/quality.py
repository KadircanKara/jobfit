"""The quality gate every rung's output must clear. PLAN.md section 9.5.

A rung that produces text failing this gate is treated as having failed, and the
ladder falls through to the next one. That is the whole reason the gate exists:
without it, rung 1 returning a two-line SEO stub would stop the ladder and hand
a stub to the tailoring skill, which is the single worst failure this system has.
"""
from __future__ import annotations

import dataclasses
import re

MIN_CHARS = 400

# English and Turkish. A Turkish posting must clear the same bar as an English
# one, not a lower one, or tr_local quietly gets worse CVs.
SECTION_MARKERS = (
    "requirement", "responsibilit", "qualification", "what you", "about the role",
    "you will", "we are looking", "must have", "nice to have", "your profile",
    "gereksinim", "aranan nitelik", "sorumluluk", "iş tanımı", "is tanimi",
    "nitelikler", "genel nitelikler", "beklenen", "aday", "görev",
)

TRUNCATION_MARKERS = (
    "read more", "show more", "see more", "devamını oku", "devamini oku",
    "daha fazla", "continue reading", "view full",
)

BOILERPLATE_PATTERNS = (
    r"equal opportunity employer",
    r"we are an equal",
    r"cookie",
    r"privacy policy",
    r"all qualified applicants will receive consideration",
    r"without regard to race",
    r"e-?verify",
    r"kişisel verilerin korunması",
    r"kvkk",
    r"çerez",
)
_BOILERPLATE = re.compile("|".join(BOILERPLATE_PATTERNS), re.I)
_SENTENCE = re.compile(r"[.!?\n]+")


@dataclasses.dataclass
class Quality:
    score: float
    passed: bool
    reasons: list[str] = dataclasses.field(default_factory=list)
    chars: int = 0

    def as_notes(self) -> dict:
        return {"score": round(self.score, 3), "passed": self.passed, "reasons": self.reasons}


def assess(text: str | None) -> Quality:
    """Score extracted prose. 1.0 is clean and complete, 0.0 is unusable."""
    body = (text or "").strip()
    reasons: list[str] = []
    if not body:
        return Quality(score=0.0, passed=False, reasons=["empty"], chars=0)

    score = 1.0

    if len(body) < MIN_CHARS:
        reasons.append(f"only {len(body)} chars, want {MIN_CHARS}")
        score -= 0.45

    lowered = body.lower()
    sentences = [s for s in _SENTENCE.split(body) if len(s.strip()) > 20]
    has_section = any(marker in lowered for marker in SECTION_MARKERS)
    if not has_section and len(sentences) < 5:
        reasons.append("no requirements or responsibilities section and under 5 sentences")
        score -= 0.3

    # Measured over whole paragraphs, not matched phrases: an EEO block is 400
    # words hanging off a 6 word trigger, and counting only the trigger would
    # report 2 percent boilerplate for a page that is nothing else.
    boilerplate_ratio = _boilerplate_ratio(body)
    if boilerplate_ratio > 0.6:
        # PLAN.md 9.5 says reject, not penalise. A page that is 95 percent EEO
        # and cookie text has no JD in it, whatever the rest of the score says.
        reasons.append(f"{boilerplate_ratio:.0%} boilerplate")
        score -= 0.4

    tail = body[-60:].strip().lower()
    if tail.endswith("...") or tail.endswith("…"):
        reasons.append("ends mid-sentence")
        score -= 0.25
    elif any(marker in tail for marker in TRUNCATION_MARKERS):
        reasons.append("ends with a read-more marker")
        score -= 0.25

    score = max(0.0, min(1.0, score))
    return Quality(score=score, passed=score >= 0.5 and not _fatal(reasons),
                   reasons=reasons, chars=len(body))


def _fatal(reasons: list[str]) -> bool:
    """Reasons that fail the gate outright, regardless of the numeric score."""
    return any(
        reason.startswith("only ") or reason == "empty" or reason.endswith("boilerplate")
        for reason in reasons
    )


def _boilerplate_ratio(body: str) -> float:
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paragraphs:
        return 0.0
    boilerplate = sum(len(p) for p in paragraphs if _BOILERPLATE.search(p))
    return boilerplate / max(1, len(body))
