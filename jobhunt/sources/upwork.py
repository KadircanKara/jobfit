"""Upwork job search, via the Upwork MCP.

This is the pure half only: `normalize` turns a stored fetch envelope into
`JobPosting` rows. `fetch` (a later task) calls out to `claude -p` with only
`find_jobs` on the tool allow-list - no adapter here ever spends a Connect.

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

from collections.abc import Iterator
from typing import Any

from jobhunt import preferences as preferences_module
from jobhunt.pipeline import normalize as norm
from jobhunt.sources import upwork_query as query
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting

_UNTRUSTED_OPEN = "<untrusted_participant_content>"
_UNTRUSTED_CLOSE = "</untrusted_participant_content>"

COMPANY_NAME_PLACEHOLDER = "Upwork client"


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


class UpworkAdapter(HttpAdapter):
    source_id = "upwork"
    market = "upwork"
    # No boards table entry gets an Upwork fetch: refs come from preferences,
    # generated fresh by `board_refs` every run. See SourceAdapter.generates_refs.
    generates_refs = True

    def board_refs(self, prefs: preferences_module.UpworkPreferences) -> list[BoardRef]:
        """One ref per (query, job_type). See `upwork_query.refs_for`."""
        return [
            BoardRef(provider=self.source_id, token=f"{q}|{job_type}", market=self.market)
            for q, job_type in query.refs_for(prefs)
        ]

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
