"""The ATS URL regex bank. PLAN.md section 3.5, Strategy A.

One place, heavily tested. Every URL the system ever sees runs through here:
apply_url, source_url, and the raw description HTML, because aggregators embed
the real apply link in the body as often as in a structured field.

Two output kinds, deliberately separate:

- BoardHit    the token is present in the URL, so the board is directly fetchable.
- DomainHint  the URL proves the company uses a provider but hides the token
              behind their own domain (`stripe.com/jobs?gh_jid=123`). The token
              has to be guessed or probed, so these are kept apart from hits and
              enter the boards table with a lower expectation of success.
"""
from __future__ import annotations

import dataclasses
import html
import re
import urllib.parse

# Subdomain labels that are never a customer token on a subdomain-keyed provider.
RESERVED_LABELS = frozenset(
    {
        "www", "api", "app", "apps", "jobs", "job", "careers", "career", "apply",
        "static", "cdn", "assets", "help", "support", "docs", "blog", "status",
        "mail", "admin", "my", "go", "embed", "widget", "partners", "developers",
    }
)

# Path segments that follow a provider host but are plumbing, not a token.
RESERVED_SEGMENTS = frozenset(
    {
        "embed", "api", "v0", "v1", "v2", "v3", "static", "assets", "search",
        "jobs", "job", "postings", "posting", "companies", "company", "about",
        "privacy", "terms", "login", "signup", "widget", "js", "css", "images",
    }
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Common Crawl surfaces these alongside real boards. They are files and template
# placeholders, not customers, and each one would otherwise cost a fetch.
_JUNK_SUFFIXES = (".txt", ".xml", ".ico", ".json", ".php", ".html", ".js", ".css", ".png", ".svg")
_JUNK_TOKENS = frozenset({"your_company", "yourcompany", "company_name", "companyname",
                          "example", "test", "demo", "null", "undefined", "token"})
_HEXISH = re.compile(r"^[0-9a-f]{16,}$")


@dataclasses.dataclass(frozen=True, order=True)
class BoardHit:
    """A directly fetchable board: provider plus the token the endpoint wants."""

    provider: str
    token: str


@dataclasses.dataclass(frozen=True, order=True)
class DomainHint:
    """The provider is known, the token is not. Feed to the domain prober."""

    provider: str
    domain: str


def _clean_token(token: str) -> str | None:
    token = urllib.parse.unquote(token).strip().strip("/")
    if not token or not _TOKEN_RE.match(token):
        return None
    if token.lower() in RESERVED_SEGMENTS:
        return None
    # A bare number is a job id that leaked out of a malformed URL, never a token.
    if token.isdigit():
        return None
    lowered = token.lower()
    if lowered in _JUNK_TOKENS or lowered.endswith(_JUNK_SUFFIXES) or _HEXISH.match(lowered):
        return None
    return token


def _clean_label(label: str) -> str | None:
    label = label.lower()
    if label in RESERVED_LABELS:
        return None
    return _clean_token(label)


# Each entry: (provider, compiled pattern, group extractor).
# Patterns are scheme-optional because URLs turn up in HTML attributes, in
# markdown, and protocol-relative. Everything is matched case-insensitively on
# the host; path tokens keep their case, because SmartRecruiters companyIds are
# capitalized on the wire ("Visa") and the API is case-sensitive.

_HOST_KEYED: list[tuple[str, re.Pattern[str]]] = [
    ("recruitee", re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})\.recruitee\.com", re.I)),
    ("personio", re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})\.jobs\.personio\.(?:de|com)", re.I)),
    ("teamtailor", re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})\.teamtailor\.com", re.I)),
    ("workable", re.compile(r"\b([a-z0-9][a-z0-9-]{0,62})\.workable\.com/j/", re.I)),
]

_PATH_KEYED: list[tuple[str, re.Pattern[str]]] = [
    ("ashby", re.compile(r"\bjobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("ashby", re.compile(r"\bembed\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.I)),
    ("ashby", re.compile(r"\bapi\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9._-]+)", re.I)),
    (
        "greenhouse",
        re.compile(r"\bboards\.greenhouse\.io/embed/job_board\?[^\"'\s]*for=([A-Za-z0-9._-]+)", re.I),
    ),
    ("greenhouse", re.compile(r"\b(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"\bboards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"\bjobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("lever", re.compile(r"\bapi\.lever\.co/v0/postings/([A-Za-z0-9._-]+)", re.I)),
    ("workable", re.compile(r"\bapply\.workable\.com/(?:api/[a-z0-9/]+/)?([A-Za-z0-9._-]+)/j/", re.I)),
    ("smartrecruiters", re.compile(r"\b(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9._-]+)", re.I)),
    ("smartrecruiters", re.compile(r"\bapi\.smartrecruiters\.com/v1/companies/([A-Za-z0-9._-]+)", re.I)),
]

# Workday needs three parts to be fetchable, so it gets its own pattern and a
# composite token. tenant alone is useless: the endpoint is keyed by
# {tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs and the site
# name is not derivable from the tenant.
_WORKDAY_RE = re.compile(
    r"\b([a-z0-9][a-z0-9-]{0,62})\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:wday/cxs/[a-z0-9-]+/)?"
    r"(?:[a-z]{2}-[A-Z]{2}/)?"
    r"([A-Za-z0-9._-]+)",
    re.I,
)

# Employer-hosted pages that only prove which provider is behind them. The
# Greenhouse probe found that `absolute_url` usually points at the employer
# (stripe.com/jobs?gh_jid=...), so this case is the rule, not the exception.
_HINT_PARAMS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"[?&]gh_jid=", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_src=", re.I)),
    ("lever", re.compile(r"[?&]lever-origin=", re.I)),
]

_PROVIDER_HOSTS = re.compile(
    r"\b(?:greenhouse\.io|ashbyhq\.com|lever\.co|recruitee\.com|workable\.com|"
    r"smartrecruiters\.com|personio\.(?:de|com)|teamtailor\.com|myworkdayjobs\.com)",
    re.I,
)


def scan(text: str | None) -> tuple[set[BoardHit], set[DomainHint]]:
    """Run the whole bank over one blob of text. Returns (hits, hints)."""
    hits: set[BoardHit] = set()
    hints: set[DomainHint] = set()
    if not text:
        return hits, hints

    # HTML-escaped and unicode-escaped URLs are common inside description bodies
    # and inside JSON embedded in a script tag. Decode before matching.
    decoded = html.unescape(text).replace("\\/", "/").replace("\\u002F", "/")

    for provider, pattern in _HOST_KEYED:
        for match in pattern.finditer(decoded):
            label = _clean_label(match.group(1))
            if label:
                hits.add(BoardHit(provider, label))

    for provider, pattern in _PATH_KEYED:
        for match in pattern.finditer(decoded):
            token = _clean_token(match.group(1))
            if token:
                hits.add(BoardHit(provider, token))

    for match in _WORKDAY_RE.finditer(decoded):
        tenant = _clean_label(match.group(1))
        site = _clean_token(match.group(3))
        if tenant and site:
            hits.add(BoardHit("workday", f"{tenant}/{match.group(2).lower()}/{site}"))

    for provider, pattern in _HINT_PARAMS:
        if not pattern.search(decoded):
            continue
        for url in _urls(decoded):
            if pattern.search(url):
                domain = _registrable_domain(url)
                if domain:
                    hints.add(DomainHint(provider, domain))

    return hits, hints


_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def _urls(text: str) -> list[str]:
    return _URL_RE.findall(text)


def _registrable_domain(url: str) -> str | None:
    """Host of a URL, minus www. Not a public-suffix parse, and does not need to be.

    The only consumer is the token guesser, which strips the TLD anyway, so
    getting `careers.acme.co.uk` instead of `acme.co.uk` costs a wasted guess and
    nothing else.
    """
    try:
        host = urllib.parse.urlsplit(url).hostname
    except ValueError:
        return None
    if not host or _PROVIDER_HOSTS.search(host):
        return None
    host = host.lower().removeprefix("www.")
    return host if "." in host else None


def guess_token(domain: str) -> str | None:
    """Domain minus subdomains minus TLD. PLAN.md section 3.5, Strategy E step 3.

    A guess, not a fact. It enters the boards table as a candidate and the
    validation loop kills it on the first 404, which costs exactly one request.
    """
    labels = [label for label in domain.lower().split(".") if label]
    if len(labels) < 2:
        return None
    core = labels[-2]
    # acme.co.uk and acme.com.tr both have a two-letter public suffix label in
    # position -2, so step one further left.
    if len(core) <= 3 and len(labels) >= 3 and core in {"co", "com", "net", "org", "gov", "ac"}:
        core = labels[-3]
    return _clean_label(core)


def scan_many(texts: list[str | None]) -> tuple[set[BoardHit], set[DomainHint]]:
    hits: set[BoardHit] = set()
    hints: set[DomainHint] = set()
    for text in texts:
        found, hinted = scan(text)
        hits |= found
        hints |= hinted
    return hits, hints
