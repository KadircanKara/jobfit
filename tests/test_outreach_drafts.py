"""The static message, and the length LinkedIn will actually accept.

The invite note is the only route with a hard ceiling worth enforcing, and it is
low enough that a message written for a DM will not fit. Length is reported, not
truncated: a note silently cut mid-sentence is worse than a refusal.
"""
from __future__ import annotations

from jobhunt.db.models import Contact, Job
from jobhunt.outreach import drafts, provider


def job() -> Job:
    return Job(
        external_id="ext-1",
        source="greenhouse",
        market="global_remote",
        title="Backend Engineer",
        title_normalized="backend engineer",
    )


def contact() -> Contact:
    return Contact(full_name="Deniz Aksoy", origin="manual")


def test_the_invite_note_limit_is_linkedins_three_hundred():
    assert drafts.limit_for(provider.INVITE_NOTE) == 300


def test_every_other_route_gets_the_long_limit():
    assert drafts.limit_for(provider.DM) > 300
    assert drafts.limit_for(provider.PAID_INMAIL) == drafts.limit_for(provider.DM)


def test_an_unchosen_route_falls_back_to_the_long_limit():
    assert drafts.limit_for(None) == drafts.limit_for(provider.DM)


def test_the_invite_note_template_fits_its_own_limit():
    body = drafts.template(job(), contact(), provider.INVITE_NOTE)
    assert len(body) <= 300
    assert not drafts.over_limit(body, provider.INVITE_NOTE)


def test_the_template_names_the_job_and_the_person():
    body = drafts.template(job(), contact(), provider.DM)
    assert "Backend Engineer" in body
    assert "Deniz" in body


def test_a_long_body_is_over_the_note_limit_but_not_the_dm_limit():
    body = "x" * 500
    assert drafts.over_limit(body, provider.INVITE_NOTE)
    assert not drafts.over_limit(body, provider.DM)
