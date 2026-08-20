from __future__ import annotations

import datetime as dt

import pytest

from jobhunt.pipeline import normalize as norm


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Acme, Inc.", "acme"),
        ("ACME LLC", "acme"),
        ("Açme Technologies", "acme technologies"),
        ("Stripe", "stripe"),
    ],
)
def test_normalize_company_name(raw: str, expected: str) -> None:
    assert norm.normalize_company_name(raw) == expected


def test_company_name_keeps_load_bearing_words() -> None:
    """Labs and Group are parts of names, not legal suffixes. Stripping them
    would collide two different companies."""
    assert norm.normalize_company_name("Acme Labs") != norm.normalize_company_name("Acme Group")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Backend Engineer (Remote)", "backend engineer"),
        ("Backend Engineer, Remote", "backend engineer"),
        ("Staff Backend Engineer", "backend engineer"),
        ("Sr. Backend Engineer (m/f/d)", "backend engineer"),
    ],
)
def test_normalize_title_collapses_seniority_and_mode(raw: str, expected: str) -> None:
    assert norm.normalize_title(raw) == expected


def test_normalize_title_keeps_distinct_roles_distinct() -> None:
    assert norm.normalize_title("Senior Backend Engineer") != norm.normalize_title(
        "Senior Frontend Engineer"
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Backend Engineer", "senior"),
        ("Staff Software Engineer", "staff"),
        ("Founding Engineer", "founding"),
        ("Principal Engineer", "principal"),
        ("Backend Engineering Intern", "intern"),
        ("Backend Engineer", None),
        ("Engineering Manager", "lead"),
        ("Senior Manager, Global Equity", "senior"),
    ],
)
def test_detect_seniority(title: str, expected: str | None) -> None:
    assert norm.detect_seniority(title) == expected


def test_seniority_prefers_the_more_specific_match() -> None:
    """'Senior Staff Engineer' is staff, not senior."""
    assert norm.detect_seniority("Senior Staff Engineer") == "staff"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Machine Learning Engineer", "ai_ml"),
        ("Data Engineer", "data"),
        ("Site Reliability Engineer", "devops"),
        ("Full Stack Developer", "fullstack"),
        ("Senior Backend Engineer", "backend"),
        ("Product Manager", "pm"),
        ("Office Administrator", "other"),
    ],
)
def test_detect_role_family(title: str, expected: str) -> None:
    assert norm.detect_role_family(title) == expected


def test_unescape_only_when_escaped() -> None:
    """Greenhouse double-encodes; Ashby does not. Unescaping real HTML twice
    would corrupt literal entities."""
    assert norm.unescape_if_escaped("&lt;p&gt;hello&lt;/p&gt;") == "<p>hello</p>"
    assert norm.unescape_if_escaped("<p>a &amp; b</p>") == "<p>a &amp; b</p>"


def test_html_to_markdown_strips_scripts_and_em_dashes() -> None:
    md = norm.html_to_markdown("<h2>Role</h2><script>bad()</script><p>Build things, fast</p>")
    assert "bad()" not in md
    assert "## Role" in md
    assert "—" not in md


@pytest.mark.parametrize(
    ("raw", "country", "remote"),
    [
        ("New York, NY", "US", "unknown"),
        ("Istanbul, Turkey", "TR", "unknown"),
        ("Remote - Europe", None, "remote"),
        ("Berlin, Germany (Hybrid)", "DE", "hybrid"),
        (None, None, "unknown"),
    ],
)
def test_parse_location(raw: str | None, country: str | None, remote: str) -> None:
    got_country, _city, got_remote = norm.parse_location(raw)
    assert (got_country, got_remote) == (country, remote)


def test_completeness_threshold() -> None:
    assert norm.completeness(None) == "none"
    assert norm.completeness("x" * 399) == "snippet"
    assert norm.completeness("x" * 400) == "full"


def test_content_hash_ignores_whitespace_and_case() -> None:
    left = norm.content_hash("Backend Engineer", "Remote", "Build   things")
    right = norm.content_hash("backend engineer", "remote", "Build things")
    assert left == right


def test_content_hash_changes_with_content() -> None:
    left = norm.content_hash("Backend Engineer", "Remote", "Build things")
    right = norm.content_hash("Backend Engineer", "Remote", "Build other things")
    assert left != right


def test_parse_datetime_normalizes_to_naive_utc() -> None:
    parsed = norm.parse_datetime("2026-08-19T14:02:07-04:00")
    assert parsed == dt.datetime(2026, 8, 19, 18, 2, 7)
    assert parsed.tzinfo is None


def test_domain_from_url_rejects_ats_hosts() -> None:
    assert norm.domain_from_url("https://stripe.com/jobs?gh_jid=1") == "stripe.com"
    assert norm.domain_from_url("https://jobs.ashbyhq.com/ramp/uuid") is None
    assert norm.domain_from_url("https://boards.greenhouse.io/acme/jobs/1") is None
    assert norm.domain_from_url(None) is None
