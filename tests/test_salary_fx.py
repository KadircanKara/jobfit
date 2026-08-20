"""The salary floor across currencies.

The rule deliberately had no FX table: a rate hardcoded once is wrong later and
would silently drop jobs. A rate snapshot fetched for the run fixes the staleness
without reintroducing the hazard, and when no snapshot exists the old behaviour
has to survive exactly — a figure in another currency stays unknown, and unknown
is never a rejection.
"""
from __future__ import annotations

from jobhunt.db.models import Job
from jobhunt.db.session import session_scope
from jobhunt.rank import deterministic
from tests.test_rank import make_job

RATES = {"USD": 1.0, "EUR": 0.918, "TRY": 48.02}


def profile(rates=None):
    salary = {"min_annual": 100000, "currency": "USD", "include_unstated": True}
    if rates is not None:
        salary["rates"] = rates
    return {"profiles": {"global_remote": {"salary": salary}}}


def verdict(cfg, job_id, filters):
    with session_scope(cfg.db_path) as session:
        job = session.get(Job, job_id)
        return deterministic.evaluate(job, job.company, filters)


def test_a_euro_salary_clearing_the_floor_passes(cfg):
    """100k EUR is about 109k USD, so it clears a 100k USD floor."""
    job_id = make_job(
        cfg, salary_min=100000, salary_max=100000, salary_currency="EUR",
        salary_period="annual", salary_is_stated=True,
    )

    assert not any("salary" in reason for reason in verdict(cfg, job_id, profile(RATES)).reasons)


def test_a_lira_salary_below_the_floor_is_rejected_in_the_users_currency(cfg):
    """1,000,000 TRY is about 21k USD."""
    job_id = make_job(
        cfg, salary_min=1000000, salary_max=1000000, salary_currency="TRY",
        salary_period="annual", salary_is_stated=True,
    )

    assert any("salary" in reason for reason in verdict(cfg, job_id, profile(RATES)).reasons)


def test_a_lira_salary_above_the_floor_passes(cfg):
    """6,000,000 TRY is about 125k USD."""
    job_id = make_job(
        cfg, salary_min=6000000, salary_max=6000000, salary_currency="TRY",
        salary_period="annual", salary_is_stated=True,
    )

    assert not any("salary" in reason for reason in verdict(cfg, job_id, profile(RATES)).reasons)


def test_without_rates_another_currency_stays_unknown(cfg):
    """The old behaviour, unchanged: unknown is not a rejection."""
    job_id = make_job(
        cfg, salary_min=1000, salary_max=1000, salary_currency="TRY",
        salary_period="annual", salary_is_stated=True,
    )

    assert not any("salary" in reason for reason in verdict(cfg, job_id, profile()).reasons)


def test_a_currency_missing_from_the_snapshot_stays_unknown(cfg):
    job_id = make_job(
        cfg, salary_min=1000, salary_max=1000, salary_currency="ZWL",
        salary_period="annual", salary_is_stated=True,
    )

    assert not any("salary" in reason for reason in verdict(cfg, job_id, profile(RATES)).reasons)


def test_the_rejection_reason_names_both_figures(cfg):
    job_id = make_job(
        cfg, salary_min=1000000, salary_max=1000000, salary_currency="TRY",
        salary_period="annual", salary_is_stated=True,
    )

    reasons = " ".join(verdict(cfg, job_id, profile(RATES)).reasons)

    assert "TRY" in reasons and "USD" in reasons


def test_same_currency_still_compares_directly(cfg):
    job_id = make_job(
        cfg, salary_min=50000, salary_max=70000, salary_currency="USD",
        salary_period="annual", salary_is_stated=True,
    )

    assert any("salary" in reason for reason in verdict(cfg, job_id, profile(RATES)).reasons)


def test_a_run_can_supply_the_rate_snapshot_it_fetched(cfg):
    """The snapshot belongs to the run, not to filters.yaml on disk."""
    from jobhunt.rank import runner

    make_job(
        cfg, salary_min=1000000, salary_max=1000000, salary_currency="TRY",
        salary_period="annual", salary_is_stated=True,
    )

    result = runner.run_deterministic(cfg, rates=RATES)

    assert result.scored >= 1
