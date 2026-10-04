"""Techcareer.net, a Turkish job board for tech roles.

No Cloudflare, and robots.txt disallows only click-tracking paths. The site's
own front end reads a JSON API that answers without a key. Probed live
2026-10-04:

    GET https://www.techcareer.net/api/bff/jobs/job-list?jobs[page]=<n>
        -> {"jobs": [...20], "totalCount": 181, "pageCount": 10,
            "pageNumber": 1, "pageSize": 20}

The parameter is the bracketed `jobs[page]` the site's query-string library
builds; a plain `page` or `pageNumber` answers 500. `jobs[pageSize]` is
ignored, so a page is always 20.

The whole board is under two hundred jobs, so it is a feed: every run reads
every page, and there are no searches to build. Each list item carries the
full description, so there is no detail fetch.

What a list item does not carry is a date or a work type; those are on the
detail endpoint (`/api/bff/jobs/job-detail?slug=<slug>`), which is not worth a
request per job for this. Techcareer is a Kariyer.net company and many of its
postings are Kariyer.net's too; the cross-source clustering folds those copies
into one card.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

from jobhunt.pipeline import normalize as norm
from jobhunt.sources.base import BoardRef, HttpAdapter, JobPosting, RateLimit

LIST_URL = "https://www.techcareer.net/api/bff/jobs/job-list"
DETAIL_URL = "https://www.techcareer.net/jobs/detail/{slug}"

# A runaway pageCount must not turn one feed into hundreds of requests.
MAX_PAGES = 20

# Work places, most flexible first: a job offering several is filed under the
# most flexible one, the way the remote filter reads it. Matched exactly, not
# lowercased: Python lowercases "İ" to "i" plus a combining dot.
_WORK_PLACES = (("Uzaktan", "remote"), ("Hibrit", "hybrid"), ("İş Yerinde", "onsite"))

_HIDDEN_COMPANY = "Gizli Firma"


class TechcareerAdapter(HttpAdapter):
    source_id = "techcareer"
    market = "tr_local"
    rate_limit = RateLimit(2.0)

    def fetch(self, ref: BoardRef, client: httpx.Client) -> dict[str, Any]:
        """Every page of the board, as the API returns them."""
        pages: list[Any] = []
        while len(pages) < MAX_PAGES:
            if pages:
                self.rate_limit.sleep()
            page = self._get_json(client, f"{LIST_URL}?jobs[page]={len(pages) + 1}")
            pages.append(page)
            if not isinstance(page, dict) or not page.get("jobs"):
                break
            if len(pages) >= int(page.get("pageCount") or 0):
                break
        return {"pages": pages}

    def normalize(self, raw: Any, ref: BoardRef) -> Iterator[JobPosting]:
        if not isinstance(raw, dict):
            return
        seen: set[str] = set()
        for page in raw.get("pages") or []:
            if not isinstance(page, dict):
                continue
            for job in page.get("jobs") or []:
                if not isinstance(job, dict):
                    continue
                posting = self._normalize_one(job, ref)
                # A job published between two page reads shifts the rest by one.
                if posting is not None and posting.external_id not in seen:
                    seen.add(posting.external_id)
                    yield posting

    def _normalize_one(self, job: dict, ref: BoardRef) -> JobPosting | None:
        external_id = str(job.get("jobId") or "")
        title = (job.get("title") or "").strip()
        slug = (job.get("slug") or "").strip()
        if not external_id or not title or not slug:
            return None
        if job.get("isDisabledJob") or job.get("jobStatus") not in (None, "Published"):
            return None

        places = [_name(job.get("location"), "locationName")]
        places += [_name(other, "locationName") for other in job.get("otherLocations") or []]
        places = [p for p in places if p]
        location_raw = "; ".join(places) or None
        # "İstanbul(Asya) / Türkiye": the first place decides country and city.
        country, city, _ = norm.parse_location(places[0] if places else None)

        description_html = job.get("description") or ""
        description_text = norm.html_to_text(description_html) if description_html else ""
        url = DETAIL_URL.format(slug=slug)
        department = (job.get("jobDepartmentName") or "").strip()

        return JobPosting(
            source=self.source_id,
            external_id=external_id,
            market=ref.market,
            title=title,
            company_name=_company(job),
            location_raw=location_raw,
            country=country,
            city=city,
            remote_type=_remote_type(job.get("workPlaces")),
            description_html=description_html or None,
            description_text=description_text or None,
            description_md=norm.html_to_markdown(description_html) if description_html else None,
            jd_completeness=norm.completeness(description_text),
            jd_source="api" if description_text else None,
            # A job applied for off the site (an employer's own careers page) says where.
            apply_url=job.get("applyLink") or url,
            source_url=url,
            departments=[department] if department else [],
        )


def _name(value: Any, key: str) -> str:
    if isinstance(value, dict):
        return str(value.get(key) or "").strip()
    return value.strip() if isinstance(value, str) else ""


def _company(job: dict) -> str:
    """The employer, or the board's own label when the employer is hidden."""
    company = job.get("company") if isinstance(job.get("company"), dict) else {}
    name = (company.get("companyProfileName") or "").strip()
    if name:
        return name
    return _name(job.get("secretCompanyInfo"), "secretCompanyDescription") or _HIDDEN_COMPANY


def _remote_type(work_places: Any) -> str:
    names = {_name(w, "workPlaceName") for w in work_places or []}
    for word, remote_type in _WORK_PLACES:
        if word in names:
            return remote_type
    return "unknown"
