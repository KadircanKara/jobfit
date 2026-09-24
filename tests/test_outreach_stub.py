"""The provider that sends nothing.

Every send in phase 1 lands here. It has to be honest about that: a ref that says
`stub:` and a record on the row, so a database inspected later can never be
mistaken for one where messages actually went out.
"""
from __future__ import annotations

from jobhunt.db.models import Contact
from jobhunt.outreach import discovery, stub


def contact(**kwargs) -> Contact:
    base = {"id": 1, "full_name": "Deniz Aksoy", "origin": "manual"}
    base.update(kwargs)
    return Contact(**base)


def test_status_comes_off_the_contact_row(cfg):
    person = contact(is_connection=True, can_send_inmail=False)
    status = stub.StubProvider(cfg).status(person)
    assert status.is_connection is True
    assert status.can_send_inmail is False


def test_unchecked_status_stays_unknown(cfg):
    status = stub.StubProvider(cfg).status(contact())
    assert status.is_connection is None
    assert status.can_send_inmail is None


def test_credits_come_from_config(cfg):
    cfg.raw["outreach"]["inmail_credits"] = 7
    assert stub.StubProvider(cfg).credits() == 7


def test_every_send_succeeds_and_is_marked_as_a_stub(cfg):
    sender = stub.StubProvider(cfg)
    for result in (
        sender.send_dm(contact(), "hello"),
        sender.send_inmail(contact(), "hello", paid=False),
        sender.send_invite(contact(), "hello"),
    ):
        assert result.ok
        assert result.ref.startswith("stub:")


def test_an_invite_is_not_accepted_until_it_is_accepted(cfg):
    sender = stub.StubProvider(cfg)
    person = contact()
    assert sender.invite_accepted(person) is False
    sender.accept(person.id)
    assert sender.invite_accepted(person) is True


def test_discovery_finds_nothing_this_phase(cfg):
    assert discovery.find_contacts(None) == []
