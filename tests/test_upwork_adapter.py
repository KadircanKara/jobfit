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
