from jobhunt.preferences import UpworkPreferences
from jobhunt.sources import upwork_query as query


def test_the_hourly_ref_carries_the_hourly_floor() -> None:
    prefs = UpworkPreferences(min_hourly=40, min_fixed=1500)
    assert query.search_params("rag", "hourly", prefs)["budget_min"] == 40


def test_the_fixed_ref_carries_the_fixed_floor() -> None:
    prefs = UpworkPreferences(min_hourly=40, min_fixed=1500)
    assert query.search_params("rag", "fixed", prefs)["budget_min"] == 1500


def test_a_missing_floor_is_omitted_rather_than_sent_as_zero() -> None:
    params = query.search_params("rag", "hourly", UpworkPreferences())
    assert "budget_min" not in params


def test_the_query_and_type_always_travel() -> None:
    params = query.search_params("rag pipeline", "hourly", UpworkPreferences())
    assert params["query"] == "rag pipeline"
    assert params["job_type"] == "hourly"


def test_recency_is_the_sort_because_there_is_no_date_filter() -> None:
    assert query.search_params("x", "hourly", UpworkPreferences())["sort"] == "recency"


def test_the_page_size_is_the_api_maximum() -> None:
    assert query.search_params("x", "hourly", UpworkPreferences())["limit"] == 10


def test_verified_payment_is_on_by_default() -> None:
    assert query.search_params("x", "hourly", UpworkPreferences())["verified_payment_only"] is True


def test_experience_levels_are_passed_through_when_a_single_one_is_chosen() -> None:
    prefs = UpworkPreferences(experience_level=["expert"])
    assert query.search_params("x", "hourly", prefs)["experience_level"] == "expert"


def test_several_experience_levels_are_omitted_because_the_api_takes_one() -> None:
    prefs = UpworkPreferences(experience_level=["intermediate", "expert"])
    assert "experience_level" not in query.search_params("x", "hourly", prefs)


def test_one_ref_per_query_and_job_type() -> None:
    prefs = UpworkPreferences(queries=["rag", "llm"], job_types=["hourly", "fixed"])
    assert query.refs_for(prefs) == [
        ("rag", "hourly"), ("rag", "fixed"), ("llm", "hourly"), ("llm", "fixed"),
    ]


def test_no_queries_means_no_refs() -> None:
    assert query.refs_for(UpworkPreferences()) == []
