"""Place names to ISO codes. The wizard collects what a person would say."""
from __future__ import annotations

from jobhunt.rank import regions


def test_country_names_and_codes_both_resolve() -> None:
    codes, worldwide = regions.resolve(["Turkey", "DE", "netherlands"])
    assert codes == ["DE", "NL", "TR"]
    assert worldwide is False


def test_regions_expand() -> None:
    codes, _ = regions.resolve(["Nordics"])
    assert codes == ["DK", "FI", "IS", "NO", "SE"]
    assert "DE" in regions.resolve(["DACH"])[0]


def test_worldwide_is_an_allowance_not_a_country_list() -> None:
    codes, worldwide = regions.resolve(["remote worldwide"])
    assert codes == []
    assert worldwide is True


def test_worldwide_combines_with_countries() -> None:
    codes, worldwide = regions.resolve(["remote worldwide", "Turkey"])
    assert codes == ["TR"]
    assert worldwide is True


def test_an_unknown_name_is_dropped_and_reported_not_guessed(cfg=None) -> None:
    """A silently invented country filters the corpus to nothing with no visible
    cause. The wizard reports what it could not place instead."""
    codes, _ = regions.resolve(["Atlantis", "Turkey"])
    assert codes == ["TR"]
    assert regions.unresolved(["Atlantis", "Turkey", "Europe"]) == ["Atlantis"]


def test_europe_includes_turkey_and_the_uk() -> None:
    codes, _ = regions.resolve(["Europe"])
    assert {"TR", "GB", "DE"} <= set(codes)


def test_eu_excludes_the_uk_and_turkey() -> None:
    codes, _ = regions.resolve(["EU"])
    assert "GB" not in codes and "TR" not in codes
    assert "DE" in codes
