import dataclasses
import json
import pathlib

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


def test_a_config_less_adapter_does_not_call_out(cfg, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(
        "jobhunt.sources.upwork.agent.run", lambda *a, **k: called.append(1) or "{}"
    )
    assert UpworkAdapter().fetch(_ref(), None)["pages"] == []
    assert called == []
