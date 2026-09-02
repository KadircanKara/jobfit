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


class FakeSearch:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def search_people(self, company, keywords, *, limit=5, location=None):
        self.calls += 1
        return self.items


def test_company_search_returns_candidates(cfg) -> None:
    client = FakeSearch([
        {"name": "Jane Doe", "headline": "Talent Partner", "profile_url": "https://www.linkedin.com/in/jane"},
    ])
    found = discovery.search_company(client, "Acme", config=cfg)
    assert [c.full_name for c in found] == ["Jane Doe"]
    assert found[0].origin == "company_search"


def test_a_repeat_search_within_a_day_is_served_from_cache(cfg) -> None:
    client = FakeSearch([{"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/jane"}])
    discovery.search_company(client, "Acme", config=cfg)
    discovery.search_company(client, "Acme", config=cfg)
    assert client.calls == 1


def test_the_cache_expires_after_a_day(cfg) -> None:
    import datetime as dt

    from jobhunt.db.models import utcnow

    client = FakeSearch([{"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/jane"}])
    discovery.search_company(client, "Acme", config=cfg)
    discovery.search_company(client, "Acme", config=cfg, now=utcnow() + dt.timedelta(hours=25))
    assert client.calls == 2


def test_a_candidate_without_a_name_is_skipped(cfg) -> None:
    client = FakeSearch([{"headline": "Recruiter"}])
    assert discovery.search_company(client, "Acme", config=cfg) == []


def test_a_differently_cased_company_name_shares_the_cache(cfg) -> None:
    client = FakeSearch([{"name": "Jane Doe", "profile_url": "https://www.linkedin.com/in/jane"}])
    discovery.search_company(client, "Acme", config=cfg)
    discovery.search_company(client, "  ACME  ", config=cfg)
    assert client.calls == 1


def test_a_malformed_item_is_skipped_not_raised(cfg) -> None:
    client = FakeSearch(["not-a-dict", {"name": "Jane Doe", "profile_url": None}])
    found = discovery.search_company(client, "Acme", config=cfg)
    assert [c.full_name for c in found] == ["Jane Doe"]


def test_a_non_string_headline_or_profile_url_is_dropped_not_raised(cfg) -> None:
    client = FakeSearch([{"name": "Jane Doe", "headline": {"nested": True}, "profile_url": ["x"]}])
    found = discovery.search_company(client, "Acme", config=cfg)
    assert found[0].headline is None
    assert found[0].profile_url is None


def test_an_empty_result_is_still_cached(cfg) -> None:
    client = FakeSearch([])
    assert discovery.search_company(client, "Acme", config=cfg) == []
    assert discovery.search_company(client, "Acme", config=cfg) == []
    assert client.calls == 1


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


def test_candidates_naming_the_company_rank_above_ones_that_do_not(cfg) -> None:
    """The live California search for "Nexora" returned, in LinkedIn's order:
    a CFO at Nexora Solutions, the founder of Nexora AI, an unrelated founder in
    India, and an Apple engineer. Whoever actually works at the company belongs
    at the top of the drawer, not wherever LinkedIn happened to put them."""
    class Client:
        def search_people(self, company, keywords, *, limit=5, location=None):
            return [
                {"name": "Nithya Subramanian", "headline": "Engineering @ Apple"},
                {"name": "Dan Collins", "headline": "VP of Talent Acquisition"},
                {"name": "Ambrin Maria", "headline": "Founder & CEO at Nexora AI"},
            ]

    got = [c.full_name for c in discovery.search_company(Client(), "Nexora", config=cfg)]
    assert got[0] == "Ambrin Maria"


def test_a_role_headline_outranks_an_unrelated_one(cfg) -> None:
    """Among candidates who all fail to name the company, the one whose job is
    to hire is still the better guess than one whose headline says nothing."""
    class Client:
        def search_people(self, company, keywords, *, limit=5, location=None):
            return [
                {"name": "Jade Sizemore", "headline": "B2B Regional Support Specialist"},
                {"name": "Dan Collins", "headline": "VP of Talent Acquisition"},
            ]

    got = [c.full_name for c in discovery.search_company(Client(), "Nexora", config=cfg)]
    assert got[0] == "Dan Collins"


def test_ranking_never_drops_anyone(cfg) -> None:
    class Client:
        def search_people(self, company, keywords, *, limit=5, location=None):
            return [{"name": "A", "headline": None}, {"name": "B", "headline": "CTO at Nexora"}]

    assert len(discovery.search_company(Client(), "Nexora", config=cfg)) == 2


def test_the_query_asks_for_fewer_roles_than_ranking_rewards(cfg) -> None:
    """OR-joining all twelve ranking roles (183 chars) returned zero results
    live, where six returned ten - the length limit fails silently, as an
    empty result set rather than an error."""
    seen = {}

    class Client:
        def search_people(self, company, keywords, *, limit=5, location=None):
            seen["keywords"] = keywords
            return []

    discovery.search_company(Client(), "Acme", config=cfg)
    assert seen["keywords"] == list(discovery.QUERY_ROLES)
    assert len(seen["keywords"]) < len(discovery.ROLE_KEYWORDS)


def test_a_founder_headline_ranks_even_though_it_is_not_in_the_query(cfg) -> None:
    class Client:
        def search_people(self, company, keywords, *, limit=5, location=None):
            return [{"name": "A", "headline": "Retail Guru"},
                    {"name": "B", "headline": "Head of Data at Acme"}]

    assert discovery.search_company(Client(), "Acme", config=cfg)[0].full_name == "B"
