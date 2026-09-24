"""Pure normalization helpers shared by every adapter.

No I/O, no session, no network. Everything here must be safe to call from
normalize() and from a test with no database.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
import unicodedata

from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from markdownify import markdownify

# --- company identity ---------------------------------------------------------

_LEGAL_SUFFIXES = {
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.", "corporation",
    "gmbh", "bv", "b.v.", "nv", "n.v.", "ab", "as", "oy", "sa", "s.a.", "plc",
    "co", "co.", "company", "holding", "holdings",
    "as.", "a.s.", "anonim", "sirketi", "şirketi", "ltd.sti", "sti", "şti",
}
# Deliberately NOT suffixes: "labs", "group", "technologies", "systems", "software".
# They read like suffixes but are load-bearing parts of real names, and stripping
# them collides Acme Labs with Acme Group.
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """Lowercase, strip accents, punctuation, and legal suffixes.

    "Acme Technologies, Inc." and "acme technologies" collapse to "acme".
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", text.lower())
    words = [w for w in _WS.sub(" ", text).strip().split(" ") if w]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or _WS.sub(" ", text).strip()


def domain_from_url(url: str | None) -> str | None:
    """Extract a bare registrable-ish host, dropping www. Returns None for ATS hosts."""
    if not url:
        return None
    match = re.match(r"https?://([^/?#]+)", url.strip(), re.IGNORECASE)
    if not match:
        return None
    host = match.group(1).lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    # An ATS host identifies the ATS, never the employer.
    ats_hosts = (
        "greenhouse.io", "ashbyhq.com", "lever.co", "recruitee.com", "workable.com",
        "smartrecruiters.com", "personio.de", "myworkdayjobs.com", "teamtailor.com",
        "himalayas.app", "remoteok.com", "remotive.com", "jobicy.com", "arbeitnow.com",
        "weworkremotely.com", "linkedin.com",
    )
    if any(host == h or host.endswith("." + h) for h in ats_hosts):
        return None
    return host or None


# --- title, seniority, role family --------------------------------------------

# Ordered most specific first: "senior staff engineer" must not match "senior".
SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern|internship|stajyer|trainee|working student|werkstudent)\b", re.I)),
    ("founding", re.compile(r"\bfounding\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\bstaff\b", re.I)),
    # senior is checked before lead so "Senior Manager" reads as senior, not lead.
    # Plain "Manager" still falls through to lead.
    ("senior", re.compile(r"\b(senior|sr\.?|kıdemli|kidemli)\b", re.I)),
    ("lead", re.compile(r"\b(lead|leader|head of|manager|müdür|yönetici)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|associate)\b", re.I)),
    ("mid", re.compile(r"\b(mid[- ]level|intermediate)\b", re.I)),
]

ROLE_FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ai_ml", re.compile(r"\b(machine learning|ml|ai|artificial intelligence|deep learning|nlp|llm|"
                         r"research scientist|applied scientist|computer vision|genai)\b", re.I)),
    ("data", re.compile(r"\b(data engineer|data scientist|analytics engineer|data analyst|"
                        r"business intelligence|bi )\b", re.I)),
    ("devops", re.compile(r"\b(devops|sre|site reliability|platform engineer|infrastructure engineer|"
                          r"cloud engineer)\b", re.I)),
    ("fullstack", re.compile(r"\b(full[- ]?stack|fullstack)\b", re.I)),
    ("frontend", re.compile(r"\b(front[- ]?end|frontend|ui engineer|react engineer)\b", re.I)),
    ("backend", re.compile(r"\b(back[- ]?end|backend|server[- ]side|api engineer|software engineer|"
                           r"yazılım|yazilim)\b", re.I)),
    ("pm", re.compile(r"\b(product manager|program manager|product owner|technical program)\b", re.I)),
]

_TITLE_NOISE = re.compile(
    r"\((remote|hybrid|on[- ]?site|onsite|contract|full[- ]?time|part[- ]?time|[a-z]{2,3}\s*/\s*[a-z]{2,3}|"
    r"[a-z ]*(office|based)|m/f/d|w/m/d|h/f)\)", re.I,
)
_TITLE_LOCATION_TAIL = re.compile(r"\s*[-–|,]\s*(remote|hybrid|on[- ]?site|onsite)\b.*$", re.I)
_SENIORITY_STRIP = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|staff|principal|lead|founding|distinguished|entry[- ]level|"
    r"mid[- ]level|intermediate|associate|graduate|new grad|kıdemli|kidemli)\b", re.I,
)


def detect_seniority(title: str, description: str | None = None) -> str | None:
    for label, pattern in SENIORITY_PATTERNS:
        if pattern.search(title):
            return label
    if description:
        # Only trust the description for the explicit, unambiguous markers.
        for label in ("intern", "founding"):
            pattern = dict(SENIORITY_PATTERNS)[label]
            if pattern.search(description[:2000]):
                return label
    return None


def detect_role_family(title: str) -> str:
    for label, pattern in ROLE_FAMILY_PATTERNS:
        if pattern.search(title):
            return label
    return "other"


def normalize_title(title: str) -> str:
    """Strip seniority, work mode, and location noise so the same role collides.

    "Senior Backend Engineer (Remote)" and "Backend Engineer, Remote" both become
    "backend engineer". Seniority is not lost: it is stored in its own column.
    """
    if not title:
        return ""
    text = html.unescape(title)
    text = _TITLE_NOISE.sub(" ", text)
    text = _TITLE_LOCATION_TAIL.sub("", text)
    text = _SENIORITY_STRIP.sub(" ", text)
    text = _PUNCT.sub(" ", text.lower())
    return _WS.sub(" ", text).strip()


# --- descriptions -------------------------------------------------------------

_BOILERPLATE_TAGS = ("script", "style", "nav", "footer", "noscript")
# Elements whose boundary is a paragraph break, not a word break. quality._boilerplate_ratio
# measures whole paragraphs (a blank-line-separated `\n\s*\n` split) specifically so a short
# EEO trigger phrase does not condemn an entire multi-thousand-character JD - that only works
# if block structure survives into description_text as blank lines, not as one run-on line.
_BLOCK_TAGS = (
    "p", "div", "li", "ul", "ol", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote", "header",
    "main", "aside", "figure", "hr", "dl", "dt", "dd",
)


def html_to_markdown(raw_html: str | None) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()
    md = markdownify(str(soup), heading_style="ATX", bullets="-")
    md = re.sub(r"\n{3,}", "\n\n", md)
    # House style: no em-dashes in generated output.
    md = md.replace("—", ", ").replace("–", "-")
    return md.strip()


def html_to_text(raw_html: str | None) -> str:
    """Flatten to plain text, keeping paragraph breaks as blank lines.

    `<br>` becomes a line break and every block-level element (see `_BLOCK_TAGS`)
    ends its own paragraph, so `get_text(" ")` never runs two unrelated blocks
    of prose together onto one line - the shape `_boilerplate_ratio` and the
    truncation-tail check in `quality.assess` both depend on.
    """
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):
        block.append("\n\n")
    text = soup.get_text(" ")
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    out_lines: list[str] = []
    for line in lines:
        if line:
            out_lines.append(line)
        elif out_lines and out_lines[-1] != "":
            out_lines.append("")
    return "\n".join(out_lines).strip()


def unescape_if_escaped(value: str | None) -> str:
    """Greenhouse serves HTML that is itself HTML-escaped. Unescape only when it is.

    Verified 2026-08-20: `content` arrives as `&lt;h2&gt;...`. Unescaping real HTML
    would be harmless here but unescaping twice can corrupt literal entities, so
    the check is explicit rather than unconditional.
    """
    if not value:
        return ""
    if "&lt;" in value and "<" not in value[:200]:
        return html.unescape(value)
    return value


def completeness(text: str | None) -> str:
    """PLAN.md section 9.5. Listing calls that carry the whole body are 'full'."""
    if not text:
        return "none"
    return "full" if len(text) >= 400 else "snippet"


def content_hash(title: str, location: str | None, description_text: str | None) -> str:
    """Exact hash for change detection. PLAN.md section 6."""
    payload = "\x00".join([
        _WS.sub(" ", (title or "").strip().lower()),
        _WS.sub(" ", (location or "").strip().lower()),
        _WS.sub(" ", (description_text or "").strip().lower()),
    ])
    return hashlib.sha256(payload.encode()).hexdigest()


# --- locations ----------------------------------------------------------------

_COUNTRY_ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "united states of america": "US", "us": "US", "america": "US",
    "uk": "GB", "united kingdom": "GB", "england": "GB", "great britain": "GB",
    "turkey": "TR", "türkiye": "TR", "turkiye": "TR", "tr": "TR",
    "germany": "DE", "deutschland": "DE", "netherlands": "NL", "the netherlands": "NL",
    "france": "FR", "spain": "ES", "portugal": "PT", "poland": "PL", "ireland": "IE",
    "canada": "CA", "india": "IN", "australia": "AU", "singapore": "SG",
    "switzerland": "CH", "sweden": "SE", "denmark": "DK", "norway": "NO",
    "finland": "FI", "italy": "IT", "belgium": "BE", "austria": "AT",
    "israel": "IL", "japan": "JP", "brazil": "BR", "mexico": "MX",
    "united arab emirates": "AE", "uae": "AE",
    "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "china": "CN", "hong kong": "HK", "taiwan": "TW",
    "new zealand": "NZ", "south africa": "ZA", "nigeria": "NG", "kenya": "KE",
    "egypt": "EG", "saudi arabia": "SA", "qatar": "QA",
    "argentina": "AR", "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY",
    "czech republic": "CZ", "czechia": "CZ", "romania": "RO", "bulgaria": "BG",
    "hungary": "HU", "greece": "GR", "croatia": "HR", "serbia": "RS",
    "ukraine": "UA", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
    "slovakia": "SK", "slovenia": "SI", "iceland": "IS", "luxembourg": "LU",
    "malta": "MT", "cyprus": "CY", "indonesia": "ID", "philippines": "PH",
    "vietnam": "VN", "thailand": "TH", "malaysia": "MY", "pakistan": "PK",
}

# Full state names, because a posting that says "Colorado" or "Texas" is a US
# posting and the timezone rule has to see that. Found live: WWR spells the
# location as a bare state name, and those were passing a GMT+3 overlap check
# because the country came back unknown.
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "washington dc", "district of columbia",
}
# "Georgia" is also a country, and "Washington" is also a city. Neither is worth
# a wrong answer, so both stay out of the name set used to infer the US.
_AMBIGUOUS_STATE_NAMES = {"georgia", "washington"}
_US_STATE_NAMES -= _AMBIGUOUS_STATE_NAMES

_US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

_REMOTE_RE = re.compile(r"\b(remote|distributed|anywhere|work from home|wfh)\b", re.I)
_HYBRID_RE = re.compile(r"\bhybrid\b", re.I)


def parse_location(location_raw: str | None) -> tuple[str | None, str | None, str]:
    """Return (country_code, city, remote_type) from a free-text location string.

    Deliberately conservative: an unrecognised location yields None rather than a
    guess. A wrong country silently drops a job from the tr_local filter.
    """
    if not location_raw:
        return None, None, "unknown"
    text = location_raw.strip()
    remote_type = "unknown"
    if _HYBRID_RE.search(text):
        remote_type = "hybrid"
    elif _REMOTE_RE.search(text):
        remote_type = "remote"

    # Work mode often rides along in a parenthetical: "Berlin, Germany (Hybrid)".
    # But so does the actual country: "Remote (United States)". Strip only the
    # parentheticals that carry no place name, or the timezone filter loses the
    # one signal that removes US-only remote listings.
    stripped = re.sub(r"\(([^)]*)\)", _keep_place, text)
    # A spaced hyphen is a separator ("Remote - California"); a hyphen inside a
    # word is part of the name ("Saint-Denis"), so it is not one.
    parts = [p.strip() for p in re.split(r"[,/|]|\s+-\s+|\bor\b", stripped) if p.strip()]
    country: str | None = None
    city: str | None = None
    for part in reversed(parts):
        key = part.lower().strip(". ")
        if key in _COUNTRY_ALIASES:
            country = _COUNTRY_ALIASES[key]
            break
        if (len(key) == 2 and key in _US_STATES) or key in _US_STATE_NAMES:
            country = "US"
            break
    if country is None:
        country = _country_from_tokens(parts)
    for part in parts:
        key = part.lower().strip(". ")
        if key in _COUNTRY_ALIASES or _REMOTE_RE.match(key) or _HYBRID_RE.match(key):
            continue
        if len(part) > 1:
            city = part
            break
    return country, city, remote_type


# Words that are a country name and also something else. Scanning tokens for
# these would turn "Georgia Tech" or "Jordan Street" into a country.
_AMBIGUOUS_COUNTRY_TOKENS = frozenset({"georgia", "jordan", "chad", "guinea", "mali", "niger"})


def _country_from_tokens(parts: list[str]) -> str | None:
    """Last resort: a country name buried inside a compound part.

    Live examples this exists for: "US Remote National" and "NYC-Privy,
    US-Remote", both of which are US postings that resolved to no country and so
    skipped the timezone rule entirely.
    """
    for part in parts:
        for token in re.split(r"[^A-Za-z]+", part.lower()):
            if len(token) < 2 or token in _AMBIGUOUS_COUNTRY_TOKENS:
                continue
            if token in _COUNTRY_ALIASES:
                return _COUNTRY_ALIASES[token]
            if token in _US_STATE_NAMES:
                return "US"
    return None


def _keep_place(match: re.Match[str]) -> str:
    """Replacement for a parenthetical: keep it when it names a place."""
    inner = match.group(1).strip()
    for piece in re.split(r"[,/|]", inner):
        key = piece.strip().lower().strip(". ")
        if key in _COUNTRY_ALIASES or (len(key) == 2 and key in _US_STATES) or key in _US_STATE_NAMES:
            return f", {inner}"
    return " "


def country_code(name: str | None) -> str | None:
    """Map a country name or code to an ISO-2 code, or None when unrecognised."""
    if not name:
        return None
    key = name.strip().lower().strip(".")
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    return name.strip().upper() if len(name.strip()) == 2 else None


def parse_datetime(value: object) -> dt.datetime | None:
    """Parse whatever a source calls a timestamp, into naive UTC."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            seconds = float(value)
            # Lever and Himalayas both send epochs, but Lever's are milliseconds.
            # 1e11 seconds is the year 5138, so anything above it is not seconds.
            if abs(seconds) > 1e11:
                seconds /= 1000.0
            parsed = dt.datetime.fromtimestamp(seconds, dt.UTC)
        else:
            parsed = date_parser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.UTC).replace(tzinfo=None)
    return parsed


# --- employment type ----------------------------------------------------------

# Every source spells this differently, and several send a list or two separate
# fields. Values below are the ones observed live on 2026-08-20, not guesses:
# Ashby "FullTime", Lever "Contract", Recruitee "fulltime_permanent", Personio
# "permanent" plus schedule "full-time", SmartRecruiters and Workable "Full-time",
# Remotive "full_time", Jobicy ["Full-Time"], We Work Remotely "Full-Time".
EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship", "temporary")

_EMPLOYMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Checked before full-time, because "internship, full time" is an internship.
    (
        "internship",
        re.compile(r"intern|stag|stajyer|trainee|apprentic|working.?student|werkstudent", re.I),
    ),
    ("part_time", re.compile(r"part.?time|parttime|yari.?zamanli|yarı.?zamanlı|teilzeit", re.I)),
    ("contract", re.compile(r"contract|freelance|consultan|b2b|sozlesme|sözleşme|contractor", re.I)),
    ("temporary", re.compile(r"tempor|seasonal|interim|fixed.?term|gecici|geçici", re.I)),
    (
        "full_time",
        re.compile(r"full.?time|fulltime|permanent|vollzeit|tam.?zamanli|tam.?zamanlı|regular", re.I),
    ),
]


def normalize_employment_type(*values: object) -> str | None:
    """First recognisable employment type among the values given, else None.

    Takes several values because sources split the signal: Personio states
    `employmentType` "permanent" and `schedule` "full-time", and neither alone is
    the whole answer. Lists are flattened, which covers Jobicy and Arbeitnow.

    Returns None rather than a default. An unstated type must stay unknown, or the
    filter would drop jobs for a value nobody published.
    """
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, list | tuple | set):
            parts.extend(str(item) for item in value if item)
        else:
            parts.append(str(value))
    text = " ".join(part for part in parts if part.strip())
    if not text.strip():
        return None
    for label, pattern in _EMPLOYMENT_PATTERNS:
        if pattern.search(text):
            return label
    return None
