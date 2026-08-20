"""Strategy A end to end, against rows placed by hand. No network.

The point of these tests is the claim in PLAN.md 3.5: after one ingestion pass,
the system knows about boards nothing ever told it about.
"""
from __future__ import annotations

from jobhunt import store
from jobhunt.db.models import Board, Company
from jobhunt.db.session import session_scope
from jobhunt.discovery import harvest
from jobhunt.sources.base import JobPosting


def add_job(cfg, **kwargs) -> int:
    defaults = {
        "source": "himalayas",
        "external_id": "x1",
        "market": "global_remote",
        "title": "Senior Backend Engineer",
        "company_name": "Acme",
    }
    defaults.update(kwargs)
    with session_scope(cfg.db_path) as session:
        job, _ = store.upsert_posting(session, JobPosting(**defaults))
        session.flush()
        return job.id


def boards_of(cfg) -> dict[tuple[str, str], Board]:
    with session_scope(cfg.db_path) as session:
        return {
            (b.provider, b.token): b
            for b in session.query(Board).all()
        }


def test_apply_url_becomes_a_fetchable_board(cfg) -> None:
    add_job(cfg, apply_url="https://jobs.ashbyhq.com/parabola/8f2c-1111")
    result = harvest.harvest(cfg)

    assert result.scanned == 1
    assert result.new_boards == 1
    board = boards_of(cfg)[("ashby", "parabola")]
    assert board.status == "candidate"
    assert board.discovered_via == "apply_url"
    # NULL next_fetch_at is what makes the next sync pick it up first.
    assert board.next_fetch_at is None


def test_board_is_attributed_to_the_jobs_company(cfg) -> None:
    add_job(cfg, company_name="Parabola", apply_url="https://jobs.ashbyhq.com/parabola/1")
    harvest.harvest(cfg)

    with session_scope(cfg.db_path) as session:
        board = session.query(Board).filter_by(token="parabola").one()
        company = session.get(Company, board.company_id)
        assert company.name == "Parabola"


def test_a_link_in_the_body_is_not_attributed_to_the_company(cfg) -> None:
    """A "see also" link in a description belongs to someone else's company."""
    add_job(
        cfg,
        company_name="Acme",
        description_html='<p>Also see <a href="https://jobs.lever.co/plaid/1">Plaid</a></p>',
    )
    harvest.harvest(cfg)

    board = boards_of(cfg)[("lever", "plaid")]
    assert board.company_id is None


def test_employer_hosted_url_learns_the_domain_and_guesses_a_token(cfg) -> None:
    add_job(
        cfg,
        company_name="Stripe",
        apply_url="https://stripe.com/jobs/listing/eng/12?gh_jid=5551212",
    )
    result = harvest.harvest(cfg)

    assert result.domains_learned == 1
    assert result.guessed_boards == 1
    with session_scope(cfg.db_path) as session:
        assert session.query(Company).filter_by(name="Stripe").one().domain == "stripe.com"
    board = boards_of(cfg)[("greenhouse", "stripe")]
    assert board.discovered_via == "domain_guess"


def test_guessing_can_be_turned_off(cfg) -> None:
    add_job(cfg, apply_url="https://acme.io/careers?gh_jid=1")
    result = harvest.harvest(cfg, guess_from_hints=False)
    assert result.guessed_boards == 0
    assert boards_of(cfg) == {}


def test_a_known_domain_is_never_overwritten(cfg) -> None:
    add_job(
        cfg,
        company_name="Acme",
        company_domain="acme.com",
        apply_url="https://jobs-portal.example.org/x?gh_jid=1",
    )
    harvest.harvest(cfg)
    with session_scope(cfg.db_path) as session:
        assert session.query(Company).one().domain == "acme.com"


def test_harvest_is_incremental_and_idempotent(cfg) -> None:
    add_job(cfg, external_id="a", apply_url="https://jobs.lever.co/plaid/1")
    first = harvest.harvest(cfg)
    assert first.scanned == 1 and first.new_boards == 1

    second = harvest.harvest(cfg)
    assert second.scanned == 0 and second.new_boards == 0

    add_job(cfg, external_id="b", apply_url="https://jobs.lever.co/brex/2")
    third = harvest.harvest(cfg)
    assert third.scanned == 1
    assert third.new_boards == 1
    assert set(boards_of(cfg)) == {("lever", "plaid"), ("lever", "brex")}


def test_full_rescan_ignores_the_watermark(cfg) -> None:
    add_job(cfg, apply_url="https://jobs.lever.co/plaid/1")
    harvest.harvest(cfg)
    again = harvest.harvest(cfg, full=True)
    assert again.scanned == 1
    assert again.new_boards == 0
    assert again.known_boards >= 1


def test_dry_run_writes_nothing(cfg) -> None:
    add_job(cfg, apply_url="https://jobs.lever.co/plaid/1")
    result = harvest.harvest(cfg, dry_run=True)
    assert result.new_boards == 1
    assert boards_of(cfg) == {}
    # The watermark must not move either, or the real run would skip the row.
    assert harvest.harvest(cfg).new_boards == 1


def test_provider_without_an_adapter_is_still_recorded(cfg) -> None:
    add_job(cfg, apply_url="https://someco.teamtailor.com/jobs/1")
    harvest.harvest(cfg)
    board = boards_of(cfg)[("teamtailor", "someco")]
    assert board.notes == harvest.UNFETCHABLE_NOTE


def test_report_is_written_to_disk_not_the_terminal(cfg) -> None:
    add_job(cfg, apply_url="https://jobs.lever.co/plaid/1")
    result = harvest.harvest(cfg)
    path = harvest.write_report(cfg, result)
    assert path.exists()
    assert "new_boards" in path.read_text(encoding="utf-8")
