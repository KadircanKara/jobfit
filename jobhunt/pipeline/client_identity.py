"""Recover the client's identity from an Upwork job description.

Upwork's API is deliberately anonymous: it exposes a client country, state
and timezone, and never a company name (see `jobhunt/sources/upwork.py`'s
`_client_company` comment). The user's outreach strategy skips Upwork's own
proposal system - applying spends Connects this account does not have - and
instead finds a person at the client's company on LinkedIn through the
existing "Find contacts" flow. That flow needs a `company_name` or
`company_domain` to search for, and the description text is the only place
one can come from.

This module is pure and does no I/O: it is a text classifier, not a fetcher.
Everything here is a heuristic tuned against real postings, not a guarantee -
a false positive here is not a bad row in a table, it is a real LinkedIn
message sent from the user's real account to a stranger at the wrong
company. When a signal is ambiguous the right answer is always to return
nothing, never to guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Only the first 4000 characters, mirroring `_haystack` in
# jobhunt/rank/deterministic.py:129 - a client who names themselves does it
# in the opening pitch ("Hi, I'm ... founder of ..."), and the tail of a long
# posting is boilerplate (application instructions, screening questions,
# Upwork's own footer text) where a stray domain or name is far more likely
# to belong to a tool, a competitor, or Upwork itself than to the client.
_SCAN_WINDOW = 4000

# Domains a job description legitimately mentions that are never the client:
# collaboration tools, code hosts, meeting software, and the platform itself.
# A naive domain regex returns one of these for a large fraction of the
# corpus (github.com and docs.google.com are named constantly - as the repo
# to work in or the doc to read, never as the client), so this list is the
# difference between the extractor being useful and being actively harmful.
_NOISE_DOMAINS = frozenset({
    "upwork.com",
    "www.upwork.com",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "google.com",
    "docs.google.com",
    "drive.google.com",
    "sheets.google.com",
    "figma.com",
    "notion.so",
    "notion.com",
    "slack.com",
    "zoom.us",
    "loom.com",
    "openai.com",
    "anthropic.com",
    "youtube.com",
    "youtu.be",
    "linkedin.com",
    "calendly.com",
    "trello.com",
    "asana.com",
    "jira.com",
    "atlassian.com",
    "airtable.com",
    "monday.com",
    "clickup.com",
    "hubspot.com",
    "salesforce.com",
    "stripe.com",
    "aws.amazon.com",
    "amazon.com",
    "microsoft.com",
    "apple.com",
    "wordpress.com",
    "wordpress.org",
    "shopify.com",
    "dropbox.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "reddit.com",
    "medium.com",
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "icloud.com",
})

_URL_RE = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:ai|io|com|co|dev|app|net|org|xyz|so))\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", re.IGNORECASE)

# Generic free-mail domains are real domains but never a client's own
# identity, so an email at one of these should not surface as `domain`.
_FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "aol.com", "live.com", "msn.com",
})

# A company name that introduces itself, strongest first. Matched against the
# original-cased text (not lowercased) so the captured group keeps the case
# the client wrote it in.
_COMPANY_PATTERNS = [
    re.compile(r"\bproduct called ([A-Z][\w.&' -]{1,40}?)\b[.,]"),
    re.compile(r"\bour (?:company|startup|business),?\s+([A-Z][\w.&' -]{1,40}?),"),
    re.compile(r"\bwe['’]re\s+([A-Z][\w.&' -]{1,40}?)[,.]"),
    re.compile(r"^([A-Z][\w.&' -]{1,40}?)\s+is looking for\b", re.MULTILINE),
    re.compile(r"^([A-Z][\w.&' -]{1,40}?)\s+is hiring\b", re.MULTILINE),
]

# Someone introducing themselves by name and, usually, their company.
_PERSON_PATTERNS = [
    re.compile(
        r"\bI['’]m\s+([A-Z][a-z]+),?\s+(?:the\s+)?founder of\s+([A-Z][\w.&' -]{1,40}?)[.,]"
    ),
    re.compile(
        r"\bthis is\s+([A-Z][a-z]+)\s+from\s+([A-Z][\w.&' -]{1,40}?)[.,]"
    ),
]

# Words a company-name capture must not equal outright - generic openers
# that happen to be capitalized and precede "is looking for"/"is hiring".
_GENERIC_SUBJECTS = frozenset({
    "i", "we", "this", "our team", "our client", "the client", "the company",
})


@dataclass(frozen=True)
class Identity:
    """Whatever could be recovered about the client. Any field may be None."""

    domain: str | None
    company_name: str | None
    person_name: str | None


_NOTHING = Identity(domain=None, company_name=None, person_name=None)


def detect(title: str, description: str | None) -> Identity:
    """Look for the client's company or the person writing the posting.

    Domain signals (a URL, a bare hostname, an email) outrank a named
    company, which outranks a named person - a stated domain is nearly
    unfakeable, while "X is looking for" can be a red herring further down
    the text. Returns `_NOTHING` rather than a guess whenever no signal
    clears the noise list.
    """
    text = description or ""
    window = text[:_SCAN_WINDOW]

    domain = _find_domain(window)
    company_name = _find_company(window)
    person_name, person_company = _find_person(window)

    if person_company and not company_name:
        company_name = person_company

    if domain is None and company_name is None and person_name is None:
        return _NOTHING

    return Identity(domain=domain, company_name=company_name, person_name=person_name)


def _find_domain(window: str) -> str | None:
    for pattern in (_URL_RE, _EMAIL_RE, _BARE_HOST_RE):
        for match in pattern.finditer(window):
            candidate = match.group(1).lower().rstrip(".")
            if _is_noise_domain(candidate) or candidate in _FREEMAIL_DOMAINS:
                continue
            return candidate
    return None


def _is_noise_domain(domain: str) -> bool:
    if domain in _NOISE_DOMAINS:
        return True
    # A subdomain of a noise host (e.g. "boards.greenhouse.io" is not in the
    # list, but "docs.google.com" style parents should still catch their own
    # subdomains) is noise too.
    return any(domain == host or domain.endswith(f".{host}") for host in _NOISE_DOMAINS)


def _find_company(window: str) -> str | None:
    for pattern in _COMPANY_PATTERNS:
        match = pattern.search(window)
        if not match:
            continue
        name = _clean_name(match.group(1))
        if name and name.lower() not in _GENERIC_SUBJECTS:
            return name
    return None


def _find_person(window: str) -> tuple[str | None, str | None]:
    for pattern in _PERSON_PATTERNS:
        match = pattern.search(window)
        if not match:
            continue
        person = match.group(1).strip()
        company = _clean_name(match.group(2))
        if company and company.lower() in _GENERIC_SUBJECTS:
            company = None
        return person, company
    return None, None


def _clean_name(raw: str) -> str | None:
    name = raw.strip().strip(".,")
    return name or None
