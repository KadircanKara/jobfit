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

# Only the first 4000 characters of title + description, mirroring `_haystack`
# in jobhunt/rank/deterministic.py:129 - a client who names themselves does it
# in the opening pitch ("Hi, I'm ... founder of ...") or the title itself (a
# product-named title like "Northquill.ai saas repair" is common and free to
# read), and the tail of a long posting is boilerplate (application
# instructions, screening questions, Upwork's own footer text) where a stray
# domain or name is far more likely to belong to a tool, a competitor, or
# Upwork itself than to the client.
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

# A sentence boundary a subject-first pattern ("X is looking for") may start
# right after: absolute string start, a run's own newline, or the end of the
# previous sentence (optionally followed by whitespace/indentation). Without
# this, an unanchored `^` (or one anchored only to the true start of the
# whole string) captures everything back to position zero the first time the
# phrase appears anywhere but the very first sentence - two sentences
# squashed into one "name".
_SENTENCE_START = r"(?:^|[.!?\n])\s*"

# A company name that introduces itself, strongest first. Matched against the
# original-cased text (not lowercased) so the captured group keeps the case
# the client wrote it in.
_COMPANY_PATTERNS = [
    re.compile(r"\bproduct called ([A-Z][\w.&' -]{1,40}?)\b[.,]"),
    re.compile(r"\bour (?:company|startup|business),?\s+([A-Z][\w.&' -]{1,40}?),"),
    re.compile(r"\bwe['’]re\s+([A-Z][\w.&' -]{1,40}?)[,.]"),
    re.compile(_SENTENCE_START + r"([A-Z][\w.&' -]{1,40}?)\s+is looking for\b"),
    re.compile(_SENTENCE_START + r"([A-Z][\w.&' -]{1,40}?)\s+is hiring\b"),
]

# Someone introducing themselves by name and, usually, their company. "This
# is" is scoped case-insensitive with an inline group (`(?i:...)`) rather
# than flagging the whole pattern - a sentence genuinely starting "This is
# Alex..." is exactly as valid a signal as "this is Alex...", but the name
# captures themselves must stay capitalised or the pattern would also fire on
# unrelated lowercase prose.
_PERSON_PATTERNS = [
    re.compile(
        r"\bI['’]m\s+([A-Z][a-z]+),?\s+(?:the\s+)?founder of\s+([A-Z][\w.&' -]{1,40}?)[.,]"
    ),
    re.compile(
        r"(?i:this is)\s+([A-Z][a-z]+)\s+from\s+([A-Z][\w.&' -]{1,40}?)[.,]"
    ),
]

# Words a company-name capture must not equal outright - generic openers
# that happen to be capitalized and precede "is looking for"/"is hiring".
_GENERIC_SUBJECTS = frozenset({
    "i", "we", "this", "our team", "our client", "the client", "the company",
})

# A captured name that needs a full stop is not a name - it is two sentences
# an unanchored regex glued together. Real company and person names are also
# short: a handful of words at most.
_MAX_NAME_CHARS = 60
_MAX_NAME_WORDS = 6
_SENTENCE_BREAK_RE = re.compile(r"[.!?\n]")


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
    window = f"{title}\n{text}"[:_SCAN_WINDOW]

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


def _brand(domain: str) -> str:
    """The brand label out of a noise domain - "zoom" out of "zoom.us"."""
    labels = domain.split(".")
    return labels[-2] if len(labels) >= 2 else labels[0]


# The same noise list, but for company/person captures: "Zoom", "zoom" and
# "zoom.us" must all be recognised as the same noise entry. A posting saying
# "this is Alex from Zoom, we want a Zoom plugin" is naming an integration
# target, not the client - the domain noise list alone would miss this
# entirely, since no domain-shaped string ever appears in that sentence.
_NOISE_BRANDS = frozenset(_brand(host) for host in _NOISE_DOMAINS)


def _normalize_word(word: str) -> str:
    """Lowercase, drop punctuation, and collapse a domain-shaped word to its
    brand label - "zoom.us," and "Zoom" both become "zoom" - so the same
    comparison recognises the brand whether it shows up bare, capitalised, or
    written as its own domain inside a captured name.
    """
    lowered = re.sub(r"[^a-z0-9.]", "", word.lower())
    return lowered.split(".")[0]


def _is_noise_name(name: str) -> bool:
    """True if ANY word in the name is a noise brand, not just the whole name.

    A naive whole-string check lets "Zoom Inc", "The Zoom team" and "Slack
    app" straight through, because none of those strings equals "zoom" or
    "slack" exactly - but each of them is still naming the noise entity plus
    one qualifier word. Checking word-by-word closes that gap without
    rejecting real multi-word names: "Karma and Luck" and "Booz Allen
    Hamilton" contain no word that is itself a noise brand.
    """
    return any(_normalize_word(word) in _NOISE_BRANDS for word in name.split())


def _plausible_name(name: str) -> bool:
    if not name or len(name) > _MAX_NAME_CHARS:
        return False
    if len(name.split()) > _MAX_NAME_WORDS:
        return False
    return not _SENTENCE_BREAK_RE.search(name)


def _valid_company_name(name: str) -> bool:
    if name.lower() in _GENERIC_SUBJECTS:
        return False
    if not _plausible_name(name):
        return False
    return not _is_noise_name(name)


def _find_company(window: str) -> str | None:
    for pattern in _COMPANY_PATTERNS:
        for match in pattern.finditer(window):
            name = _clean_name(match.group(1))
            if name and _valid_company_name(name):
                return name
    return None


def _find_person(window: str) -> tuple[str | None, str | None]:
    for pattern in _PERSON_PATTERNS:
        for match in pattern.finditer(window):
            person = match.group(1).strip()
            company_raw = _clean_name(match.group(2))
            if company_raw and _is_noise_name(company_raw):
                # The sentence itself says this person is "from Zoom" - they
                # are affiliated with the noise entity, not the client. Drop
                # the whole match (person included) rather than keep a name
                # now known to point at the wrong company, and keep looking.
                continue
            company = company_raw if company_raw and _valid_company_name(company_raw) else None
            return person, company
    return None, None


def _clean_name(raw: str) -> str | None:
    name = raw.strip().strip(".,")
    return name or None


# The same TLDs `_BARE_HOST_RE` recognises as domain-shaped - stripping one
# off the end when deriving a name is the mirror image of requiring one to
# recognise a domain in the first place.
_KNOWN_TLDS = frozenset({"ai", "io", "com", "co", "dev", "app", "net", "org", "xyz", "so"})


def name_from_domain(domain: str) -> str:
    """Turn a bare domain into a plain, searchable company name.

    The domain is the safest signal this module produces - unlike a pattern
    match, it is nearly unfakeable - so it is worth cleaning up before it
    reaches `discovery.search_company`: "northquill.ai" searches better as
    "Northquill" than as the raw hostname, and "acme-labs.io" as "Acme Labs".
    """
    labels = domain.split(".")
    core = labels[-2] if len(labels) >= 2 and labels[-1] in _KNOWN_TLDS else labels[0]
    words = re.split(r"[-_]+", core)
    return " ".join(word.capitalize() for word in words if word) or domain
