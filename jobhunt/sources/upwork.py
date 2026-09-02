"""Upwork job search, via the Upwork MCP.

`fetch` shells out to `claude -p` with only `find_jobs` on the tool
allow-list - `manage_proposals`, `confirm_draft`, `send_message` and
`save_job` never appear anywhere near that allow-list, in this module or the
prompt it renders, because applying to a listing spends Connects (a currency
this account holds ten of, against a typical cost of 23) while searching and
reading cost nothing. That allow-list, not a comment or a convention, is what
makes an adapter bug in this file merely wasteful rather than expensive - see
`jobhunt/web/gate.py`'s `--allowedTools ""` for the same argument made once
already. `normalize` is the pure half: it turns a stored fetch envelope into
`JobPosting` rows and never touches the network.

Facts below were verified live against the Upwork MCP, not re-derived from
documentation. A `find_jobs action=search` result carries a *truncated*
`description_snippet` alongside its metadata (`job_type`, `budget`,
`experience_level`, `duration`, `engagement`, `proposal_count`, `skills`,
`client`, ...), all top-level and snake_case, with `job_type` lowercase
(`"hourly"`/`"fixed"`). The full description lives only in the per-job `get`
document, nested under `data.marketplaceJobPosting`:

- `data.marketplaceJobPosting.content.description` is the full description,
  wrapped in `<untrusted_participant_content>` tags.
- `data.marketplaceJobPosting.contractTerms.hourlyContractTerms.hourlyBudgetMin`/
  `hourlyBudgetMax` (plain numbers) is an hourly row's real rate. The
  search-level `budget` is the string `"0.0"` on every hourly row regardless
  of the real rate - zero is never a real budget for either contract type, so
  it is treated as absent rather than free work. A fixed row's real number is
  the search-level `budget` directly; no detail is needed for it.
- `data.marketplaceJobPosting.clientCompanyPublic` carries `country`, `state`
  and `timezone` - and no company name. That is why `company_name` falls back
  to a constant here; Task 8 extracts a real identity from the description
  text. It appears only in the detail document, never on a search result.

Upwork's `experience_level` (`ENTRY_LEVEL`/`INTERMEDIATE`/`EXPERT`) rates the
*contract's* difficulty, not the freelancer's career stage, so it is never
mapped onto our seniority ladder - it only ever reaches the description
trailer, as plain text for a human or the gate to read, never a structured
field this codebase could accidentally filter on.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import re
import subprocess
from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt import preferences as preferences_module
from jobhunt.config import Config
from jobhunt.db.models import utcnow
from jobhunt.pipeline import normalize as norm
from jobhunt.sources import upwork_query as query
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting
from jobhunt.sources.upwork_guard import FetchBudget
from jobhunt.web import agent

log = logging.getLogger(__name__)

_UNTRUSTED_OPEN = "<untrusted_participant_content>"
_UNTRUSTED_CLOSE = "</untrusted_participant_content>"

COMPANY_NAME_PLACEHOLDER = "Upwork client"

# The only tool `fetch` ever puts on an allow-list. See the module docstring -
# this is the enforcement, not a comment beside it. A single tool name, not a
# list: `agent.run`'s `tools` is the literal `--allowedTools` value, and a
# comma-joined string of one item is indistinguishable from this anyway.
ALLOWED_TOOLS = "mcp__upwork__upwork__find_jobs"

# Verified live against this account (see upwork_query.py's module docstring
# for the same discipline). UPWORK_ORG_UID overrides it without a code change
# if this personal tool is ever pointed at a different Upwork account.
_DEFAULT_ORG_UID = "1808166514497822721"

# How many `action=get` calls one fetch may make. Each is its own read inside
# the same agent turn, not a separate Connect-spending action - the cap exists
# to keep one turn's tool calls bounded, the same reasoning as PAGE_SIZE in
# upwork_query.py, not to protect anything scarce on Upwork's side.
DETAIL_BUDGET = 10

# Well under agent.DEFAULT_TIMEOUT (1800s): a hung MCP call must not block a
# nightly run for half an hour, twice over. Worst case per ref is now two
# attempts at this timeout each - 600s total - since a hang on the first
# attempt still pays for a retry rather than escaping the loop early.
FETCH_TIMEOUT_SECONDS = 300.0

_PROMPT_RELATIVE = pathlib.Path("prompts/upwork_fetch.md")
_PACKAGED_PROMPT = pathlib.Path(__file__).resolve().parent.parent / "assets" / _PROMPT_RELATIVE

# The five placeholders `upwork_fetch.md` declares, matched in one pass so
# that a value being substituted in - a preference string, a rendered call
# blob - can never itself contain a literal "{cutoff}" or "{known_ids}" that
# a later, separate .replace() call would then corrupt. Named "{call}", not
# "{params}": it renders the *whole* find_jobs call object (action, org_uid,
# and params together) - the name "{params}" is what let org_uid get nested
# one level too deep in the first place, and a future editor re-adding a
# top-level key belongs less to a name that already implies "just the filters".
_PLACEHOLDER = re.compile(r"\{call\}|\{cutoff\}|\{max_pages\}|\{known_ids\}|\{detail_budget\}")

# Mirrors gate.ARRAY: a fenced or bare object is read the same way a fenced or
# bare array is, because a model asked for "one JSON object" reliably wraps it
# in prose or a code fence anyway.
_OBJECT = re.compile(r"\{.*\}", re.S)


def _strip_untrusted_wrapper(text: str) -> str:
    """Drop the outermost `<untrusted_participant_content>` tags, keep the text.

    This is storage hygiene, not a safety boundary: the stripped text is what
    the CV tailor, the browser, and the quality gate all end up reading, and a
    literal wrapper tag in the middle of a job description would just be noise
    to a human or a downstream model. It does nothing to make that text safe.
    The description is still anonymous free text written by a stranger, it
    still reaches the LLM gate's prompt as plain text after this, and the gate
    remains a real prompt-injection surface - stripping the tag here does not
    close it, and nothing in this module tries to.
    """
    stripped = text.strip()
    if stripped.startswith(_UNTRUSTED_OPEN) and stripped.endswith(_UNTRUSTED_CLOSE):
        stripped = stripped[len(_UNTRUSTED_OPEN) : -len(_UNTRUSTED_CLOSE)]
    return stripped.strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _positive_float(value: Any) -> float | None:
    """`value` as a float, or None if it is missing, unparseable, or <= 0.

    Both the hourly detail's `hourlyBudgetMin`/`Max` and the search-level
    `budget` string need this same guard: a stated 0 is never a real rate on
    either path, and a `hourlyBudgetMin: 0` beside a real max must not report
    a $0 floor.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_str(value: Any) -> str:
    """`value` if it is a string, else "". Same discipline as `_as_dict`/`_as_list`:
    a source that sends the wrong type for a field degrades to absent, never
    a crash that drops the whole posting via the caller's try/except."""
    return value if isinstance(value, str) else ""


def _detail_node(detail: dict) -> dict[str, Any]:
    """The detail document's actual job fields, unwrapped from its envelope.

    A per-job `get` returns `{"data": {"marketplaceJobPosting": {...}}}` -
    everything this module reads from a detail (description, contract terms,
    client company) lives inside that inner object, never at the top level.
    """
    return _as_dict(_as_dict(detail.get("data")).get("marketplaceJobPosting"))


def _client_company(detail_node: dict) -> dict[str, Any]:
    """The client's location block. Only present on the detail document."""
    return _as_dict(detail_node.get("clientCompanyPublic"))


def _salary(
    item: dict, detail_node: dict, job_type: str
) -> tuple[float | None, float | None, str | None, bool]:
    """(min, max, period, is_stated).

    Hourly rows carry their real range only in the detail's `contractTerms`;
    the search-level `budget` field is a placeholder `"0.0"` for them. Fixed
    rows carry the real number in `budget` directly, with no detail needed.
    """
    hourly_terms = _as_dict(_as_dict(detail_node.get("contractTerms")).get("hourlyContractTerms"))
    low = _positive_float(hourly_terms.get("hourlyBudgetMin"))
    high = _positive_float(hourly_terms.get("hourlyBudgetMax"))
    if low is not None or high is not None:
        return low, high, "hourly", True

    budget = _positive_float(item.get("budget"))
    if budget is not None:
        # Only "hourly"/"fixed" are meaningful downstream - an unexpected or
        # missing API string must not leak into the stored period.
        period = "hourly" if job_type.startswith("hourly") else "fixed"
        return budget, None, period, True
    return None, None, None, False


def _trailer(item: dict, client_company: dict) -> str:
    """Skills, experience level, proposal count, and a `Client:` summary line.

    Every field here is search-result metadata (snake_case, top-level, or the
    nested `client` block) - none of it depends on a detail having been
    fetched, so a snippet-only posting still gets a real trailer.
    """
    skills = item.get("skills") or []
    experience_level = item.get("experience_level")
    proposals = item.get("proposal_count")
    stats = _as_dict(item.get("client"))

    client_bits = [
        client_company.get("country") or stats.get("country") or "unknown country",
        f"{stats.get('total_hires')} hires" if stats.get("total_hires") is not None else "hires unknown",
        f"{stats.get('total_spent')} spent" if stats.get("total_spent") is not None else "spend unknown",
        f"{stats.get('rating')} rating" if stats.get("rating") is not None else "rating unknown",
        stats.get("verification_status") or "verification unknown",
    ]

    lines = [
        f"Skills: {', '.join(skills) if skills else 'none listed'}",
        f"Experience level: {experience_level or 'unstated'}",
        f"Proposals: {proposals if proposals is not None else 'unstated'}",
        f"Client: {', '.join(str(bit) for bit in client_bits)}",
    ]
    return "\n".join(lines)


def _cutoff(max_age_days: int) -> str:
    """The oldest `created_date` worth paginating into, as an ISO date.

    The API has no date filter of its own (see upwork_query.py) - this and
    `sort="recency"` are what stand in for one. Reusing the user's own
    `max_age_days` rather than a second, Upwork-only constant means one knob
    controls "how old is too old" everywhere a source can answer it.
    """
    return (utcnow() - dt.timedelta(days=max(1, max_age_days))).date().isoformat()


def _render_prompt(
    config: Config,
    *,
    call: dict[str, Any],
    cutoff: str,
    max_pages: int,
    known_ids: set[str],
    detail_budget: int,
) -> str | None:
    """Fill `upwork_fetch.md`'s placeholders, or None if no template is readable.

    A missing packaged asset or an unreadable user copy must degrade `fetch`
    to the empty envelope, not raise out of it - `rank/runner.py`'s prompt
    loader (`_prompt_text`) makes the same existence check before reading.

    Substitution is one regex pass (`_PLACEHOLDER`) over the *original* text,
    not five chained `.replace()` calls: `{call}` renders as a JSON object
    that could itself contain a literal "{cutoff}" or "{known_ids}" inside a
    user's own query string, and a later `.replace()` call would then corrupt
    that already-substituted text. One pass never re-scans a substitution.
    """
    user_path = config.home / _PROMPT_RELATIVE
    source = user_path if user_path.exists() else _PACKAGED_PROMPT
    if not source.exists():
        return None
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None

    known = ", ".join(sorted(known_ids)) if known_ids else "(none yet - fetch every detail)"
    mapping = {
        "{call}": json.dumps(call, indent=2),
        "{cutoff}": cutoff,
        "{max_pages}": str(max_pages),
        "{known_ids}": known,
        "{detail_budget}": str(detail_budget),
    }
    return _PLACEHOLDER.sub(lambda m: mapping[m.group(0)], text)


def _parse(raw: str) -> dict[str, Any] | None:
    """The envelope inside `agent.run`'s answer text, or None if none is readable.

    `agent.run` already unwraps the CLI's own `--output-format json` envelope
    via `agent.text_of` before this ever sees the string, so what arrives here
    is the model's answer text - which still needs its own JSON pulled out of
    whatever prose or code fence surrounds it. Mirrors `gate._parse`: a bare
    `json.loads` first, then `_OBJECT`, because a model asked for "one JSON
    object" reliably wraps it in a code fence or a sentence anyway.
    """
    text = (raw or "").strip()
    if not text:
        return None

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _OBJECT.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_envelope(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """The exact two-key shape `normalize` expects, or None if `parsed` isn't one.

    A model can hand back valid JSON that is not a real result: `{}`, an
    `{"error": "..."}` with no `pages` at all, or a `pages` that came back the
    wrong type. Writing any of those to disk as the envelope reports success
    with zero jobs - indistinguishable from a search that genuinely found
    nothing. Only a well-typed `pages` list counts as an actual result;
    anything else is treated as a failure to report upstream, not data to
    normalize. `details` is coerced rather than gating on, since a missing or
    malformed `details` still leaves the search results themselves usable.
    """
    pages = parsed.get("pages")
    if not isinstance(pages, list):
        return None
    details = parsed.get("details")
    return {"pages": pages, "details": details if isinstance(details, dict) else {}}


class UpworkAdapter(HttpAdapter):
    source_id = "upwork"
    market = "upwork"
    # No boards table entry gets an Upwork fetch: refs come from preferences,
    # generated fresh by `board_refs` every run. See SourceAdapter.generates_refs.
    generates_refs = True

    def __init__(
        self,
        refs: list[BoardRef] | None = None,
        *,
        config: Config | None = None,
        known_ids: set[str] | None = None,
    ) -> None:
        super().__init__(refs)
        self.config = config
        # `FetchBudget` needs a real config to touch the database - a
        # config-less adapter (the `normalize`-only shape the tests above all
        # use) never calls `fetch` for real, so it gets no budget rather than
        # one that would blow up the moment anything asked it a question.
        self.budget = FetchBudget(config) if config is not None else None
        self.known_ids = known_ids or set()

    def board_refs(self, prefs: preferences_module.UpworkPreferences) -> list[BoardRef]:
        """One ref per (query, job_type). See `upwork_query.refs_for`."""
        return [
            BoardRef(provider=self.source_id, token=f"{q}|{job_type}", market=self.market)
            for q, job_type in query.refs_for(prefs)
        ]

    def still_fetching(self) -> bool:
        """False once the budget has nothing left to spend for this run.

        Mirrors `LinkedInAdapter.still_fetching`: a config-less adapter has
        nothing to be polite about, and a budget that has tripped or run dry
        makes every remaining ref in this run a guaranteed-empty round trip.
        """
        return self.budget is None or self.budget.allow()

    def was_truncated(self) -> bool:
        """Always True.

        Every other adapter's `was_truncated` answers "did this fetch see the
        whole listing" so `deactivate_missing` can trust an empty result to
        mean "this ref genuinely has nothing left". A parameterised Upwork
        search is never the whole listing - it is one query against one
        filtered slice of the board, by construction. Reporting True
        unconditionally is what stops `deactivate_missing` from reading a
        legitimately-empty page (or a page that just missed the rate floor)
        as proof the entire corpus behind that query vanished.
        """
        return True

    def fetch(self, ref: BoardRef, client: httpx.Client | None) -> dict[str, Any]:
        """One search-and-detail pass through the Upwork MCP, via `claude -p`.

        Never raises. No config, a tripped or exhausted budget, an unreadable
        prompt template, a subprocess failure or timeout, or a response this
        module cannot parse into the expected shape all degrade to the same
        empty envelope - the contract `linkedin.py` established, because a
        caller iterating many refs cannot afford one bad ref to end the run.
        """
        empty: dict[str, Any] = {"pages": [], "details": {}}
        if self.config is None or self.budget is None:
            return empty

        try:
            prefs, _ = preferences_module.load(self.config)
        except Exception:
            log.warning("upwork fetch for %r skipped: preferences could not be read", ref.token)
            self.budget.record_failure()
            return empty

        job_query, _, job_type = ref.token.partition("|")
        # `org_uid` is a sibling of `params` on every `find_jobs` call, never a
        # filter inside it - confirmed live against the MCP, not inferred.
        # `search_params` stays a pure filter-dict mapper; the account id is
        # env/deployment concern, so it is layered on here instead.
        org_uid = os.environ.get(query.ORG_UID_ENV) or _DEFAULT_ORG_UID
        search_call = {
            "action": "search",
            "org_uid": org_uid,
            "params": query.search_params(job_query, job_type, prefs.upwork),
        }

        prompt = _render_prompt(
            self.config,
            call=search_call,
            cutoff=_cutoff(prefs.max_age_days),
            max_pages=max(1, prefs.upwork.max_pages),
            known_ids=self.known_ids,
            detail_budget=DETAIL_BUDGET,
        )
        if prompt is None:
            log.warning("upwork fetch for %r skipped: prompt template unreadable", ref.token)
            self.budget.record_failure()
            return empty

        envelope: dict[str, Any] | None = None
        # One retry, for an unparseable or wrongly-shaped response only - a
        # subprocess failure, a timeout, or a reported `error` is not retried,
        # since the CLI already ran the whole turn (or told us plainly it
        # could not) and a repeat is no more likely to succeed. The budget is
        # spent, and the refusal checked, on *each* attempt - a free second
        # agent turn (and a free second live search) for one budget unit would
        # make DAILY_REFS mean up to twice as many real turns as its name
        # promises. The one check this replaces (before the loop, against the
        # same `self.budget.refusal()`) was redundant with attempt 1's own
        # check below.
        for attempt in (1, 2):
            refusal = self.budget.refusal()
            if refusal is not None:
                log.warning(
                    "upwork fetch for %r stopped before %s: %s",
                    ref.token, "the search" if attempt == 1 else "a retry", refusal,
                )
                return empty
            self.budget.spend()
            try:
                raw = agent.run(
                    self.config, "upwork", prompt, tools=ALLOWED_TOOLS,
                    timeout=FETCH_TIMEOUT_SECONDS,
                )
            except (agent.AgentError, subprocess.TimeoutExpired) as error:
                log.warning("upwork fetch for %r failed: %s", ref.token, error)
                self.budget.record_failure()
                return empty

            parsed = _parse(raw)
            if parsed is None:
                continue

            # A model that follows the prompt's own "if a call fails" section
            # reports the problem correctly - as a well-typed `pages: []` plus
            # a truthy `error`. That shape would otherwise sail straight
            # through `_coerce_envelope` and reach `record_ok()`: the exact
            # "indistinguishable from a genuinely empty search" state the
            # coercion exists to prevent, arriving through the door the
            # coercion itself doesn't watch. A reported error is therefore a
            # failure regardless of how well-typed the rest of the envelope
            # is, and it is not retried - the model already told us why.
            reported_error = parsed.get("error")
            coerced = _coerce_envelope(parsed)
            if reported_error:
                log.warning("upwork fetch for %r reported an error: %s", ref.token, reported_error)
                self.budget.record_failure()
                partial = coerced or empty
                return {**partial, "error": str(reported_error)}

            envelope = coerced
            if envelope is not None:
                break

        if envelope is None:
            log.warning("upwork fetch for %r returned an unreadable or malformed response", ref.token)
            self.budget.record_failure()
            return empty

        self.budget.record_ok()
        return envelope

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        """Fetch envelope -> postings. Never raises: one bad result is one lost job.

        A shape a real fetch never actually produced is exactly what a
        hand-written fixture cannot rule out, so every result is isolated in
        its own try/except rather than trusted to be well-formed - and the
        page/result containers themselves are guarded before iterating, since
        a non-list `pages` or `results` would otherwise raise a `TypeError`
        straight out of this generator and kill the whole run.
        """
        if not isinstance(raw, dict):
            return
        details = _as_dict(raw.get("details"))
        for page in _as_list(raw.get("pages")):
            if not isinstance(page, dict):
                continue
            for item in _as_list(page.get("results")):
                try:
                    posting = self._normalize_one(item, ref, details)
                except Exception:
                    continue
                if posting is not None:
                    yield posting

    def _normalize_one(self, item: Any, ref: BoardRef, details: dict) -> JobPosting | None:
        if not isinstance(item, dict):
            return None
        external_id = str(item.get("id") or "")
        title = (item.get("title") or "").strip()
        if not external_id or not title:
            return None

        detail_node = _detail_node(_as_dict(details.get(external_id)))
        job_type = str(item.get("job_type") or "").lower()

        raw_description = _as_dict(detail_node.get("content")).get("description")
        detail_text = _strip_untrusted_wrapper(_as_str(raw_description)) if raw_description else ""
        if detail_text:
            # A detail was fetched and it actually held prose after stripping -
            # `norm.completeness` is not used here on purpose: that helper's
            # length threshold is for judging text of *unknown* wholeness
            # (as on greenhouse.py, where the source always sends the whole
            # body). Here wholeness is a known fact from where the text came
            # from, not something to re-derive from how long it happens to be.
            description_text = detail_text
            jd_completeness = "full"
            jd_source = "api"
        else:
            # Either no detail was fetched, or it was fetched and its
            # description was only the wrapper tags / whitespace - either way
            # there is no whole body, so this falls to the snippet path
            # rather than a bare truthiness check on `raw_description`.
            snippet_text = _as_str(item.get("description_snippet")).strip()
            description_text = snippet_text
            # A snippet is truncated by definition, never "full" - regardless
            # of how long it happens to be. `jd_completeness` is load-bearing
            # downstream (applications.py gates on "full"; store.py stamps
            # jd_extracted_at on it), so a long snippet mislabelled "full"
            # would be recorded as an extracted, complete JD and never
            # re-fetched.
            jd_completeness = "snippet" if snippet_text else "none"
            jd_source = "api" if snippet_text else None

        client_company = _client_company(detail_node)
        trailer = _trailer(item, client_company)
        description_md = f"{description_text}\n\n{trailer}" if description_text else trailer

        salary_min, salary_max, salary_period, salary_is_stated = _salary(item, detail_node, job_type)

        # The client's country, not a work location - every Upwork gig is remote,
        # so this answers "where is the client based", never "where must I be".
        country_name = client_company.get("country") or _as_dict(item.get("client")).get("country")
        country = norm.country_code(country_name)

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=COMPANY_NAME_PLACEHOLDER,
            location_raw="Remote",
            country=country,
            remote_type="remote",
            employment_type="contract",
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency="USD" if salary_is_stated else None,
            salary_period=salary_period,
            salary_is_stated=salary_is_stated,
            description_text=description_text or None,
            description_md=description_md,
            jd_completeness=jd_completeness,
            jd_source=jd_source,
            # `created_date`, not `published_date`: the API's own cutoff logic
            # (there is no date filter, only client-side pagination against
            # this field - see `upwork_query.py`) keys off `created_date`, so
            # a stored `posted_at` that disagreed with it would make a job
            # look older or newer than the cutoff that actually admitted it.
            posted_at=norm.parse_datetime(item.get("created_date")),
            apply_url=item.get("url"),
            source_url=item.get("url"),
        )
