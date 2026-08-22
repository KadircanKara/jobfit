"""Which slices of each aggregator are worth fetching, decided by evidence.

A provider's categories are not equally worth a request. The question "is
Full-Stack Programming worth a daily fetch" has a factual answer, and it is
already on disk: the raw payloads name their own category on every posting, so
the number of postings with that label whose title matched can simply be
counted. No similarity function, because the information that separates
"Back-End Programming" (where the AI roles are) from "Front-End Programming"
(where they are not) is nowhere in either string.

Nothing here writes a proposal down. `due_boards` fetches every board that is
not dead, so a stored proposal would be fetched before anyone approved it.
Proposals are recomputed from the payloads each time they are asked for.
"""
from __future__ import annotations

import collections
import dataclasses
import html
import json
import pathlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator

from sqlalchemy import select

from jobhunt import store
from jobhunt.config import Config
from jobhunt.db.models import Board
from jobhunt.db.session import session_scope
from jobhunt.preferences import Preferences, title_patterns

# A fortnight of runs. Long enough that a category is judged on more than one
# day's postings, short enough that one which stopped producing drifts off the
# list on its own instead of being retired by hand.
DEFAULT_RUNS = 14


@dataclasses.dataclass(frozen=True)
class Posting:
    """One posting as the estimator needs it: identity, labels, title."""

    posting_id: str
    labels: tuple[str, ...]
    title: str


@dataclasses.dataclass(frozen=True)
class Extractor:
    """The two provider-specific pieces: reading labels, and naming a token."""

    read: Callable[[str], Iterator[Posting]]
    token_for: Callable[[str], str | None]


def _wwr_postings(payload: str) -> Iterator[Posting]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return
    for item in root.findall("./channel/item"):
        def text(tag: str, item: ET.Element = item) -> str:
            node = item.find(tag)
            return (node.text or "").strip() if node is not None and node.text else ""

        link, raw_title = text("link"), text("title")
        if not link or not raw_title:
            continue
        # "Company: Position", the same split wwr.py:_split_title makes. Only
        # the position half is a job title, and matching the company half would
        # count "Stripe" as a title match.
        _, _, title = raw_title.partition(":")
        category = text("category")
        yield Posting(link, (category,) if category else (), (title.strip() or raw_title))


# The literal kebab form is the default and is correct for most names:
# "Management and Finance" -> remote-management-and-finance-jobs and
# "Sales and Marketing" -> remote-sales-and-marketing-jobs both verified 200
# live with the connective kept, so connectives are never stripped generally.
#
# These are measured exceptions, not derived ones: each value below was
# checked against the live feed by hand, not guessed from a stripping rule.
# Do not "simplify" this back into connective-stripping - that breaks the
# names above, which need the connective kept.
#   - "DevOps and Sysadmin": the literal kebab is a dead 301; the real slug
#     drops "and" instead.
#   - "All Other Remote": no variant resolves; there is no feed for it.
_WWR_MEASURED_TOKENS: dict[str, str | None] = {
    "devops and sysadmin": "remote-devops-sysadmin-jobs",
    "all other remote": None,
}


def _wwr_token(name: str) -> str | None:
    key = name.strip().lower()
    if key in _WWR_MEASURED_TOKENS:
        return _WWR_MEASURED_TOKENS[key]
    words = re.sub(r"[^a-z0-9]+", " ", name.lower().replace("&", " and ")).split()
    if not words:
        return None
    return "remote-" + "-".join(words) + "-jobs"


def _clean_label(value: str) -> str:
    """One provider-stated label, as text rather than as markup.

    Jobicy serves HTML entities inside `jobIndustry` - 13 of its 19 real
    values contain `&amp;`, e.g. "Customer Support &amp; Success". The label
    becomes both the display string and the fetch token, and jobicy.py builds
    `&industry={token}` by concatenation, so an un-decoded `&amp;` would end
    the query parameter early: the fetch would silently return the whole
    un-narrowed feed, look healthy to `_record_board_outcome`, and duplicate
    the `all` feed every day. Decoding at extraction keeps the entity out of
    the token, the URL and the screen at once.
    """
    return html.unescape(value).strip()


def _json_postings(
    payload: str | dict | list,
    *,
    rows: str | None,
    id_key: str,
    title_key: str,
    label_keys: tuple[str, ...],
) -> Iterator[Posting]:
    """Shared shape for the three JSON aggregators.

    `payload` arrives pre-decoded from `sync.py` for these three providers
    (their `fetch()` returns parsed JSON, unlike wwr's XML text), but a JSON
    string is still accepted so a caller that has its own raw text does not
    need to decode it first.
    """
    if isinstance(payload, str):
        try:
            document = json.loads(payload)
        except json.JSONDecodeError:
            return
    else:
        document = payload
    items = document.get(rows, []) if rows and isinstance(document, dict) else document
    if not isinstance(items, list):
        return
    for row in items:
        if not isinstance(row, dict):
            continue
        identity, title = row.get(id_key), row.get(title_key)
        if identity is None or not title:
            continue
        labels: list[str] = []
        for key in label_keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(_clean_label(value))
            elif isinstance(value, list):
                labels.extend(_clean_label(str(v)) for v in value if str(v).strip())
        yield Posting(str(identity), tuple(labels), str(title))


VOCABULARY: dict[str, Extractor] = {
    "wwr": Extractor(_wwr_postings, _wwr_token),
    "remotive": Extractor(
        lambda p: _json_postings(
            p, rows="jobs", id_key="id", title_key="title", label_keys=("category",)
        ),
        # The observed values are display names. Whether ?category= wants the
        # display name or a slug is untested, so the display name is sent and a
        # wrong guess dies on its first fetch like any other candidate.
        lambda name: name.strip() or None,
    ),
    "jobicy": Extractor(
        lambda p: _json_postings(
            p, rows="jobs", id_key="id", title_key="jobTitle", label_keys=("jobIndustry",)
        ),
        lambda name: name.strip() or None,
    ),
    "remoteok": Extractor(
        lambda p: _json_postings(
            p, rows=None, id_key="id", title_key="position", label_keys=("tags",)
        ),
        lambda name: name.strip() or None,
    ),
    # arbeitnow is absent on purpose: its fetch ignores `token` entirely
    # (arbeitnow.py:29), so there is no slice of it that could be proposed.
}


def _run_directories(config: Config, provider: str, runs: int) -> list[pathlib.Path]:
    folder = pathlib.Path(str(config.raw_dir)) / provider
    if not folder.is_dir():
        return []
    return sorted((d for d in folder.iterdir() if d.is_dir()), reverse=True)[:runs]


def observe(config: Config, provider: str, runs: int = DEFAULT_RUNS) -> list[Posting]:
    """Distinct postings for one provider, newest `runs` stored runs.

    Deduplicated by the provider's own id, so a posting seen on three days is
    one posting and a long-lived listing does not outvote a busy category.
    """
    extractor = VOCABULARY.get(provider)
    if extractor is None:
        return []
    seen: dict[str, Posting] = {}
    for directory in _run_directories(config, provider, runs):
        for path in sorted(directory.glob("*.json")):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(envelope, dict):
                continue
            payload = envelope.get("payload")
            # Adapters disagree on what `fetch()` hands back: wwr returns raw
            # XML text (a str), while the three JSON aggregators return
            # already-parsed objects (dict or list). sync.py stores whatever
            # `fetch()` gave it, unmodified, so both shapes show up for real
            # on disk and both must be accepted here.
            if not isinstance(payload, (str, dict, list)):
                continue
            for posting in extractor.read(payload):
                seen.setdefault(posting.posting_id, posting)
    return list(seen.values())


@dataclasses.dataclass(frozen=True)
class Proposal:
    """One category, with the evidence for fetching it.

    `matched` is the number the ordering uses and the number the panel leads
    with. `sample` is shown beside it because a 100% rate over four postings
    should not read like a 40% rate over four hundred.
    """

    provider: str
    category: str
    token: str | None
    matched: int
    sample: int
    registered: bool


# Approving and retiring both end in a row the sync loop may find dead, so the
# note is what tells the two apart afterwards. An approved feed stays on the
# panel whatever its status - a dead one is exactly the outcome the user is
# owed, since this feature ships no prober and the panel is the only place a
# failed guess is ever reported. A retired feed is a decision already made, so
# it leaves the panel and returns to the proposals it came from.
APPROVED_NOTE = "approved category feed"
RETIRED_NOTE = "retired category feed"


def _registered_tokens(config: Config) -> set[tuple[str, str]]:
    """Pairs that must not be offered as proposals again.

    A live board is obviously one. So is an approved category feed that has
    since died: it has been tried, and the approved list reports its outcome,
    so re-offering it would invite the user to spend the same request again
    and again with nothing new to learn. A retired feed is deliberately not
    one - the user turned it off, and it belongs back among the proposals so
    it can be turned on again.
    """
    with session_scope(config.db_path) as session:
        rows = session.execute(
            select(Board.provider, Board.token).where(
                (Board.status != "dead") | (Board.notes == APPROVED_NOTE)
            )
        ).all()
    return {(provider, token) for provider, token in rows}


def propose(
    config: Config, prefs: Preferences, runs: int = DEFAULT_RUNS
) -> list[Proposal]:
    """Every category with evidence behind it, best first. Writes nothing.

    Ordered by absolute matched postings, never by rate. Rate is misleading
    here: a small category can match everything in it and still be worth less
    than a large one that matches a tenth of itself.
    """
    patterns = [re.compile(p) for p in title_patterns(prefs.titles)]
    if not patterns:
        return []

    registered = _registered_tokens(config)
    proposals: list[Proposal] = []
    for provider, extractor in VOCABULARY.items():
        matched: collections.Counter[str] = collections.Counter()
        sample: collections.Counter[str] = collections.Counter()
        for posting in observe(config, provider, runs):
            hit = any(p.search(posting.title) for p in patterns)
            for label in posting.labels:
                sample[label] += 1
                if hit:
                    matched[label] += 1
        for label, count in matched.items():
            if not count:
                continue
            token = extractor.token_for(label)
            proposals.append(
                Proposal(
                    provider=provider,
                    category=label,
                    token=token,
                    matched=count,
                    sample=sample[label],
                    registered=bool(token) and (provider, token) in registered,
                )
            )
    proposals.sort(key=lambda p: (-p.matched, -p.sample, p.provider, p.category))
    return proposals


# The same marker `feeds.seed` uses (discovery/feeds.py:35). An approved
# category is a tier 2 aggregator feed that happened to be chosen in the
# browser rather than shipped in the seed list, and nothing downstream should
# have to care which.
DISCOVERED_VIA = "feed"
MARKET = "global_remote"



def approve(config: Config, selections: list[tuple[str, str]]) -> int:
    """Register `(provider, token)` pairs as boards. Idempotent.

    The board is created as a candidate, which is the honest description: the
    token is derived from a category name and may be wrong. `record_fetch_failures`
    (sync.py:274) kills a candidate on its first failed fetch, so a bad guess
    costs exactly one request and then reports itself in the panel.
    """
    changed = 0
    with session_scope(config.db_path) as session:
        for provider, token in selections:
            if provider not in VOCABULARY or not token:
                continue
            board = store.get_or_create_board(
                session, provider, token, DISCOVERED_VIA, MARKET
            )
            board.notes = APPROVED_NOTE
            if board.status == "dead":
                # Re-approving something previously retired puts it back in the
                # running rather than leaving a dead row that can never revive.
                board.status = "candidate"
                board.consecutive_errors = 0
            changed += 1
    return changed


def retire(config: Config, selections: list[tuple[str, str]]) -> int:
    """Stop fetching these, without deleting the history of having had them."""
    changed = 0
    with session_scope(config.db_path) as session:
        for provider, token in selections:
            board = session.scalars(
                select(Board).where(Board.provider == provider, Board.token == token)
            ).first()
            if board is None or board.notes == RETIRED_NOTE:
                continue
            # Dead stops the sync loop fetching it; the note records that the
            # user stopped it rather than the feed failing, so the panel does
            # not report a deliberate choice as "did not resolve".
            board.status = "dead"
            board.notes = RETIRED_NOTE
            changed += 1
    return changed
