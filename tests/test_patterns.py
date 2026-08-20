"""The regex bank is Strategy A's whole surface area. Every provider gets a real URL."""
from __future__ import annotations

import pytest

from jobhunt.discovery import patterns
from jobhunt.discovery.patterns import BoardHit, DomainHint


def hits(text: str) -> set[BoardHit]:
    return patterns.scan(text)[0]


def hints(text: str) -> set[DomainHint]:
    return patterns.scan(text)[1]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.ashbyhq.com/ramp/8f2c1a04-1111-2222-3333-444455556666", ("ashby", "ramp")),
        ("https://embed.ashbyhq.com/openai/jobs", ("ashby", "openai")),
        (
            "https://api.ashbyhq.com/posting-api/job-board/notion?includeCompensation=true",
            ("ashby", "notion"),
        ),
        ("https://boards.greenhouse.io/stripe/jobs/1234567", ("greenhouse", "stripe")),
        ("https://job-boards.greenhouse.io/anthropic/jobs/4020", ("greenhouse", "anthropic")),
        (
            "https://boards.greenhouse.io/embed/job_board?for=airbnb&token=1",
            ("greenhouse", "airbnb"),
        ),
        (
            "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true",
            ("greenhouse", "figma"),
        ),
        ("https://jobs.lever.co/matchgroup/abc-def", ("lever", "matchgroup")),
        ("https://jobs.eu.lever.co/voleon/xyz", ("lever", "voleon")),
        ("https://api.lever.co/v0/postings/voleon?mode=json", ("lever", "voleon")),
        ("https://channable.recruitee.com/o/senior-engineer", ("recruitee", "channable")),
        ("https://apply.workable.com/acme-inc/j/7A1B2C3D4E/", ("workable", "acme-inc")),
        (
            "https://careers.smartrecruiters.com/Visa/743999888",
            ("smartrecruiters", "Visa"),
        ),
        (
            "https://api.smartrecruiters.com/v1/companies/Visa/postings",
            ("smartrecruiters", "Visa"),
        ),
        ("https://personio.jobs.personio.de/job/12345", ("personio", "personio")),
        ("https://acme.jobs.personio.com/job/999", ("personio", "acme")),
        ("https://someco.teamtailor.com/jobs/1234-engineer", ("teamtailor", "someco")),
    ],
)
def test_single_url(url: str, expected: tuple[str, str]) -> None:
    assert BoardHit(*expected) in hits(url)


def test_workday_needs_tenant_wd_and_site() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Remote/Engineer_JR1"
    assert BoardHit("workday", "nvidia/wd5/NVIDIAExternalCareerSite") in hits(url)


def test_workday_api_url_form() -> None:
    url = "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    assert BoardHit("workday", "nvidia/wd5/NVIDIAExternalCareerSite") in hits(url)


def test_reserved_subdomain_is_not_a_token() -> None:
    assert hits("https://www.recruitee.com/pricing") == set()
    assert hits("https://api.teamtailor.com/v1/jobs") == set()


def test_reserved_path_segment_is_not_a_token() -> None:
    # The embed URL carries its token in ?for=, so the path segment must not win.
    found = hits("https://boards.greenhouse.io/embed/job_board?for=airbnb")
    assert found == {BoardHit("greenhouse", "airbnb")}


def test_bare_numeric_segment_is_not_a_token() -> None:
    assert hits("https://jobs.lever.co/12345") == set()


def test_html_escaped_url_still_matches() -> None:
    body = "Apply at &lt;a href=&quot;https://jobs.ashbyhq.com/linear/abc&quot;&gt;here&lt;/a&gt;"
    assert BoardHit("ashby", "linear") in hits(body)


def test_json_escaped_slashes_still_match() -> None:
    body = '{"url":"https:\\/\\/boards.greenhouse.io\\/discord\\/jobs\\/55"}'
    assert BoardHit("greenhouse", "discord") in hits(body)


def test_description_body_with_many_links() -> None:
    body = """
    <p>See also <a href="https://jobs.lever.co/plaid/1">Plaid</a> and
    <a href="https://boards.greenhouse.io/brex/jobs/2">Brex</a>.</p>
    <script src="https://cdn.jsdelivr.net/npm/thing.js"></script>
    """
    assert hits(body) == {BoardHit("lever", "plaid"), BoardHit("greenhouse", "brex")}


def test_employer_hosted_greenhouse_url_is_a_hint_not_a_hit() -> None:
    url = "https://stripe.com/jobs/listing/engineer/1234?gh_jid=5551212"
    assert hits(url) == set()
    assert hints(url) == {DomainHint("greenhouse", "stripe.com")}


def test_hint_strips_www_and_ignores_provider_hosts() -> None:
    url = "https://www.acme.io/careers?gh_src=abc"
    assert hints(url) == {DomainHint("greenhouse", "acme.io")}
    assert hints("https://boards.greenhouse.io/acme/jobs/1?gh_src=x") == set()


def test_noise_produces_nothing() -> None:
    assert patterns.scan("https://example.com/about") == (set(), set())
    assert patterns.scan(None) == (set(), set())
    assert patterns.scan("") == (set(), set())


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("stripe.com", "stripe"),
        ("careers.acme.io", "acme"),
        ("acme.co.uk", "acme"),
        ("getir.com.tr", "getir"),
        ("localhost", None),
        ("www.jobs.com", None),
    ],
)
def test_guess_token(domain: str, expected: str | None) -> None:
    assert patterns.guess_token(domain) == expected


def test_scan_many_unions() -> None:
    found, _ = patterns.scan_many(
        ["https://jobs.lever.co/plaid/1", None, "https://jobs.lever.co/brex/2"]
    )
    assert found == {BoardHit("lever", "plaid"), BoardHit("lever", "brex")}


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/robots.txt",
        "https://boards.greenhouse.io/sitemap.xml",
        "https://boards.greenhouse.io/YOUR_COMPANY/jobs/1",
        "https://boards.greenhouse.io/a3c41b8b71eff8c4/jobs/1",
    ],
)
def test_junk_tokens_from_common_crawl_are_rejected(url: str) -> None:
    """Every junk token costs a validating fetch, and Common Crawl produces them
    by the hundred."""
    assert hits(url) == set()
