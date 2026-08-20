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


def test_parse_datetime_accepts_millisecond_epochs() -> None:
    """Lever sends createdAt in milliseconds. Seconds would land in the year 58000."""
    assert norm.parse_datetime(1787203369315).year == 2026
    assert norm.parse_datetime(1787203369).year == 2026


# --- employment type ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FullTime", "full_time"),                    # ashby
        ("Contract", "contract"),                     # lever
        ("fulltime_permanent", "full_time"),          # recruitee
        ("permanent", "full_time"),                   # personio employmentType
        ("full-time", "full_time"),                   # personio schedule
        ("Full-time", "full_time"),                   # smartrecruiters, workable
        ("full_time", "full_time"),                   # remotive
        (["Full-Time"], "full_time"),                 # jobicy, a list
        ("Full-Time", "full_time"),                   # wwr rss
        ("Part-time", "part_time"),
        ("Internship", "internship"),
        ("Working Student", "internship"),
        ("Freelance", "contract"),
        ("Fixed term", "temporary"),
        ("Stajyer", "internship"),
        ("Yarı zamanlı", "part_time"),
    ],
)
def test_employment_type_maps_every_live_wire_value(raw, expected) -> None:
    assert norm.normalize_employment_type(raw) == expected


def test_employment_type_is_none_when_unstated() -> None:
    """Greenhouse and RemoteOK do not state it. Guessing would let the filter
    drop jobs for a value nobody published."""
    assert norm.normalize_employment_type(None) is None
    assert norm.normalize_employment_type("") is None
    assert norm.normalize_employment_type([]) is None
    assert norm.normalize_employment_type("Engineering") is None


def test_employment_type_reads_several_fields() -> None:
    """Personio splits it across employmentType and schedule."""
    assert norm.normalize_employment_type(None, "permanent") == "full_time"
    assert norm.normalize_employment_type("part-time", "permanent") == "part_time"


def test_an_internship_is_not_read_as_full_time() -> None:
    """"Internship, full time" is an internship. Order matters here."""
    assert norm.normalize_employment_type("Internship, full time") == "internship"


def test_a_country_in_parentheses_is_not_thrown_away() -> None:
    """Found live: "Remote (United States)" parsed to country None, so the
    timezone rule skipped it, and US-only remote listings passed stage 1. That
    rule is the main thing keeping US-anchored postings out."""
    country, _, remote = norm.parse_location("Remote (United States)")
    assert country == "US"
    assert remote == "remote"


def test_a_work_mode_parenthetical_is_still_stripped() -> None:
    country, city, remote = norm.parse_location("Berlin, Germany (Hybrid)")
    assert (country, city, remote) == ("DE", "Berlin", "hybrid")


def test_a_us_state_in_parentheses_resolves_too() -> None:
    assert norm.parse_location("Remote (CA)")[0] == "US"


def test_a_bare_us_state_name_resolves_to_the_us() -> None:
    """Found live: We Work Remotely spells the location "Colorado", which parsed
    to an unknown country, so a US-only job passed a GMT+3 overlap check."""
    assert norm.parse_location("Colorado")[0] == "US"
    assert norm.parse_location("Austin, Texas")[0] == "US"
    assert norm.parse_location("Remote - California")[0] == "US"


def test_ambiguous_state_names_are_left_alone() -> None:
    """Georgia is also a country and Washington is also a city. A wrong country
    is worse than an unknown one."""
    assert norm.parse_location("Georgia")[0] != "US"
    assert norm.parse_location("Washington")[0] is None


def test_a_spaced_hyphen_separates_but_a_word_hyphen_does_not() -> None:
    assert norm.parse_location("Remote - Bangalore")[1] == "Bangalore"
    assert norm.parse_location("Saint-Denis, France") == ("FR", "Saint-Denis", "unknown")
