import httpx
import pytest

from jobhunt.outreach import unipile_client as uc


def _client(handler) -> uc.UnipileClient:
    transport = httpx.MockTransport(handler)
    return uc.UnipileClient(
        dsn="https://api1.unipile.com:13111",
        api_key="secret",
        account_id="acct-1",
        client=httpx.Client(transport=transport),
    )


def test_the_api_key_travels_as_a_header() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("X-API-KEY")
        return httpx.Response(200, json={"provider_id": "ACoAAA"})

    _client(handler).get_user("jane-doe")
    assert seen["key"] == "secret"


def test_starting_a_chat_posts_the_recipient_and_text() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"chat_id": "c1", "message_id": "m1"})

    result = _client(handler).start_chat("ACoAAA", "hello")
    assert "/chats" in seen["url"]
    assert "ACoAAA" in seen["body"] and "hello" in seen["body"]
    assert result["message_id"] == "m1"


def test_an_inmail_sets_the_classic_flag() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"chat_id": "c1", "message_id": "m1"})

    _client(handler).start_chat("ACoAAA", "hello", inmail=True)
    assert '"inmail": true' in seen["body"] or '"inmail":true' in seen["body"]


def test_an_invite_posts_to_the_invite_endpoint() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"invitation_id": "i1"})

    _client(handler).send_invite("ACoAAA", "hi there")
    assert seen["url"].endswith("/users/invite")


def test_an_error_response_raises_unipile_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "invalid provider_id"})

    with pytest.raises(uc.UnipileError) as excinfo:
        _client(handler).start_chat("nope", "hello")
    assert "invalid provider_id" in str(excinfo.value)


def test_the_api_key_never_appears_in_an_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(uc.UnipileError) as excinfo:
        _client(handler).get_user("jane")
    assert "secret" not in str(excinfo.value)


def test_missing_credentials_are_named(monkeypatch) -> None:
    monkeypatch.delenv("UNIPILE_DSN", raising=False)
    monkeypatch.setenv("UNIPILE_API_KEY", "k")
    monkeypatch.setenv("UNIPILE_ACCOUNT_ID", "a")
    with pytest.raises(uc.MissingCredentials) as excinfo:
        uc.credentials_from_env()
    assert "UNIPILE_DSN" in str(excinfo.value)


def test_a_network_failure_becomes_a_unipile_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(uc.UnipileError) as excinfo:
        _client(handler).get_user("jane")
    assert "secret" not in str(excinfo.value)


def test_a_malformed_json_body_on_success_raises_unipile_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane")


def test_a_traversal_attempt_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane/../../../../etc")


def test_a_plain_identifier_still_works() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"provider_id": "ACoAAA"})

    _client(handler).get_user("jane-doe")
    assert seen["url"].endswith("/api/v1/users/jane-doe?account_id=acct-1")


def test_a_non_https_dsn_is_rejected() -> None:
    with pytest.raises(uc.MissingCredentials):
        uc.UnipileClient(dsn="http://api1.unipile.com", api_key="secret", account_id="acct-1")


def test_a_malformed_dsn_is_rejected() -> None:
    with pytest.raises(uc.MissingCredentials):
        uc.UnipileClient(dsn="not-a-url", api_key="secret", account_id="acct-1")


def test_search_people_rejects_a_wrong_shaped_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": "not-a-list"})

    with pytest.raises(uc.UnipileError):
        _client(handler).search_people("Acme", ["engineer"])


def test_search_people_rejects_a_non_dict_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["oops"])

    with pytest.raises(uc.UnipileError):
        _client(handler).search_people("Acme", ["engineer"])
