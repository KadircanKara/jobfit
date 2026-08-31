"""Discovery of the stated contact: whoever the posting itself names."""
from __future__ import annotations

from jobhunt.db.models import Job
from jobhunt.db.session import session_scope
from jobhunt.outreach import discovery
from jobhunt.sources.base import JobPosting
from jobhunt.store import upsert_posting


def test_a_job_with_a_named_poster_yields_one_candidate(cfg) -> None:
    with session_scope(cfg.db_path) as session:
        job = Job(
            source="linkedin", external_id="p1", market="global_remote",
            title="Backend Engineer", title_normalized="backend engineer",
            poster_name="Jane Doe",
            poster_profile_url="https://www.linkedin.com/in/jane-doe-1234",
        )
        session.add(job)
        session.flush()
        found = discovery.find_contacts(job)
    assert [c.full_name for c in found] == ["Jane Doe"]
    assert found[0].origin == "job_poster"
    assert found[0].profile_url == "https://www.linkedin.com/in/jane-doe-1234"


def test_a_job_with_no_poster_yields_nothing(cfg) -> None:
    with session_scope(cfg.db_path) as session:
        job = Job(
            source="ashby", external_id="p2", market="global_remote",
            title="X", title_normalized="x",
        )
        session.add(job)
        session.flush()
        assert discovery.find_contacts(job) == []


def test_no_job_yields_nothing() -> None:
    assert discovery.find_contacts(None) == []


def test_upsert_posting_carries_the_poster_into_the_database(cfg) -> None:
    posting = JobPosting(
        source="linkedin",
        external_id="li-1",
        market="global_remote",
        company_name="Acme",
        title="Backend Engineer",
        location_raw=None,
        poster_name="Jane Doe",
        poster_profile_url="https://www.linkedin.com/in/jane-doe-1234",
    )
    with session_scope(cfg.db_path) as session:
        job, outcome = upsert_posting(session, posting)
        session.flush()
        assert outcome == "new"
        assert job.poster_name == "Jane Doe"
        assert job.poster_profile_url == "https://www.linkedin.com/in/jane-doe-1234"
