from jobhunt.preferences import Preferences
from jobhunt.sources import linkedin_query as query


def test_the_intern_band_asks_linkedin_for_internships() -> None:
    prefs = Preferences(experience_min="intern", experience_max="intern")
    assert query.search_params("Backend Engineer", None, prefs)["f_E"] == "1"


def test_a_band_spanning_levels_asks_for_all_of_them() -> None:
    prefs = Preferences(experience_min="junior", experience_max="mid")
    assert query.search_params("Backend Engineer", None, prefs)["f_E"] == "2,3"


def test_no_band_omits_the_experience_filter() -> None:
    assert "f_E" not in query.search_params("Backend Engineer", None, Preferences())


def test_remote_work_model_maps_to_the_workplace_filter() -> None:
    prefs = Preferences(work_model=["remote"])
    assert query.search_params("Backend Engineer", None, prefs)["f_WT"] == "2"


def test_age_becomes_a_seconds_window() -> None:
    prefs = Preferences(max_age_days=7)
    assert query.search_params("Backend Engineer", None, prefs)["f_TPR"] == "r604800"


def test_a_known_location_becomes_a_geo_id() -> None:
    params = query.search_params("Backend Engineer", "Germany", Preferences())
    assert params["geoId"] == query.GEO_IDS["germany"]


def test_an_unknown_location_falls_back_to_the_text_field() -> None:
    params = query.search_params("Backend Engineer", "Atlantis", Preferences())
    assert "geoId" not in params
    assert params["location"] == "Atlantis"


def test_the_keyword_is_the_title() -> None:
    assert query.search_params("Backend Engineer", None, Preferences())["keywords"] == "Backend Engineer"


def test_pagination_is_by_start() -> None:
    assert query.search_params("X", None, Preferences(), start=20)["start"] == "20"
