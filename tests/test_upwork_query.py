from jobhunt.preferences import UpworkPreferences
from jobhunt.sources import upwork_query as query


def test_the_hourly_ref_carries_the_hourly_floor() -> None:
    prefs = UpworkPreferences(min_hourly=40, min_fixed=1500)
    assert query.search_params("rag", "hourly", prefs)["rate_min"] == 40


def test_the_fixed_ref_carries_the_fixed_floor() -> None:
    prefs = UpworkPreferences(min_hourly=40, min_fixed=1500)
    assert query.search_params("rag", "fixed", prefs)["budget_min"] == 1500


def test_a_missing_floor_is_omitted_rather_than_sent_as_zero() -> None:
    params = query.search_params("rag", "hourly", UpworkPreferences())
    assert "rate_min" not in params and "budget_min" not in params


def test_a_zero_floor_is_also_omitted_rather_than_sent_as_zero() -> None:
    params = query.search_params("rag", "hourly", UpworkPreferences(min_hourly=0))
    assert "rate_min" not in params and "budget_min" not in params


def test_the_query_and_type_always_travel() -> None:
    params = query.search_params("rag pipeline", "hourly", UpworkPreferences())
    assert params["query"] == "rag pipeline"
    assert params["job_type"] == "hourly"


def test_the_age_limit_is_enforced_at_rank_not_by_the_sort() -> None:
    """This used to pin `sort` to recency so pagination could stop at an age
    cutoff. `_check_age` drops old postings anyway, so recency bought a slightly
    cheaper fetch and cost the entire relevance signal."""
    assert query.search_params("x", "hourly", UpworkPreferences())["sort"] != "recency"


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


def test_workload_is_passed_through_when_a_single_one_is_chosen() -> None:
    prefs = UpworkPreferences(workload=["part_time"])
    assert query.search_params("x", "hourly", prefs)["workload"] == "part_time"


def test_several_workloads_are_omitted_because_the_api_takes_one() -> None:
    prefs = UpworkPreferences(workload=["part_time", "full_time"])
    assert "workload" not in query.search_params("x", "hourly", prefs)


def test_proposals_max_is_passed_through() -> None:
    prefs = UpworkPreferences(proposals_max=5)
    assert query.search_params("x", "hourly", prefs)["proposals_max"] == 5


def test_client_hires_min_is_passed_through_under_the_wire_name() -> None:
    prefs = UpworkPreferences(client_min_hires=10)
    assert query.search_params("x", "hourly", prefs)["client_hires_min"] == 10


def test_one_ref_per_query_and_job_type() -> None:
    prefs = UpworkPreferences(queries=["rag", "llm"], job_types=["hourly", "fixed"])
    assert query.refs_for(prefs) == [
        ("rag", "hourly"), ("rag", "fixed"), ("llm", "hourly"), ("llm", "fixed"),
    ]


def test_no_queries_means_no_refs() -> None:
    assert query.refs_for(UpworkPreferences()) == []


# --- sort, and which parameter carries an hourly floor ------------------------


def test_the_search_sorts_by_relevance_by_default() -> None:
    """Live comparison, same query and same filters, ten results each: recency
    and relevance had *zero* titles in common. Recency returned virtual-assistant
    postings that merely mention AI; relevance returned the engineering roles.
    The website's own default is Best match, which is why the app's results did
    not resemble the site's."""
    params = query.search_params("AI Agent", "hourly", UpworkPreferences())
    assert params["sort"] == "relevance"


def test_the_sort_can_be_changed() -> None:
    prefs = UpworkPreferences(sort="client_total_charge")
    assert query.search_params("x", "hourly", prefs)["sort"] == "client_total_charge"


def test_an_hourly_floor_goes_to_rate_min_not_budget_min() -> None:
    """`rate_min` and `budget_min` are separate parameters: `budget_min` is the
    fixed-price budget. Sending an hourly floor as `budget_min` filtered on the
    wrong field - verified live, the two return different result sets."""
    params = query.search_params("x", "hourly", UpworkPreferences(min_hourly=50))
    assert params["rate_min"] == 50
    assert "budget_min" not in params


def test_a_fixed_floor_still_goes_to_budget_min() -> None:
    params = query.search_params("x", "fixed", UpworkPreferences(min_fixed=1500))
    assert params["budget_min"] == 1500
    assert "rate_min" not in params


def test_an_hourly_ref_ignores_the_fixed_floor_and_the_reverse() -> None:
    prefs = UpworkPreferences(min_hourly=50, min_fixed=1500)
    assert "budget_min" not in query.search_params("x", "hourly", prefs)
    assert "rate_min" not in query.search_params("x", "fixed", prefs)
