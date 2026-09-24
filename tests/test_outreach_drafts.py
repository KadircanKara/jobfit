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


def _upwork_job(title: str = "Senior AI Engineer for RAG platform") -> Job:
    return Job(
        external_id="upwork-1",
        source="upwork",
        market="global_remote",
        title=title,
        title_normalized=title.lower(),
    )


def test_an_upwork_gig_gets_its_own_opening(cfg) -> None:
    body = drafts.template(_upwork_job(), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert "Oliver" not in body  # sanity: contact() is Deniz, not the brief's example name
    assert "Deniz" in body
    assert "Senior AI Engineer for RAG platform" in body
    assert "Upwork" in body


def test_a_salaried_role_keeps_the_existing_opening(cfg) -> None:
    body = drafts.template(job(), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert "Upwork" not in body


def test_the_rate_line_comes_from_config(cfg) -> None:
    cfg.raw.setdefault("outreach", {})["rate_line"] = "$45/hour"
    body = drafts.template(_upwork_job("AI Engineer"), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert "$45/hour" in body


def test_the_rate_line_can_be_turned_off(cfg) -> None:
    cfg.raw.setdefault("outreach", {})["rate_line"] = ""
    body = drafts.template(_upwork_job("AI Engineer"), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert "/hour" not in body


def test_the_rate_line_defaults_on_without_a_config(cfg) -> None:
    body = drafts.template(_upwork_job("AI Engineer"), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert "$30/hour" in body


def test_the_upwork_pitch_fits_a_dm(cfg) -> None:
    body = drafts.template(_upwork_job("A" * 120), contact(), provider.INVITE_THEN_DM, config=cfg)
    assert not drafts.over_limit(body, provider.INVITE_THEN_DM)


def test_the_upwork_note_fits_a_connection_request(cfg) -> None:
    """invite_note carries a 300 character cap; a title this long forces truncation.

    120 "A"s fits inside the fixed prefix/suffix untouched - it does not exercise
    `_truncated_title` at all. 250 does, and the fixed ask must survive verbatim:
    truncating the ask instead of the title would still fit the limit while
    destroying the message.
    """
    body = drafts.template(_upwork_job("A" * 250), contact(), provider.INVITE_NOTE, config=cfg)
    assert not drafts.over_limit(body, provider.INVITE_NOTE)
    assert "would love to connect about it." in body


def test_upwork_template_without_config_still_works() -> None:
    """`config` is optional so every existing non-Upwork caller keeps working unchanged."""
    body = drafts.template(_upwork_job("AI Engineer"), contact(), provider.INVITE_THEN_DM)
    assert "Upwork" in body
    assert "$30/hour" in body
