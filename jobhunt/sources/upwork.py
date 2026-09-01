"""Upwork job search, via the Upwork MCP.

This is the pure half only: `normalize` turns a stored fetch envelope into
`JobPosting` rows. `fetch` (a later task) calls out to `claude -p` with only
`find_jobs` on the tool allow-list - no adapter here ever spends a Connect.

Facts below were verified live against the Upwork MCP, not re-derived from
documentation:

- A search result carries a *truncated* `description_snippet`. The full
  description exists only in `details[id]`, populated by a per-job `get`.
- `budget` is the string `"0.0"` on hourly rows regardless of the real rate -
  the actual range lives in `contractTerms.hourlyContractTerms.hourlyBudgetMin`/
  `hourlyBudgetMax`, inside the detail document only. Zero is never a real
  budget for either contract type, so it is treated as absent rather than
  free work.
- `clientCompanyPublic` carries `country`, `state` and `timezone` - and no
  company name. That is why `company_name` falls back to a constant here;
  Task 8 extracts a real identity from the description text.

Upwork's `experience_level` (`ENTRY_LEVEL`/`INTERMEDIATE`/`EXPERT`) rates the
*contract's* difficulty, not the freelancer's career stage. `JobPosting.seniority`
is deliberately left unset - mapping it onto `SENIORITY_ORDER` would let a
salaried seniority band silently filter gigs, exactly the class of bug this
project keeps re-finding elsewhere.
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


def _client_company(item: dict, detail: dict) -> dict[str, Any]:
    """The client's location block. Detail wins when both are present."""
    return _as_dict(detail.get("clientCompanyPublic")) or _as_dict(item.get("clientCompanyPublic"))


def _client_stats(item: dict, detail: dict) -> dict[str, Any]:
    return _as_dict(detail.get("client")) or _as_dict(item.get("client"))


def _salary(item: dict, detail: dict, job_type: str) -> tuple[float | None, float | None, str | None, bool]:
    """(min, max, period, is_stated).

    Hourly rows carry their real range only in the detail's `contractTerms`;
    the search-level `budget` field is a placeholder `"0.0"` for them. Fixed
    rows carry the real number in `budget` directly, with no detail needed.
    """
    hourly_terms = _as_dict(_as_dict(detail.get("contractTerms")).get("hourlyContractTerms"))
    low = hourly_terms.get("hourlyBudgetMin")
    high = hourly_terms.get("hourlyBudgetMax")
    if low or high:
        return low, high, "hourly", True

    try:
        budget = float(item.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget > 0:
        return budget, None, job_type or "fixed", True
    return None, None, None, False


def _trailer(item: dict, detail: dict) -> str:
    """Skills, experience level, proposal count, and a `Client:` summary line.

    Prefers the detail document's fields when a detail was fetched; falls back
    to whatever the search result itself carried, so a snippet-only posting
    still gets a trailer rather than a bare "Skills:"/"Client:" with nothing
    after it.
    """
    source = detail or item
    skills = source.get("skills") or item.get("skills") or []
    experience_level = source.get("experienceLevel") or item.get("experienceLevel")
    proposals = source.get("numberOfProposals") or source.get("proposalsTier")

    company = _client_company(item, detail)
    stats = _client_stats(item, detail)
    client_bits = [
        company.get("country") or "unknown country",
        f"{stats.get('totalHires')} hires" if stats.get("totalHires") is not None else "hires unknown",
        f"${stats.get('totalSpent')} spent" if stats.get("totalSpent") is not None else "spend unknown",
        f"{stats.get('feedback')} rating" if stats.get("feedback") is not None else "rating unknown",
        stats.get("paymentVerificationStatus") or "verification unknown",
    ]

    lines = [
        f"Skills: {', '.join(skills) if skills else 'none listed'}",
        f"Experience level: {experience_level or 'unstated'}",
        f"Proposals: {proposals or 'unstated'}",
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
        its own try/except rather than trusted to be well-formed.
        """
        if not isinstance(raw, dict):
            return
        details = _as_dict(raw.get("details"))
        for page in raw.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for item in page.get("results") or []:
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

        detail = _as_dict(details.get(external_id))
        job_type = str(item.get("job_type") or "").lower()

        raw_description = detail.get("description")
        if raw_description:
            description_text = _strip_untrusted_wrapper(str(raw_description))
            jd_completeness = "full"
            jd_source = "api"
        else:
            description_text = (item.get("description_snippet") or "").strip()
            jd_completeness = norm.completeness(description_text) if description_text else "snippet"
            jd_source = "api" if description_text else None

        trailer = _trailer(item, detail)
        description_md = f"{description_text}\n\n{trailer}" if description_text else trailer

        salary_min, salary_max, salary_period, salary_is_stated = _salary(item, detail, job_type)

        client_company = _client_company(item, detail)
        # The client's country, not a work location - every Upwork gig is remote,
        # so this answers "where is the client based", never "where must I be".
        country = norm.country_code(client_company.get("country"))

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
            posted_at=norm.parse_datetime(item.get("created_date")),
            apply_url=item.get("url"),
            source_url=item.get("url"),
        )
