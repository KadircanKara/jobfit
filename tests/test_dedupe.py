"""Layer 2 clustering against synthetic duplicates. PLAN.md phase 1 deliverable."""
from __future__ import annotations

import datetime as dt

from jobhunt.pipeline import simhash
from jobhunt.pipeline.dedupe import DedupeRecord, cluster

T0 = dt.datetime(2026, 8, 1, 9, 0)

BACKEND_JD = (
    "We are looking for a backend engineer to build and operate our payments API. "
    "You will design services in Python and FastAPI, own Postgres schemas, and work "
    "with Redis queues. Five years of production experience required."
)
BACKEND_JD_REWORDED = (
    "We're hiring a backend engineer to build and run our payments API. "
    "You will design services in Python and FastAPI, own Postgres schemas, and work "
    "with Redis queues. 5+ years of production experience required."
)
FRONTEND_JD = (
    "We are looking for a frontend engineer to own our design system in React. "
    "You will build accessible components, drive our TypeScript migration, and "
    "partner with design on interaction patterns. Portfolio required."
)


def h(text: str) -> str:
    return simhash.to_hex(simhash.simhash(text))


def record(job_id: int, **kwargs) -> DedupeRecord:
    defaults = {
        "company_key": "acme.com",
        "title_normalized": "backend engineer",
        "country": None,
        "city": None,
        "seniority": None,
        "description_hash": h(BACKEND_JD),
        "first_seen_at": T0 + dt.timedelta(minutes=job_id),
        "source": "greenhouse",
    }
    return DedupeRecord(id=job_id, **{**defaults, **kwargs})


def test_single_job_is_its_own_head() -> None:
    assert cluster([record(1)]) == {1: 1}


def test_same_role_on_three_sources_collapses_to_one_cluster() -> None:
    """The whole point: company board, Himalayas, and RemoteOK carrying one role."""
    records = [
        record(1, source="greenhouse", description_hash=h(BACKEND_JD)),
        record(2, source="himalayas", description_hash=h(BACKEND_JD_REWORDED)),
        record(3, source="remoteok", description_hash=h(BACKEND_JD)),
    ]
    assert cluster(records) == {1: 1, 2: 1, 3: 1}


def test_head_is_the_earliest_seen() -> None:
    records = [
        record(7, first_seen_at=T0 + dt.timedelta(days=3)),
        record(2, first_seen_at=T0),
        record(5, first_seen_at=T0 + dt.timedelta(days=1)),
    ]
    assert set(cluster(records).values()) == {2}


def test_same_title_at_different_companies_never_merges() -> None:
    """Never dedupe on title alone. PLAN.md section 5."""
    records = [record(1, company_key="acme.com"), record(2, company_key="globex.com")]
    assert cluster(records) == {1: 1, 2: 2}


def test_different_roles_at_one_company_stay_separate() -> None:
    records = [
        record(1, title_normalized="backend engineer", description_hash=h(BACKEND_JD)),
        record(2, title_normalized="frontend engineer", description_hash=h(FRONTEND_JD)),
    ]
    assert cluster(records) == {1: 1, 2: 2}


def test_same_normalized_title_but_unrelated_descriptions_stay_separate() -> None:
    """Grouping key collides, simhash breaks the tie. Two different backend roles
    on one board are two jobs."""
    records = [
        record(1, description_hash=h(BACKEND_JD)),
        record(2, description_hash=h(FRONTEND_JD)),
    ]
    assert cluster(records) == {1: 1, 2: 2}


def test_conflicting_countries_never_merge() -> None:
    """Same role advertised in two countries is two jobs the user must choose between."""
    records = [record(1, country="US"), record(2, country="DE")]
    assert cluster(records) == {1: 1, 2: 2}


def test_unknown_country_merges_with_a_known_one() -> None:
    """Aggregators routinely report no country for a job the company board places
    precisely. Splitting on that is the failure clustering exists to prevent."""
    records = [record(1, country="US"), record(2, country=None, source="himalayas")]
    assert cluster(records) == {1: 1, 2: 1}


def test_missing_descriptions_fall_back_to_the_grouping_key() -> None:
    records = [record(1, description_hash=None), record(2, description_hash=None)]
    assert cluster(records) == {1: 1, 2: 1}


def test_seniority_variants_merge_when_one_side_is_unknown() -> None:
    """title_normalized strips seniority, so the two titles land in one group.
    They merge only because the aggregator's row states no level."""
    from jobhunt.pipeline.normalize import normalize_title

    assert normalize_title("Senior Backend Engineer") == normalize_title("Backend Engineer")
    records = [
        record(1, title_normalized=normalize_title("Senior Backend Engineer"), seniority="senior"),
        record(2, title_normalized=normalize_title("Backend Engineer (Remote)"), seniority=None),
    ]
    assert cluster(records) == {1: 1, 2: 1}


def test_different_levels_of_one_role_never_merge() -> None:
    """Manager and Senior Manager at one company are two jobs. title_normalized
    strips the level, so seniority has to carry the distinction."""
    records = [
        record(1, title_normalized="manager global equity administration", seniority="lead"),
        record(2, title_normalized="manager global equity administration", seniority="senior"),
    ]
    assert cluster(records) == {1: 1, 2: 2}


def test_same_role_in_two_cities_never_merges() -> None:
    """A San Francisco posting and a Seoul posting are two jobs, and for a
    candidate in Istanbul the difference is the whole decision."""
    records = [record(1, city="San Francisco"), record(2, city="Seoul")]
    assert cluster(records) == {1: 1, 2: 2}


def test_city_spelling_variants_still_merge() -> None:
    """"New York" and "New York City" are one place, and two sources will spell
    it both ways for the same posting."""
    records = [record(1, city="New York"), record(2, city="New York City", source="himalayas")]
    assert cluster(records) == {1: 1, 2: 1}


def test_unknown_city_merges_with_a_known_one() -> None:
    records = [record(1, city="Berlin"), record(2, city=None, source="remoteok")]
    assert cluster(records) == {1: 1, 2: 1}


def test_clustering_is_stable_across_input_order() -> None:
    records = [record(1), record(2), record(3)]
    assert cluster(records) == cluster(list(reversed(records)))


def test_three_way_split_keeps_three_heads() -> None:
    records = [
        record(1, country="US", description_hash=h(BACKEND_JD)),
        record(2, country="US", description_hash=h(FRONTEND_JD)),
        record(3, country="DE", description_hash=h(BACKEND_JD)),
    ]
    result = cluster(records)
    assert result[1] == 1
    assert result[2] == 2
    assert result[3] == 3
