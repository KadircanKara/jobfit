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
import unicodedata
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

# The company name an Upwork posting gets when nothing here recovers a real one.
# It lives in this module, not in the adapter, because `store.upsert_posting`
# has to recognise it to keep a real extracted company from being overwritten by
# it - and importing the adapter from the store would be a cycle (the adapter's
# budget imports the store) as well as dragging httpx into a database module.
COMPANY_NAME_PLACEHOLDER = "Upwork client"

# Domains a job description legitimately mentions that are never the client:
# collaboration tools, code hosts, meeting software, and the platform itself.
# A naive domain regex returns one of these for a large fraction of the
# corpus (github.com and docs.google.com are named constantly - as the repo
# to work in or the doc to read, never as the client), so this list is the
# difference between the extractor being useful and being actively harmful.
#
# Several of these brands (monday, medium, notion, apple, asana, loom, and
# among the deployment and no-code entries make, render, railway, bubble, fly)
# are also common English words, which means a real client legitimately named
# "Apple Valley Farms" or "Monday Morning Studio" will be silently rejected
# by the word-level check below (`_is_noise_name`). That trade is deliberate,
# not an oversight: a miss here costs one job no outreach; a false positive
# sends a real LinkedIn message to a stranger at Apple. Do not narrow this
# list to recover those names without weighing that trade again.
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
    # Where the work gets deployed, stored, or sent from. These are what an
    # Upwork gig actually names - "deploys to vercel.com", "our data is in
    # supabase" - and a domain outranks every other signal here, so without
    # them "Vercel" becomes the client. Worse than a bad name: `store.
    # get_or_create_company` dedupes on domain, so the gig merges into the real
    # Vercel company row and takes its contacts and history with it.
    "vercel.com",
    "vercel.app",
    "netlify.com",
    "netlify.app",
    "supabase.com",
    "firebase.com",
    "heroku.com",
    "herokuapp.com",
    "railway.app",
    "render.com",
    "fly.io",
    "replit.com",
    "cloudflare.com",
    "digitalocean.com",
    "godaddy.com",
    "hostinger.com",
    "bluehost.com",
    "github.io",
    "mongodb.com",
    # Site builders and storefronts: the platform the client's site sits on,
    # never the client.
    "wix.com",
    "squarespace.com",
    "webflow.com",
    "framer.com",
    "bubble.io",
    "contentful.com",
    "strapi.io",
    "woocommerce.com",
    "bigcommerce.com",
    "canva.com",
    # Messaging, email, payments and support: the integration to build against.
    "twilio.com",
    "sendgrid.com",
    "mailchimp.com",
    "klaviyo.com",
    "intercom.com",
    "zendesk.com",
    "discord.com",
    "telegram.org",
    "whatsapp.com",
    "paypal.com",
    "typeform.com",
    # Automation and no-code, the single most-named category in an AI-automation
    # gig ("automation expert (Make.com)" is a title, not a client).
    "zapier.com",
    "make.com",
    "n8n.io",
    "retool.com",
    # Models, vector stores and AI infrastructure - the stack the gig asks for.
    "huggingface.co",
    "pinecone.io",
    "weaviate.io",
    "qdrant.tech",
    "langchain.com",
    "openrouter.ai",
    "replicate.com",
    "perplexity.ai",
    "elevenlabs.io",
    "claude.ai",
    "midjourney.com",
    # Rival marketplaces and job boards, named to compare rates or to say where
    # not to be contacted.
    "fiverr.com",
    "freelancer.com",
    "toptal.com",
    "indeed.com",
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
# The lead-in words are matched case-insensitively with an inline `(?i:...)`
# group, the same way `_PERSON_PATTERNS` scopes "this is" below. Without it
# these patterns only ever fired mid-sentence: "We're Acme" and "Our company,
# Acme," - which is how a posting actually opens - never matched at all, while
# "we're Acme" did. The capture itself stays case-sensitive on purpose, so the
# `[A-Z]` anchor still separates a name from ordinary prose and the captured
# group keeps the case the client wrote.
_COMPANY_PATTERNS = [
    re.compile(r"(?i:\bproduct called )([A-Z][\w.&' -]{1,40}?)\b[.,]"),
    re.compile(r"(?i:\bour (?:company|startup|business),?\s+)([A-Z][\w.&' -]{1,40}?),"),
    re.compile(r"(?i:\bwe['’]re\s+)([A-Z][\w.&' -]{1,40}?)[,.]"),
    re.compile(_SENTENCE_START + r"([A-Z][\w.&' -]{1,40}?)\s+(?i:is looking for)\b"),
    re.compile(_SENTENCE_START + r"([A-Z][\w.&' -]{1,40}?)\s+(?i:is hiring)\b"),
]

# The thing the client says they want built. Weaker than every pattern above,
# and read only when they all come back empty: an anonymous client who names
# nothing else will still name the product, and on a corpus where the company
# is recovered for roughly one posting in six, that sentence is often the only
# lead there is. Weak because a product that does not exist yet frequently has
# no company behind it - which is why this feeds the reviewed candidate list in
# the drawer and never an address to send to.
_PRODUCT_PATTERNS = [
    re.compile(r"(?i:\b(?:looking to build|we are building|we['’]re building)\s+"
               r"(?:the\s+)?)([A-Z][\w.&' -]{1,40}?)\s*[,.]"),
]

# Words that describe what a thing *is*, not what it is called. A product name
# is written as "<name> <what it does>" - "Nexora AI Operations Hub", "Aurora
# Data Platform" - and only the leading part is what anyone puts in a LinkedIn
# headline. Measured against the live search: the full "Nexora AI Operations
# Hub" returned nobody at Nexora, "Nexora AI" two people, and "Nexora" four.
_PRODUCT_NOUNS = frozenset({
    "ai", "ml", "api", "app", "application", "assistant", "automation", "bot",
    "cloud", "crm", "dashboard", "data", "engine", "erp", "gateway", "hub",
    "intelligence", "manager", "operations", "ops", "pipeline", "platform",
    "portal", "saas", "service", "services", "software", "solution",
    "solutions", "suite", "system", "systems", "tool", "toolkit", "workflow",
})


def _trim_product_name(name: str) -> str | None:
    """The leading proper part of a product name, or None if there is none.

    Everything from the first descriptive word onward is dropped, because that
    tail describes the thing rather than naming it and no employee's headline
    carries it. At least one word must survive, and it must not itself be a
    descriptive word - "Platform Hub" names nothing and searching for it would
    return whoever LinkedIn thinks "Platform" means.
    """
    kept: list[str] = []
    for word in name.split():
        if word.strip(".,&'-").lower() in _PRODUCT_NOUNS:
            break
        kept.append(word)
    return " ".join(kept) if kept else None


# Someone introducing themselves by name and, usually, their company. "This
# is" is scoped case-insensitive with an inline group (`(?i:...)`) rather
# than flagging the whole pattern - a sentence genuinely starting "This is
# Alex..." is exactly as valid a signal as "this is Alex...", but the name
# captures themselves must stay capitalised or the pattern would also fire on
# unrelated lowercase prose.
_PERSON_PATTERNS = [
    re.compile(
        r"(?i:\bI['’]m\s+)([A-Z][a-z]+),?\s+(?i:(?:the\s+)?founder of\s+)([A-Z][\w.&' -]{1,40}?)[.,]"
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
    """Whatever could be recovered about the client. Any field may be None.

    No `person_name`: the person patterns are still read, but only for the
    company half of "this is Alex from Northquill". The outreach message
    greets the LinkedIn contact it is actually being sent to, by that
    contact's own name (`drafts._first_name`) - who need not be, and usually
    is not, whoever typed the posting - so a name recovered here has no
    consumer, and a returned field nothing reads is a field a later editor
    will wire into a greeting believing it was meant for one.
    """

    domain: str | None
    company_name: str | None


_NOTHING = Identity(domain=None, company_name=None)


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
    _, person_company = _find_person(window)

    if person_company and not company_name:
        company_name = person_company
    if not company_name:
        # Last, and only when nothing stated a company: a product name is a
        # lead, not an assertion about who the client is.
        company_name = _find_product(window)

    if domain is None and company_name is None:
        return _NOTHING

    return Identity(domain=domain, company_name=company_name)


def _find_product(window: str) -> str | None:
    """A product the client says they want built, trimmed to its name."""
    for pattern in _PRODUCT_PATTERNS:
        for match in pattern.finditer(window):
            captured = _clean_name(match.group(1))
            if captured is None:
                continue
            trimmed = _trim_product_name(captured)
            if trimmed is None or _is_noise_name(trimmed):
                continue
            return trimmed
    return None


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
#
# "x" (from x.com) is dropped on purpose: kept, it would reject any company
# name containing a standalone "X" token ("X Corp", "Acme X"), which is a
# far more likely real name than a stray mention of x.com typed as bare "X".
# The domain itself is still caught separately by `_is_noise_domain`.
_NOISE_BRANDS = frozenset(_brand(host) for host in _NOISE_DOMAINS) - {"x"}


def _tokenize(name: str) -> list[str]:
    """Split a captured name into brand-comparable tokens.

    Splitting on a run of non-alphanumeric characters, rather than deleting
    them in place, is what makes "Zoom-Labs" tokenize to ["zoom", "labs"]
    instead of collapsing to the single non-matching token "zoomlabs" - the
    same fix separates a possessive ("Zoom's team" -> ["zoom", "s", "team"])
    and a slash-joined pair. NFKC normalisation is applied first, which folds
    a handful of Unicode compatibility variants (e.g. full-width Latin
    letters) to their ordinary form; it does NOT defend against a genuine
    homoglyph attack (a Cyrillic "о" in place of a Latin "o" has no NFKC
    mapping to the Latin letter, since they are different letters in
    different scripts, not the same letter in two forms) - that class of
    spoofing is a known, accepted gap, not something this module claims to
    close.
    """
    normalized = unicodedata.normalize("NFKC", name).lower()
    return [token for token in re.split(r"[^a-z0-9]+", normalized) if token]


def _is_noise_name(name: str) -> bool:
    """True if ANY token in the name is a noise brand, not just the whole name.

    A naive whole-string check lets "Zoom Inc", "The Zoom team" and "Slack
    app" straight through, because none of those strings equals "zoom" or
    "slack" exactly - but each of them is still naming the noise entity plus
    one qualifier word. Checking token-by-token closes that gap without
    rejecting real multi-word names: "Karma and Luck", "Booz Allen Hamilton"
    and "Zoomer Labs" all survive, since none of their tokens - "zoomer" is
    not "zoom" - is itself a noise brand.

    Tokenising on punctuation is what makes a hyphenated name whose second half
    is a brand word - "Heir-loom Goods" splitting to ["heir", "loom", "goods"] -
    rejected outright, and that is the deliberate fail-closed side of the same
    rule that catches "Zoom-Labs".
    """
    return any(token in _NOISE_BRANDS for token in _tokenize(name))


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
    # "www" is never anyone's name. Dropped before the fallback below picks the
    # first label, which is what turned "www.cb-holistictherapy.ie" into the
    # company "Www" on a live run.
    if len(labels) > 1 and labels[0].lower() == "www":
        labels = labels[1:]
    core = labels[-2] if len(labels) >= 2 and labels[-1] in _KNOWN_TLDS else labels[0]
    words = re.split(r"[-_]+", core)
    return " ".join(word.capitalize() for word in words if word) or domain
