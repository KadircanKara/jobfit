import datetime as dt

from jobhunt.db.models import utcnow
from jobhunt.sources.upwork_guard import DAILY_REFS, FetchBudget


def test_a_fresh_day_allows_a_fetch(cfg) -> None:
    assert FetchBudget(cfg).allow() is True


def test_the_daily_ref_budget_is_finite(cfg) -> None:
    budget = FetchBudget(cfg)
    budget.spend(DAILY_REFS)
    assert budget.allow() is False


def test_the_budget_survives_a_restart(cfg) -> None:
    FetchBudget(cfg).spend(DAILY_REFS)
    assert FetchBudget(cfg).allow() is False


def test_the_budget_resets_the_next_day(cfg) -> None:
    budget = FetchBudget(cfg)
    budget.spend(DAILY_REFS)
    assert budget.allow(now=utcnow() + dt.timedelta(days=1)) is True


def test_three_failures_trip_the_breaker(cfg) -> None:
    budget = FetchBudget(cfg)
    for _ in range(3):
        budget.record_failure()
    assert budget.tripped is True
    assert budget.allow() is False


def test_a_success_clears_the_failure_streak(cfg) -> None:
    budget = FetchBudget(cfg)
    budget.record_failure()
    budget.record_failure()
    budget.record_ok()
    budget.record_failure()
    assert budget.tripped is False


def test_the_refusal_says_which_limit_stopped_it(cfg) -> None:
    budget = FetchBudget(cfg)
    assert budget.refusal() is None
    budget.spend(DAILY_REFS)
    assert "budget" in (budget.refusal() or "")


def test_the_daily_budget_fits_one_located_run_with_every_search_retried() -> None:
    """The user's own setup on 2026-09-23: five queries plus the feed, both job
    types, the United States and Canada. `fetch` spends one unit per attempt
    and may make two, so a run needs twice its search count."""
    from jobhunt.preferences import UpworkPreferences
    from jobhunt.sources import upwork_query

    prefs = UpworkPreferences(
        queries=["AI Agent", "LLM", "Generative AI", "RAG", "LangChain"],
        client_locations=["United States", "Canada"], recommended_feed=True,
    )
    searches = len(upwork_query.refs_for(prefs))
    assert searches == 24
    assert DAILY_REFS >= 2 * searches
