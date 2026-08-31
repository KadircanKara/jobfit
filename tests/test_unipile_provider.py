import pytest

from jobhunt.db.models import Contact
from jobhunt.outreach import stub as stub_module
from jobhunt.outreach.unipile import UnipileProvider, build_sender


class FakeClient:
    def __init__(self, user=None):
        self.user = user or {"provider_id": "ACoAAA", "network_distance": "DISTANCE_2"}
        self.calls = []

    def get_user(self, identifier):
        self.calls.append(("get_user", identifier))
        return self.user

    def start_chat(self, provider_id, text, *, inmail=False):
        self.calls.append(("start_chat", provider_id, text, inmail))
        return {"chat_id": "c1", "message_id": "m1"}

    def send_invite(self, provider_id, note):
        self.calls.append(("send_invite", provider_id, note))
        return {"invitation_id": "i1"}


def _contact(**kwargs) -> Contact:
    return Contact(full_name="Jane Doe", provider_id="ACoAAA", **kwargs)


def test_first_degree_is_a_connection(cfg) -> None:
    client = FakeClient({"provider_id": "ACoAAA", "network_distance": "FIRST_DEGREE"})
    status = UnipileProvider(cfg, client).status(_contact())
    assert status.is_connection is True


def test_second_degree_is_not_a_connection(cfg) -> None:
    status = UnipileProvider(cfg, FakeClient()).status(_contact())
    assert status.is_connection is False


def test_a_dm_starts_a_chat(cfg) -> None:
    client = FakeClient()
    result = UnipileProvider(cfg, client).send_dm(_contact(), "hello")
    assert result.ok is True
    assert result.ref == "m1"
    assert client.calls[-1] == ("start_chat", "ACoAAA", "hello", False)


def test_a_paid_inmail_sets_the_inmail_flag(cfg) -> None:
    client = FakeClient()
    UnipileProvider(cfg, client).send_inmail(_contact(), "hello", paid=True)
    assert client.calls[-1][3] is True


def test_an_invite_carries_the_note(cfg) -> None:
    client = FakeClient()
    result = UnipileProvider(cfg, client).send_invite(_contact(), "hi there")
    assert result.ok is True
    assert client.calls[-1] == ("send_invite", "ACoAAA", "hi there")


def test_a_contact_without_a_provider_id_is_resolved_from_the_profile_url(cfg) -> None:
    client = FakeClient()
    contact = Contact(
        full_name="Jane Doe",
        provider_id=None,
        profile_url="https://www.linkedin.com/in/jane-doe-1234",
    )
    UnipileProvider(cfg, client).send_dm(contact, "hello")
    assert ("get_user", "jane-doe-1234") in client.calls
    assert contact.provider_id == "ACoAAA"


def test_a_profile_url_with_a_trailing_path_still_reduces_to_the_slug(cfg) -> None:
    client = FakeClient()
    contact = Contact(
        full_name="Jane Doe",
        provider_id=None,
        profile_url="https://www.linkedin.com/in/jane-doe/recent-activity/all/",
    )
    UnipileProvider(cfg, client).send_dm(contact, "hello")
    assert ("get_user", "jane-doe") in client.calls
    assert contact.provider_id == "ACoAAA"


def test_an_unresolvable_contact_refuses_the_send(cfg) -> None:
    contact = Contact(full_name="Jane Doe", provider_id=None, profile_url=None)
    result = UnipileProvider(cfg, FakeClient()).send_dm(contact, "hello")
    assert result.ok is False
    assert "no LinkedIn identifier" in (result.failure or "")


def test_a_failed_send_reports_rather_than_raising(cfg) -> None:
    class Broken(FakeClient):
        def start_chat(self, provider_id, text, *, inmail=False):
            from jobhunt.outreach.unipile_client import UnipileError

            raise UnipileError("POST /api/v1/chats returned 422: nope")

    result = UnipileProvider(cfg, Broken()).send_dm(_contact(), "hello")
    assert result.ok is False
    assert "422" in (result.failure or "")


def test_a_non_dict_send_response_reports_rather_than_raising(cfg) -> None:
    class Weird(FakeClient):
        def start_chat(self, provider_id, text, *, inmail=False):
            self.calls.append(("start_chat", provider_id, text, inmail))
            return ["not", "a", "dict"]

    result = UnipileProvider(cfg, Weird()).send_dm(_contact(), "hello")
    assert result.ok is False
    assert result.failure


def test_a_non_dict_status_response_degrades_to_unknown_rather_than_raising(cfg) -> None:
    class Weird(FakeClient):
        def get_user(self, identifier):
            self.calls.append(("get_user", identifier))
            return ["not", "a", "dict"]

    status = UnipileProvider(cfg, Weird()).status(_contact())
    assert status.is_connection is None


def test_the_default_config_resolves_to_the_stub(cfg) -> None:
    assert isinstance(build_sender(cfg), stub_module.StubProvider)


def test_selecting_unipile_without_credentials_fails_loudly(cfg, monkeypatch) -> None:
    from jobhunt.outreach.unipile_client import MissingCredentials

    cfg.raw.setdefault("outreach", {})["provider"] = "unipile"
    for name in ("UNIPILE_DSN", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingCredentials):
        build_sender(cfg)
