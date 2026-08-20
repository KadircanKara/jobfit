"""Strategy B plus E: company list to candidate boards. No network.

The list is placed on disk exactly as fetch_hiring would write it, so this
exercises the parse pass in isolation, which is the point of splitting them.
"""
from __future__ import annotations

import json

from jobhunt.db.models import Board, Company
from jobhunt.db.session import session_scope
from jobhunt.discovery import yc

COMPANIES = [
    {"name": "Inkeep", "website": "https://inkeep.com", "batch": "W23",
     "slug": "inkeep", "industries": ["B2B"], "team_size": 12},
    {"name": "Canary Technologies", "website": "https://www.canarytechnologies.com/",
     "batch": "W19", "slug": "canary", "industries": ["Travel"], "team_size": 200},
    {"name": "No Website Co", "website": "", "batch": "S20", "slug": "nws"},
]


def place_list(cfg, companies=COMPANIES):
    path = cfg.raw_dir / "yc" / "R1" / "hiring.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(companies), encoding="utf-8")
    return path


def test_seed_creates_companies_and_one_candidate_per_provider(cfg) -> None:
    result = yc.seed(cfg, place_list(cfg))

    assert result.companies == 3
    assert result.with_website == 2
    assert result.new_companies == 2
    assert result.new_boards == 2 * len(yc.GUESS_PROVIDERS)

    with session_scope(cfg.db_path) as session:
        boards = session.query(Board).all()
        assert {b.token for b in boards} == {"inkeep", "canarytechnologies"}
        assert {b.provider for b in boards} == set(yc.GUESS_PROVIDERS)
        assert all(b.status == "candidate" for b in boards)
        assert all(b.discovered_via == "yc" for b in boards)
        # NULL next_fetch_at is what makes the next sync validate them.
        assert all(b.next_fetch_at is None for b in boards)
        assert all(b.company_id is not None for b in boards)


def test_yc_metadata_lands_on_the_company(cfg) -> None:
    yc.seed(cfg, place_list(cfg))
    with session_scope(cfg.db_path) as session:
        company = session.query(Company).filter_by(name="Inkeep").one()
        assert company.domain == "inkeep.com"
        assert company.yc_batch == "W23"
        assert company.team_size == 12


def test_seed_is_idempotent(cfg) -> None:
    path = place_list(cfg)
    yc.seed(cfg, path)
    again = yc.seed(cfg, path)
    assert again.new_boards == 0
    assert again.known_boards == 2 * len(yc.GUESS_PROVIDERS)
    with session_scope(cfg.db_path) as session:
        assert session.query(Board).count() == 2 * len(yc.GUESS_PROVIDERS)


def test_limit_caps_the_companies_seeded(cfg) -> None:
    result = yc.seed(cfg, place_list(cfg), limit=1)
    assert result.tokens_guessed == 1
    assert result.new_boards == len(yc.GUESS_PROVIDERS)


def test_dry_run_writes_nothing(cfg) -> None:
    result = yc.seed(cfg, place_list(cfg), dry_run=True)
    assert result.new_boards == 2 * len(yc.GUESS_PROVIDERS)
    with session_scope(cfg.db_path) as session:
        assert session.query(Board).count() == 0
        assert session.query(Company).count() == 0


def test_domain_of_strips_scheme_www_and_path() -> None:
    assert yc.domain_of("https://www.acme.io/careers?x=1") == "acme.io"
    assert yc.domain_of("acme.io") == "acme.io"
    assert yc.domain_of("") is None
    assert yc.domain_of("notadomain") is None


def test_a_company_already_known_by_name_is_upgraded_not_duplicated(cfg) -> None:
    """The corpus meets companies by name long before it learns their domain."""
    from jobhunt import store
    from jobhunt.sources.base import JobPosting

    with session_scope(cfg.db_path) as session:
        store.upsert_posting(
            session,
            JobPosting(source="ashby", external_id="1", market="global_remote",
                       title="Engineer", company_name="Inkeep"),
        )

    yc.seed(cfg, place_list(cfg))
    with session_scope(cfg.db_path) as session:
        rows = session.query(Company).filter_by(normalized_name="inkeep").all()
        assert len(rows) == 1
        assert rows[0].domain == "inkeep.com"


def test_yc_boards_land_in_the_yc_market(cfg) -> None:
    yc.seed(cfg, place_list(cfg))
    with session_scope(cfg.db_path) as session:
        assert {b.market for b in session.query(Board).all()} == {"yc"}


def test_the_market_can_be_overridden(cfg) -> None:
    yc.seed(cfg, place_list(cfg), market="global_remote")
    with session_scope(cfg.db_path) as session:
        assert {b.market for b in session.query(Board).all()} == {"global_remote"}


def test_feeds_register_as_boards_and_are_idempotent(cfg) -> None:
    """An aggregator goes through the same scheduler as an ATS board, so it is a
    board row. One fetch path, not two."""
    from jobhunt.discovery import feeds

    first = feeds.seed(cfg)
    assert first.new == len(feeds.FEEDS)
    assert feeds.seed(cfg).new == 0

    with session_scope(cfg.db_path) as session:
        rows = session.query(Board).all()
        assert len(rows) == len(feeds.FEEDS)
        assert all(b.discovered_via == "feed" for b in rows)
