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


def test_seniority_is_never_inferred_from_upworks_experience_level() -> None:
    """entry/intermediate/expert rate the contract, not the person."""
    for posting in UpworkAdapter().normalize(_payload(), _ref()):
        assert posting.seniority in (None, "")


def test_a_posting_without_a_detail_is_still_yielded_as_a_snippet() -> None:
    payload = _payload() | {"details": {}}
    postings = list(UpworkAdapter().normalize(payload, _ref()))
    assert postings
    assert all(p.jd_completeness == "snippet" for p in postings)


def test_the_skills_and_client_reach_the_description_trailer() -> None:
    posting = next(p for p in UpworkAdapter().normalize(_payload(), _ref()) if p.description_md)
    assert "Skills:" in posting.description_md
    assert "Client:" in posting.description_md


def test_malformed_results_yield_nothing_rather_than_raising() -> None:
    assert list(UpworkAdapter().normalize({"pages": [{"results": ["nonsense"]}]}, _ref())) == []


def test_an_empty_envelope_yields_nothing() -> None:
    assert list(UpworkAdapter().normalize({}, _ref())) == []


def test_refs_come_from_preferences_not_from_boards() -> None:
    prefs = UpworkPreferences(queries=["rag"], job_types=["hourly", "fixed"])
    tokens = [ref.token for ref in UpworkAdapter().board_refs(prefs)]
    assert tokens == ["rag|hourly", "rag|fixed"]
