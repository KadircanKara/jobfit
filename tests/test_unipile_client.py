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
    with pytest.raises(uc.UnipileError):
        uc.UnipileClient(dsn="http://api1.unipile.com", api_key="secret", account_id="acct-1")


def test_a_malformed_dsn_is_rejected() -> None:
    with pytest.raises(uc.UnipileError):
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


def test_a_dot_identifier_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user(".")


def test_a_dot_dot_identifier_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("..")


def test_a_non_string_identifier_raises_unipile_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user(None)


def test_an_underscore_identifier_is_accepted() -> None:
    """LinkedIn member URNs are base64url and routinely contain underscores."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"provider_id": "ACoAAA"})

    _client(handler).get_user("AbC_123-xyZ")
    assert "AbC_123-xyZ" in seen["url"]


def test_a_dot_segment_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane.doe")


def test_a_slash_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane/doe")


def test_a_percent_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane%2e%2e")


def test_a_slash_encoded_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane%2fdoe")


def test_a_dot_dot_slash_encoded_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("%2e%2e%2f")


def test_a_dot_dot_slash_dot_dot_identifier_is_still_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("..%2f..")


def test_a_control_character_identifier_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("jane\x00doe")


def test_an_empty_identifier_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should never be sent for a bad identifier")

    with pytest.raises(uc.UnipileError):
        _client(handler).get_user("")


@pytest.mark.parametrize(
    ("decoded", "wire_form"),
    [
        ("esra-çakal-18579461", "esra-%C3%A7akal-18579461"),
        ("aslı-kemaloğlu-93303985", "asl%C4%B1-kemalo%C4%9Flu-93303985"),
        ("petra-horáková", "petra-hor%C3%A1kov%C3%A1"),
    ],
)
def test_a_decoded_unicode_slug_is_accepted_and_re_encoded_on_the_wire(decoded, wire_form) -> None:
    """The real defect: a Turkish/Czech slug must pass validation and hit the

    exact percent-encoded path LinkedIn's own URL uses, so `quote(..., safe="")`
    inside `_call` is the only place encoding happens - never the identifier
    itself.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"provider_id": "ACoAAA"})

    _client(handler).get_user(decoded)
    assert wire_form in seen["url"]


def test_the_account_id_travels_as_a_query_parameter_not_in_the_body() -> None:
    """Live, the body form is a hard 400: "path": "/account_id", "Required
    property". The whole company-search path had never once worked."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"items": []})

    _client(handler).search_people("Acme", ["engineer"])
    assert "account_id=acct-1" in seen["url"]
    assert "account_id" not in seen["body"]


def test_a_location_reaches_the_search_when_one_is_known() -> None:
    """Measured: "Nexora" alone returned Istanbul and Tunisia; the same query
    filtered to California surfaced the Californian company's founder first."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/parameters" in str(request.url):
            return httpx.Response(200, json={"items": [{"id": "102095887",
                                                        "title": "California, United States"}]})
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"items": []})

    _client(handler).search_people("Nexora", ["founder"], location="California, United States")
    assert "102095887" in seen["body"]


def test_an_unresolvable_location_searches_without_one() -> None:
    """A location nobody can place must cost the filter, never the search."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/parameters" in str(request.url):
            return httpx.Response(200, json={"items": []})
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"items": []})

    _client(handler).search_people("Acme", ["founder"], location="Atlantis")
    assert "location" not in seen["body"]


def test_a_failing_location_lookup_still_searches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/parameters" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"items": [{"name": "Jane"}]})

    assert _client(handler).search_people("Acme", ["founder"], location="California") == [
        {"name": "Jane"}
    ]


def test_the_company_name_is_the_query_and_roles_do_not_dilute_it() -> None:
    """Measured live: "Nexora founder OR CTO OR recruiter" returned six
    co-founders of six unrelated companies and nobody at Nexora - LinkedIn
    matched the roles and lost the name. The company name alone returned the
    CFO of Nexora Solutions and the founder of Nexora AI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"items": []})

    _client(handler).search_people("Nexora", ["founder", "recruiter", "CTO"])
    assert '"keywords": "Nexora"' in seen["body"] or '"keywords":"Nexora"' in seen["body"]
    assert "OR" not in seen["body"]
