import pytest

from jobhunt.db.models import Contact
from jobhunt.outreach import stub as stub_module
from jobhunt.outreach.unipile import UnipileProvider, _is_safe_slug, build_sender


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


def test_resolving_a_non_dict_user_during_a_send_refuses_rather_than_raising(cfg) -> None:
    """`_provider_id` also calls `get_user`, and must guard the same way `status` does."""

    class Weird(FakeClient):
        def get_user(self, identifier):
            self.calls.append(("get_user", identifier))
            return ["not", "a", "dict"]

    contact = Contact(
        full_name="Jane Doe",
        provider_id=None,
        profile_url="https://www.linkedin.com/in/jane-doe-1234",
    )
    result = UnipileProvider(cfg, Weird()).send_dm(contact, "hello")
    assert result.ok is False
    assert contact.provider_id is None


@pytest.mark.parametrize(
    ("encoded_slug", "expected_identifier"),
    [
        ("esra-%C3%A7akal-18579461", "esra-çakal-18579461"),
        ("asl%C4%B1-kemalo%C4%9Flu-93303985", "aslı-kemaloğlu-93303985"),
        ("petra-hor%C3%A1kov%C3%A1", "petra-horáková"),
    ],
)
def test_a_percent_encoded_non_ascii_slug_resolves_to_its_unicode_identifier(
    cfg, encoded_slug, expected_identifier
) -> None:
    """The real defect: LinkedIn's own URL for a Turkish/Czech name is percent-encoded.

    `_identifier` must decode it once, rather than handing the raw `%C3%A7`-laden
    slug to `get_user`, which is what previously made every non-ASCII contact
    unreachable at send time despite looking fine in the drawer.
    """
    client = FakeClient()
    contact = Contact(
        full_name="Non-ASCII Contact",
        provider_id=None,
        profile_url=f"https://www.linkedin.com/in/{encoded_slug}",
    )
    UnipileProvider(cfg, client).send_dm(contact, "hello")
    assert ("get_user", expected_identifier) in client.calls


@pytest.mark.parametrize(
    "encoded_slug",
    [
        "jane%2fdoe",  # decodes to jane/doe - a path segment split
        "..%2f..",  # decodes to ../.. - a traversal
        "%2e%2e%2f",  # decodes to ../ - a traversal
        "%00etc",  # decodes to a control character
    ],
)
def test_a_traversal_payload_in_the_profile_url_never_reaches_get_user(cfg, encoded_slug) -> None:
    """A slug is scraped off a page an attacker can shape, so decoding it must

    never resurrect the traversal the original ASCII-only regex was closing.
    """
    client = FakeClient()
    contact = Contact(
        full_name="Attacker Contact",
        provider_id=None,
        profile_url=f"https://www.linkedin.com/in/{encoded_slug}",
    )
    result = UnipileProvider(cfg, client).send_dm(contact, "hello")
    assert result.ok is False
    assert client.calls == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("esra-çakal-18579461", True),
        ("jane-doe", True),
        ("..", False),
        (".", False),
        ("jane/doe", False),
        ("jane\\doe", False),
        ("\x00", False),
        ("", False),
    ],
)
def test_is_safe_slug_rejects_traversal_and_control_characters(value, expected) -> None:
    assert _is_safe_slug(value) is expected


def test_a_provider_id_bypasses_decoding_entirely(cfg) -> None:
    """`Contact.provider_id` (an `ACoAAA...` LinkedIn URN) must keep working unchanged:

    it is returned before the profile-url slug is ever looked at, so nothing
    about the decode touches it.
    """
    client = FakeClient()
    contact = _contact(profile_url="https://www.linkedin.com/in/esra-%C3%A7akal-18579461")
    UnipileProvider(cfg, client).send_dm(contact, "hello")
    assert not any(call[0] == "get_user" for call in client.calls)
    assert client.calls[-1][:2] == ("start_chat", "ACoAAA")


def test_the_default_config_resolves_to_the_stub(cfg) -> None:
    assert isinstance(build_sender(cfg), stub_module.StubProvider)


def test_selecting_unipile_without_credentials_fails_loudly(cfg, monkeypatch) -> None:
    from jobhunt.outreach.unipile_client import MissingCredentials

    cfg.raw.setdefault("outreach", {})["provider"] = "unipile"
    for name in ("UNIPILE_DSN", "UNIPILE_API_KEY", "UNIPILE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingCredentials):
        build_sender(cfg)
