"""The extraction ladder and its quality gate. No network.

The gate is the part that matters. Without it, a rung returning a two-line SEO
stub would stop the ladder and hand that stub to the tailoring skill, which is
the worst failure this system has: a CV tailored against a truncated JD looks
finished.
"""
from __future__ import annotations

import json

from jobhunt import store
from jobhunt.db.models import Job
from jobhunt.db.session import session_scope
from jobhunt.extract import fetcher, jsonld, ladder, quality, readability
from jobhunt.sources.base import JobPosting

GOOD_JD = """
About the role

We are looking for a senior backend engineer to own our ingestion pipeline.
You will design APIs, run Postgres at scale, and work closely with the ML team.

Requirements

Five years of Python. Experience with FastAPI and async IO. Comfortable owning
infrastructure end to end. Strong written communication in English.

Responsibilities

Design and ship services. Review code. Mentor two junior engineers.
"""


def jsonld_page(description: str, **overrides) -> str:
    payload = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Backend Engineer",
        "description": f"<p>{description}</p>",
        "datePosted": "2026-08-01",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": "Acme", "sameAs": "https://acme.io"},
        "jobLocation": {
            "@type": "Place",
            "address": {"addressLocality": "Berlin", "addressCountry": "Germany"},
        },
        "baseSalary": {
            "@type": "MonetaryAmount",
            "currency": "EUR",
            "value": {"minValue": 80000, "maxValue": 100000, "unitText": "YEAR"},
        },
    }
    payload.update(overrides)
    return f"""<html><head>
    <script type="application/ld+json">{json.dumps(payload)}</script>
    </head><body><nav>Jobs</nav><p>ignored</p></body></html>"""


# --- quality gate -------------------------------------------------------------


def test_a_real_jd_passes() -> None:
    result = quality.assess(GOOD_JD)
    assert result.passed
    assert result.score >= 0.9


def test_a_short_stub_fails_regardless_of_anything_else() -> None:
    result = quality.assess("Senior Backend Engineer. Apply now.")
    assert not result.passed
    assert any("chars" in reason for reason in result.reasons)


def test_empty_text_fails() -> None:
    assert not quality.assess("").passed
    assert not quality.assess(None).passed


def test_a_truncation_marker_costs_score() -> None:
    truncated = GOOD_JD[:600] + " ...read more"
    result = quality.assess(truncated)
    assert any("read-more" in reason or "mid-sentence" in reason for reason in result.reasons)


def test_a_page_that_is_mostly_boilerplate_fails() -> None:
    boilerplate = (
        "Acme is an equal opportunity employer.\n\n"
        "All qualified applicants will receive consideration without regard to race, "
        "colour, religion, sex, or national origin, and we participate in E-Verify.\n\n"
        "We use cookies. See our privacy policy for details about cookie handling.\n\n"
    ) * 3
    result = quality.assess(boilerplate + "\n\nWe need an engineer.")
    assert not result.passed
    assert any("boilerplate" in reason for reason in result.reasons)


def test_turkish_sections_count_as_sections() -> None:
    turkish = (
        "İş Tanımı\n\nBackend geliştirici arıyoruz. Python ve FastAPI ile çalışacaksınız.\n\n"
        "Aranan Nitelikler\n\nBeş yıl deneyim. Postgres bilgisi. İngilizce yazışma becerisi.\n\n"
        "Sorumluluklar\n\nServis tasarlamak ve kod incelemek.\n\n"
    ) * 2
    assert quality.assess(turkish).passed


# --- rung 1 -------------------------------------------------------------------


def test_jsonld_pulls_the_posting_out_of_a_page() -> None:
    found = jsonld.extract(jsonld_page(GOOD_JD))
    assert found is not None
    assert found.usable
    assert found.title == "Senior Backend Engineer"
    assert found.company == "Acme"
    assert found.company_domain == "acme.io"
    assert found.country == "DE"
    assert found.city == "Berlin"
    assert "80000" in found.salary_text


def test_jsonld_finds_a_posting_nested_in_a_graph() -> None:
    page = f"""<html><script type="application/ld+json">{json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Board"},
            {"@type": "JobPosting", "title": "Engineer", "description": GOOD_JD},
        ],
    })}</script></html>"""
    found = jsonld.extract(page)
    assert found is not None and found.title == "Engineer"


def test_jsonld_handles_escaped_description_html() -> None:
    escaped = "&lt;p&gt;" + GOOD_JD + "&lt;/p&gt;"
    found = jsonld.extract(jsonld_page(GOOD_JD).replace(
        json.dumps(f"<p>{GOOD_JD}</p>"), json.dumps(escaped)
    ))
    assert found is not None
    assert "<p>" in found.description_html


def test_jsonld_survives_broken_markup_and_bad_json() -> None:
    assert jsonld.extract("<html><script type='application/ld+json'>{not json</script>") is None
    assert jsonld.extract("<html><body>no structured data</body></html>") is None
    assert jsonld.extract(None) is None


# --- rung 2 -------------------------------------------------------------------


def test_readability_strips_chrome_and_keeps_the_body() -> None:
    page = f"""<html><body>
      <nav><a href="/x">Jobs</a><a href="/y">Companies</a></nav>
      <article><h1>Senior Backend Engineer</h1><p>{GOOD_JD}</p></article>
      <footer>Cookie policy. Privacy policy.</footer>
    </body></html>"""
    markdown, text = readability.extract(page)
    assert markdown and "senior backend engineer" in markdown.lower()
    assert "ingestion pipeline" in text


def test_readability_returns_nothing_for_nothing() -> None:
    assert readability.extract(None) == (None, None)
    assert readability.extract("") == (None, None)


# --- the ladder ---------------------------------------------------------------


def make_job(cfg, **kwargs) -> int:
    defaults = {
        "source": "wwr", "external_id": "x1", "market": "global_remote",
        "title": "Senior Backend Engineer", "company_name": "Acme",
        "source_url": "https://example.com/jobs/1",
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        return job.id


def place_detail(cfg, source: str, job_id: int, html: str) -> None:
    path = fetcher.cache_path(cfg, source, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def test_a_listing_that_already_has_a_full_jd_costs_no_fetch(cfg) -> None:
    job_id = make_job(cfg, description_text=GOOD_JD, jd_completeness="full", jd_source="api")
    with session_scope(cfg.db_path) as session:
        result = ladder.run(cfg, session, session.get(Job, job_id))
    assert result.rung == "api"
    assert result.origin is None  # nothing was fetched


def test_a_snippet_is_upgraded_from_json_ld(cfg) -> None:
    job_id = make_job(cfg, description_text="Short teaser.", jd_completeness="snippet")
    place_detail(cfg, "wwr", job_id, jsonld_page(GOOD_JD))

    with session_scope(cfg.db_path) as session:
        result = ladder.run(cfg, session, session.get(Job, job_id))
        assert result.rung == "jsonld"
        assert result.completeness == "full"
        assert result.origin == "cache"

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.jd_source == "jsonld"
        assert job.jd_completeness == "full"
        assert "ingestion pipeline" in job.description_text


def test_a_json_ld_stub_falls_through_to_readability(cfg) -> None:
    """The whole reason the gate sits between the rungs."""
    job_id = make_job(cfg, description_text="Short teaser.", jd_completeness="snippet")
    page = jsonld_page("Apply now.").replace(
        "<p>ignored</p>", f"<article><h1>Role</h1><p>{GOOD_JD}</p></article>"
    )
    place_detail(cfg, "wwr", job_id, page)

    with session_scope(cfg.db_path) as session:
        result = ladder.run(cfg, session, session.get(Job, job_id))
    assert result.rung == "readability"
    assert result.completeness == "full"
    assert any("jsonld" in reason for reason in result.reasons) or result.reasons == []


def test_a_job_with_no_url_reports_why_instead_of_fetching(cfg) -> None:
    job_id = make_job(cfg, source_url=None, apply_url=None, description_text="teaser")
    with session_scope(cfg.db_path) as session:
        result = ladder.run(cfg, session, session.get(Job, job_id))
    assert result.rung is None
    assert "no detail url" in result.reasons


def test_paste_is_trusted_even_when_the_heuristics_grumble(cfg) -> None:
    """A human chose this text for a job they are about to apply to."""
    job_id = make_job(cfg, description_text="teaser", jd_completeness="snippet")
    with session_scope(cfg.db_path) as session:
        result = ladder.paste(session, session.get(Job, job_id), GOOD_JD)
        assert result.rung == "manual_paste"

    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        assert job.jd_completeness == "full"
        assert job.jd_source == "manual_paste"


def test_an_empty_paste_changes_nothing(cfg) -> None:
    job_id = make_job(cfg, description_text="teaser", jd_completeness="snippet")
    with session_scope(cfg.db_path) as session:
        result = ladder.paste(session, session.get(Job, job_id), "   ")
    assert result.reasons == ["empty paste"]
    with session_scope(cfg.db_path) as session:
        assert session.get(Job, job_id).jd_completeness == "snippet"


def test_detail_html_is_cached_so_a_parser_fix_costs_no_request(cfg) -> None:
    job_id = make_job(cfg, description_text="teaser", jd_completeness="snippet")
    place_detail(cfg, "wwr", job_id, jsonld_page(GOOD_JD))
    path = fetcher.cache_path(cfg, "wwr", job_id)
    assert path.exists()

    html, origin = fetcher.fetch(cfg, "wwr", job_id, "https://example.com/never-called")
    assert origin == "cache"
    assert "JobPosting" in html
