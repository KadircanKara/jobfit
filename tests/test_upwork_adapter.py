import dataclasses
import json
import pathlib
import subprocess

from jobhunt.preferences import UpworkPreferences
from jobhunt.sources.base import BoardRef
from jobhunt.sources.upwork import UpworkAdapter

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _payload() -> dict:
    return json.loads((FIXTURES / "upwork_payload.json").read_text(encoding="utf-8"))


def _ref() -> BoardRef:
    return BoardRef(provider="upwork", token="rag|hourly", market="upwork")


def test_a_search_result_becomes_a_posting() -> None:
    postings = list(UpworkAdapter().normalize(_payload(), _ref()))
    assert postings
    first = postings[0]
    assert first.source == "upwork"
    assert first.market == "upwork"
    assert first.remote_type == "remote"
    assert first.employment_type == "contract"


def test_the_untrusted_wrapper_is_stripped_from_the_description() -> None:
    posting = next(p for p in UpworkAdapter().normalize(_payload(), _ref()) if p.description_text)
    assert "untrusted_participant_content" not in posting.description_text


def test_an_hourly_rate_comes_from_the_detail_not_the_search() -> None:
    """`budget` is the string "0.0" on hourly rows; the real range is in `get`."""
    postings = {p.external_id: p for p in UpworkAdapter().normalize(_payload(), _ref())}
    hourly = postings["2094821655490856773"]
    assert hourly.salary_period == "hourly"
    assert hourly.salary_min == 20 and hourly.salary_max == 30
    assert hourly.salary_is_stated is True


def test_a_fixed_price_job_uses_the_fixed_period() -> None:
    postings = {p.external_id: p for p in UpworkAdapter().normalize(_payload(), _ref())}
    fixed = next(p for p in postings.values() if p.salary_period == "fixed")
    assert fixed.salary_min and fixed.salary_is_stated is True


def test_a_zero_budget_is_absent_rather_than_free_work() -> None:
    payload = _payload()
    for item in payload["pages"][0]["results"]:
        item["budget"] = "0.0"
    payload["details"] = {}
    for posting in UpworkAdapter().normalize(payload, _ref()):
        assert posting.salary_min is None


def test_upworks_experience_level_never_reaches_the_seniority_signal() -> None:
    """Entry/intermediate/expert rate the contract, not the person.

    `store.upsert_posting` derives the Job row's seniority from
    `norm.detect_seniority(posting.title, posting.description_text)` - so the
    bar is that Upwork's EXPERT/ENTRY_LEVEL/INTERMEDIATE value never appears in
    any field that feeds that (or any other) seniority-shaped signal. It is
    allowed - and expected - to show up in `description_md`'s human-facing
    trailer, which nothing seniority-related reads.
    """
    for posting in UpworkAdapter().normalize(_payload(), _ref()):
        fields = dataclasses.asdict(posting)
        fields.pop("description_md")
        haystack = " ".join(str(v) for v in fields.values() if v is not None).upper()
        assert "EXPERT" not in haystack
        assert "ENTRY_LEVEL" not in haystack

    hourly = next(
        p for p in UpworkAdapter().normalize(_payload(), _ref())
        if p.external_id == "2094821655490856773"
    )
    assert "EXPERT" in hourly.description_md


def test_a_posting_without_a_detail_falls_back_to_the_search_snippet() -> None:
    """`description_snippet` must actually reach `description_text` - not just
    the "snippet" label reading correctly regardless of what text is there."""
    payload = _payload() | {"details": {}}
    postings = {p.external_id: p for p in UpworkAdapter().normalize(payload, _ref())}
    assert postings
    for item in payload["pages"][0]["results"]:
        posting = postings[item["id"]]
        assert posting.description_text == item["description_snippet"].strip()
        assert posting.jd_completeness == "snippet"


def test_a_posting_with_neither_a_detail_nor_a_snippet_is_none_not_snippet() -> None:
    """A gate that only ever sees the trailer must not be told the body was
    merely truncated - `linkedin.py` sets "none" in exactly this case."""
    payload = _payload() | {"details": {}}
    for item in payload["pages"][0]["results"]:
        item.pop("description_snippet", None)
    for posting in UpworkAdapter().normalize(payload, _ref()):
        assert posting.description_text is None
        assert posting.jd_completeness == "none"


def test_a_long_snippet_is_still_labelled_snippet_not_full() -> None:
    """A snippet is truncated by definition, regardless of how long it is -
    `norm.completeness`'s length threshold must never promote it to "full"."""
    payload = _payload() | {"details": {}}
    long_snippet = "Backend engineer needed for a long-running project. " * 20
    assert len(long_snippet) >= 400
    for item in payload["pages"][0]["results"]:
        item["description_snippet"] = long_snippet
    for posting in UpworkAdapter().normalize(payload, _ref()):
        assert posting.jd_completeness == "snippet"


def test_a_wrapper_only_detail_description_falls_back_to_the_snippet() -> None:
    """A detail whose description is only the wrapper tags (or whitespace) is
    truthy as a string but holds no prose - it must not be reported as "full"
    with no text, the exact defect the snippet path was already fixed for."""
    payload = _payload()
    node = payload["details"]["2094821655490856773"]["data"]["marketplaceJobPosting"]
    node["content"]["description"] = "<untrusted_participant_content></untrusted_participant_content>"
    postings = {p.external_id: p for p in UpworkAdapter().normalize(payload, _ref())}
    hourly = postings["2094821655490856773"]
    snippet = next(
        item for item in payload["pages"][0]["results"] if item["id"] == "2094821655490856773"
    )["description_snippet"]
    assert hourly.description_text == snippet.strip()
    assert hourly.jd_completeness == "snippet"


def test_jd_completeness_is_full_on_the_detail_path() -> None:
    postings = {p.external_id: p for p in UpworkAdapter().normalize(_payload(), _ref())}
    hourly = postings["2094821655490856773"]
    assert hourly.jd_completeness == "full"
    assert hourly.jd_source == "api"


def test_an_hourly_budget_min_of_zero_beside_a_real_max_is_dropped() -> None:
    """`hourlyBudgetMin: 0` is not a real floor - it must not report salary_min
    == 0 just because a real max sits beside it."""
    payload = _payload()
    node = payload["details"]["2094821655490856773"]["data"]["marketplaceJobPosting"]
    node["contractTerms"]["hourlyContractTerms"]["hourlyBudgetMin"] = 0
    postings = {p.external_id: p for p in UpworkAdapter().normalize(payload, _ref())}
    hourly = postings["2094821655490856773"]
    assert hourly.salary_min is None
    assert hourly.salary_max == 30
    assert hourly.salary_period == "hourly"
    assert hourly.salary_is_stated is True


def test_the_skills_and_client_reach_the_description_trailer() -> None:
    posting = next(p for p in UpworkAdapter().normalize(_payload(), _ref()) if p.description_md)
    assert "Skills:" in posting.description_md
    assert "Client:" in posting.description_md


def test_malformed_results_yield_nothing_rather_than_raising() -> None:
    assert list(UpworkAdapter().normalize({"pages": [{"results": ["nonsense"]}]}, _ref())) == []


def test_a_non_list_pages_yields_nothing_rather_than_raising() -> None:
    assert list(UpworkAdapter().normalize({"pages": 5}, _ref())) == []


def test_a_non_list_results_yields_nothing_rather_than_raising() -> None:
    assert list(UpworkAdapter().normalize({"pages": [{"results": 5}]}, _ref())) == []


def test_an_empty_envelope_yields_nothing() -> None:
    assert list(UpworkAdapter().normalize({}, _ref())) == []


def test_refs_come_from_preferences_not_from_boards() -> None:
    prefs = UpworkPreferences(queries=["rag"], job_types=["hourly", "fixed"])
    tokens = [ref.token for ref in UpworkAdapter().board_refs(prefs)]
    assert tokens == ["rag|hourly", "rag|fixed"]


# --- fetch --------------------------------------------------------------


def test_only_find_jobs_is_ever_allowed(cfg, monkeypatch) -> None:
    """A tool that is not on the list cannot spend connects."""
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout=0):
        seen["tools"] = tools
        seen["phase"] = phase
        return "{}"

    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", fake_run)
    UpworkAdapter(config=cfg).fetch(_ref(), None)
    assert seen["tools"] == "mcp__upwork__upwork__find_jobs"
    assert seen["phase"] == "upwork"


def test_a_malformed_response_degrades_rather_than_raising(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: "sorry, I could not do that"
    )
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None) == {"pages": [], "details": {}}
    assert adapter.was_truncated() is True


def test_an_agent_error_degrades_rather_than_raising(cfg, monkeypatch) -> None:
    from jobhunt.web.agent import AgentError

    def boom(*a, **k):
        raise AgentError("claude exited 1")

    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", boom)
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None)["pages"] == []
    assert adapter.was_truncated() is True


def test_json_wrapped_in_prose_is_still_read(cfg, monkeypatch) -> None:
    body = 'here you go:\n```json\n{"pages": [], "details": {}, "error": null}\n```\nhope that helps'
    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", lambda *a, **k: body)
    assert UpworkAdapter(config=cfg).fetch(_ref(), None)["pages"] == []


def test_known_ids_reach_the_prompt_so_details_are_not_refetched(cfg, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run",
        lambda config, phase, prompt, **k: seen.setdefault("prompt", prompt) and "{}" or "{}",
    )
    UpworkAdapter(config=cfg, known_ids={"12345"}).fetch(_ref(), None)
    assert "12345" in seen["prompt"]


def test_upwork_is_always_truncated(cfg, monkeypatch) -> None:
    """A parameterised search is a filtered view, never a whole board."""
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run",
        lambda *a, **k: '{"pages": [], "details": {}, "stopped_because": "no_cursor"}',
    )
    adapter = UpworkAdapter(config=cfg)
    adapter.fetch(_ref(), None)
    assert adapter.was_truncated() is True


def test_the_guard_refusing_skips_the_subprocess_entirely(cfg, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: called.append(1) or "{}"
    )
    adapter = UpworkAdapter(config=cfg)
    adapter.budget.tripped = True
    adapter.fetch(_ref(), None)
    assert called == []
    # The envelope a refusal writes is identical to the one an empty search
    # writes; this flag is the only thing that tells the run they differ.
    assert adapter.was_refused() is True


def test_a_search_that_actually_ran_is_not_reported_as_refused(cfg, monkeypatch) -> None:
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: '{"pages": [], "details": {}}'
    )
    adapter = UpworkAdapter(config=cfg)
    adapter.fetch(_ref(), None)
    assert adapter.was_refused() is False


def test_an_unreadable_response_is_logged_with_its_raw_text(cfg, monkeypatch, caplog) -> None:
    """The only evidence a first real run leaves for an environmental failure -
    a missing MCP server, a login prompt - is this log line."""
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: "I could not reach the Upwork server."
    )
    with caplog.at_level("WARNING"):
        UpworkAdapter(config=cfg).fetch(_ref(), None)
    assert "could not reach the Upwork server" in caplog.text


def test_a_config_less_adapter_does_not_call_out(cfg, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: called.append(1) or "{}"
    )
    assert UpworkAdapter().fetch(_ref(), None)["pages"] == []
    assert called == []


def test_org_uid_sits_beside_params_not_inside_it(cfg, monkeypatch) -> None:
    """The MCP schema is {action, org_uid, params} - org_uid is not a filter."""
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout=0):
        seen["prompt"] = prompt
        return "{}"

    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", fake_run)
    UpworkAdapter(config=cfg).fetch(_ref(), None)
    payload = json.loads(seen["prompt"].split("```json\n", 1)[1].split("\n```", 1)[0])
    assert "org_uid" in payload
    assert "org_uid" not in payload["params"]
    assert payload["action"] == "search"


def test_a_response_missing_pages_is_treated_as_a_failure(cfg, monkeypatch) -> None:
    """`{}` is valid JSON but not a real result, and has no `error` to carry."""
    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", lambda *a, **k: "{}")
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None) == {"pages": [], "details": {}}


def test_a_reported_error_is_a_failure_not_a_success(cfg, monkeypatch) -> None:
    """A model that reports an MCP failure exactly as told must not look like

    a genuinely empty search: it must trip the failure streak, not
    `record_ok()`, and the reason must survive into the stored envelope.
    """
    calls = []
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run",
        lambda *a, **k: calls.append(1)
        or '{"pages": [], "details": {}, "error": "find_jobs timed out"}',
    )
    adapter = UpworkAdapter(config=cfg)
    result = adapter.fetch(_ref(), None)
    assert result == {"pages": [], "details": {}, "error": "find_jobs timed out"}
    assert len(calls) == 1  # not retried - the model already explained itself
    assert adapter.budget._streak == 1


def test_a_null_error_does_not_trip_the_breaker(cfg, monkeypatch) -> None:
    """`"error": null` is what a clean run looks like - falsy, not a failure."""
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run",
        lambda *a, **k: '{"pages": [], "details": {}, "error": null}',
    )
    adapter = UpworkAdapter(config=cfg)
    result = adapter.fetch(_ref(), None)
    assert "error" not in result
    assert adapter.budget._streak == 0


def test_a_wrongly_typed_pages_is_treated_as_a_failure(cfg, monkeypatch) -> None:
    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", lambda *a, **k: '{"pages": "oops"}')
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None) == {"pages": [], "details": {}}


def test_a_timeout_degrades_rather_than_raising(cfg, monkeypatch) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", boom)
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None) == {"pages": [], "details": {}}


def test_a_missing_prompt_template_degrades_rather_than_raising(cfg, monkeypatch) -> None:
    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", lambda *a, **k: "{}")
    monkeypatch.setattr("jobhunt.sources.upwork._PACKAGED_PROMPT", pathlib.Path("/no/such/file.md"))
    adapter = UpworkAdapter(config=cfg)
    assert adapter.fetch(_ref(), None) == {"pages": [], "details": {}}


def test_an_unparseable_response_spends_the_budget_on_each_retry(cfg, monkeypatch) -> None:
    """The retry must cost as much as the first attempt - no free second turn."""
    from jobhunt.db.models import utcnow

    calls = []
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run",
        lambda *a, **k: calls.append(1) or "sorry, I could not do that",
    )
    adapter = UpworkAdapter(config=cfg)
    adapter.fetch(_ref(), None)
    assert len(calls) == 2
    assert adapter.budget._spent(utcnow()) == 2



# --- the projected detail shape, and description loss -------------------------


def _envelope(details: dict) -> dict:
    return {
        "pages": [{"results": [{
            "id": "1", "title": "Build a RAG pipeline", "job_type": "fixed",
            "budget": "2000.0", "created_date": "2026-09-01T10:00:00+0000",
            "description_snippet": "short teaser",
            "client": {"verification_status": "VERIFIED", "total_spent": "$4,336.92"},
        }]}],
        "details": details,
    }


def test_a_projected_detail_is_read_the_same_as_a_wrapped_one() -> None:
    """The prompt now asks for `data.marketplaceJobPosting` already unwrapped.
    Payloads fetched before that change are still on disk and still normalize."""
    projected = {"1": {"content": {"title": "t", "description": "the full body " * 20}}}
    wrapped = {"1": {"data": {"marketplaceJobPosting": projected["1"]}}}

    a = next(UpworkAdapter().normalize(_envelope(projected), _ref()))
    b = next(UpworkAdapter().normalize(_envelope(wrapped), _ref()))

    assert a.jd_completeness == "full" and b.jd_completeness == "full"
    assert a.description_text == b.description_text


def test_a_detail_that_lost_its_description_does_not_claim_to_be_full() -> None:
    """The exact shape the first real run produced: six details, every one with
    `content` present but `description` silently gone. Counting those as full
    marks the row extracted and it is never re-fetched."""
    posting = next(UpworkAdapter().normalize(_envelope({"1": {"content": {"title": "t"}}}), _ref()))
    assert posting.jd_completeness == "snippet"
    assert posting.description_text == "short teaser"


def test_client_verification_and_spend_are_read_from_the_search_result() -> None:
    posting = next(UpworkAdapter().normalize(_envelope({}), _ref()))
    assert posting.client_verified is True
    assert posting.client_total_spent == 4336.92


def test_an_unstated_client_block_leaves_both_fields_unknown() -> None:
    envelope = _envelope({})
    envelope["pages"][0]["results"][0].pop("client")
    posting = next(UpworkAdapter().normalize(envelope, _ref()))
    assert posting.client_verified is None
    assert posting.client_total_spent is None


def test_a_fetch_that_lost_every_description_is_retried_once(cfg, monkeypatch) -> None:
    """Description loss is silent - well-formed JSON, no error key - so nothing
    upstream can tell it from a search that legitimately found no detail. The
    one place it is detectable is here, against the ids we asked for."""
    import json as _json

    lossy = _json.dumps(_envelope({"1": {"content": {"title": "t"}}}))
    good = _json.dumps(_envelope({"1": {"content": {"title": "t", "description": "x" * 200}}}))
    answers = [lossy, good]
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: answers.pop(0)
    )

    adapter = UpworkAdapter(config=cfg)
    envelope = adapter.fetch(_ref(), None)

    assert answers == []
    assert "description" in envelope["details"]["1"]["content"]


def test_a_fetch_whose_retry_is_also_lossy_keeps_what_it_got(cfg, monkeypatch) -> None:
    """One retry, then take it. A ref with genuinely description-less details
    must still contribute its search results rather than returning empty."""
    import json as _json

    lossy = _json.dumps(_envelope({"1": {"content": {"title": "t"}}}))
    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", lambda *a, **k: lossy)

    envelope = UpworkAdapter(config=cfg).fetch(_ref(), None)

    assert len(envelope["pages"][0]["results"]) == 1


def test_a_client_country_object_does_not_lose_the_job() -> None:
    """`clientCompanyPublic.country` is an object, not the string the module
    docstring claimed: `{"name": "United States", "region": "", ...}`. It reached
    `norm.country_code`, which called `.strip()` on a dict, and `normalize`'s
    per-result `except Exception` swallowed it - so a posting was dropped for the
    sole reason that its detail fetch had succeeded. Seen live: 19 of 20 results
    stored, the missing one being the only one with a detail."""
    details = {"1": {
        "content": {"title": "t", "description": "the full body " * 20},
        "clientCompanyPublic": {"country": {"name": "United States", "region": ""}, "state": "FL"},
    }}
    postings = list(UpworkAdapter().normalize(_envelope(details), _ref()))

    assert len(postings) == 1
    assert postings[0].country == "US"
    assert postings[0].jd_completeness == "full"


def test_a_client_country_string_still_works() -> None:
    details = {"1": {"content": {"title": "t", "description": "body " * 40},
                     "clientCompanyPublic": {"country": "Germany"}}}
    assert next(UpworkAdapter().normalize(_envelope(details), _ref())).country == "DE"


def test_an_unusable_client_country_is_simply_unknown() -> None:
    details = {"1": {"content": {"title": "t", "description": "body " * 40},
                     "clientCompanyPublic": {"country": {"region": "EMEA"}}}}
    posting = next(UpworkAdapter().normalize(_envelope(details), _ref()))
    assert posting.country is None
    assert posting.jd_completeness == "full"


# --- the client's region ------------------------------------------------------


def test_a_us_state_code_is_spelled_out() -> None:
    """"CA" must never travel as-is: LinkedIn's own location lookup resolves it
    to Canada, so an abbreviation would silently search the wrong country."""
    details = {"1": {"content": {"title": "t", "description": "body " * 40},
                     "clientCompanyPublic": {"country": {"name": "United States"}, "state": "CA"}}}
    assert next(UpworkAdapter().normalize(_envelope(details), _ref())).client_region == "California"


def test_a_state_code_outside_the_us_is_left_alone() -> None:
    """The map is US-only. "CA" under Canada is a province code this module has
    no table for, and inventing one would be worse than storing nothing."""
    details = {"1": {"content": {"title": "t", "description": "body " * 40},
                     "clientCompanyPublic": {"country": {"name": "Canada"}, "state": "ON"}}}
    assert next(UpworkAdapter().normalize(_envelope(details), _ref())).client_region is None


def test_a_spelled_out_region_survives_unchanged() -> None:
    details = {"1": {"content": {"title": "t", "description": "body " * 40},
                     "clientCompanyPublic": {"country": {"name": "United States"},
                                             "state": "California"}}}
    assert next(UpworkAdapter().normalize(_envelope(details), _ref())).client_region == "California"


def test_no_detail_means_no_region() -> None:
    assert next(UpworkAdapter().normalize(_envelope({}), _ref())).client_region is None


# --- the feed, locations, and budgets over a thousand ---------------------------


def _call(cfg, monkeypatch, token: str) -> dict:
    """The call object the prompt asks the agent to make, for one ref."""
    seen = {}

    def fake_run(config, phase, prompt, *, tools, timeout=0):
        seen["prompt"] = prompt
        return "{}"

    monkeypatch.setattr("jobhunt.sources.upwork.agent.run", fake_run)
    UpworkAdapter(config=cfg).fetch(BoardRef(provider="upwork", token=token, market="upwork"), None)
    return json.loads(seen["prompt"].split("```json\n", 1)[1].split("\n```", 1)[0])


def test_a_feed_ref_reads_the_most_recent_feed(cfg, monkeypatch) -> None:
    from jobhunt import preferences

    prefs, _ = preferences.load(cfg)
    call = _call(cfg, monkeypatch, "@feed|hourly|United States")
    assert call["action"] == "smart_search"
    assert call["params"]["mode"] == "most_recent"
    assert call["params"]["days_posted"] == prefs.max_age_days
    assert call["params"]["location"] == "United States"
    assert "query" not in call["params"]


def test_a_keyword_ref_with_a_location_searches_that_location(cfg, monkeypatch) -> None:
    call = _call(cfg, monkeypatch, "rag|fixed|Canada")
    assert call["action"] == "search"
    assert call["params"]["query"] == "rag"
    assert call["params"]["job_type"] == "fixed"
    assert call["params"]["location"] == "Canada"


def test_board_refs_put_the_feed_first_and_fan_out_by_location() -> None:
    prefs = UpworkPreferences(
        queries=["rag"], job_types=["hourly"],
        client_locations=["United States", "Canada"], recommended_feed=True,
    )
    assert [ref.token for ref in UpworkAdapter().board_refs(prefs)] == [
        "@feed|hourly|United States", "@feed|hourly|Canada",
        "rag|hourly|United States", "rag|hourly|Canada",
    ]


def test_a_fixed_budget_over_a_thousand_is_read() -> None:
    """Search sends "1,500.00". `float` refused the comma, so every fixed job of
    $1,000 or more was stored as unstated and slipped past the budget floor."""
    envelope = _envelope({})
    envelope["pages"][0]["results"][0]["budget"] = "1,500.00"
    posting = next(UpworkAdapter().normalize(envelope, _ref()))
    assert posting.salary_min == 1500 and posting.salary_is_stated is True


def test_a_feed_result_becomes_a_posting() -> None:
    """Shape copied from a live `smart_search mode=most_recent` response on
    2026-09-23: a capitalised experience level, a `$` in the budget, and a hire
    count on the client block that search results do not carry."""
    envelope = {"pages": [{"results": [{
        "id": "2102835592985772593", "title": "AI Agent to Find Bid Opportunities",
        "job_type": "fixed", "budget": "$1,500.00", "experience_level": "Intermediate",
        "created_date": "2026-09-23T19:00:49.275Z", "description_snippet": "short teaser",
        "client": {"country": "United States", "total_hires": 6,
                   "total_spent": "$1,990.02", "verification_status": "VERIFIED"},
    }]}], "details": {}}
    feed_ref = BoardRef(provider="upwork", token="@feed|fixed", market="upwork")
    posting = next(UpworkAdapter().normalize(envelope, feed_ref))
    assert posting.salary_min == 1500 and posting.salary_period == "fixed"
    assert posting.client_total_spent == 1990.02
    assert posting.client_verified is True


def test_the_prompt_names_the_real_paging_markers() -> None:
    from jobhunt.sources.upwork import _PACKAGED_PROMPT

    text = _PACKAGED_PROMPT.read_text(encoding="utf-8")
    assert "next_cursor" in text and "hasMore" in text and "hasNextPage" in text


def test_the_prompt_turns_a_rejected_filter_into_an_error() -> None:
    from jobhunt.sources.upwork import _PACKAGED_PROMPT

    assert "filters_rejected" in _PACKAGED_PROMPT.read_text(encoding="utf-8")
