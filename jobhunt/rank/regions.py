"""Plain-language place names to ISO country codes.

The config wizard collects what a person would actually say: "Europe", "Turkey",
"remote worldwide". The filter needs `hard_requires.country` as a list of codes.
This is the translation, kept in one place so both the writer and the display
side of the wizard agree.

The country universe is `timezones.COUNTRY_OFFSETS`, so a country this project
cannot reason about timezone-wise is not one it will filter on either.
"""
from __future__ import annotations

from jobhunt.rank.timezones import COUNTRY_OFFSETS

# A name that means "no country restriction at all". Handled by the caller as an
# allowance rather than as a country list, because the worldwide case is about
# location_raw saying "Anywhere", not about a country code.
WORLDWIDE_NAMES = frozenset({
    "worldwide", "anywhere", "global", "remote worldwide", "remote", "any",
    "any location", "world", "everywhere",
})

REGIONS: dict[str, tuple[str, ...]] = {
    "europe": (
        "GB", "IE", "PT", "FR", "DE", "NL", "BE", "ES", "IT", "CH", "AT", "SE",
        "NO", "DK", "PL", "CZ", "HU", "SK", "SI", "HR", "RS", "BA", "AL", "MK",
        "LU", "MT", "ME", "FI", "EE", "LV", "LT", "RO", "BG", "GR", "CY", "UA",
        "MD", "IS", "TR",
    ),
    "eu": (
        "IE", "PT", "FR", "DE", "NL", "BE", "ES", "IT", "AT", "SE", "DK", "PL",
        "CZ", "HU", "SK", "SI", "HR", "LU", "MT", "FI", "EE", "LV", "LT", "RO",
        "BG", "GR", "CY",
    ),
    "emea": tuple(sorted(set(COUNTRY_OFFSETS) - {
        "US", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "EC", "UY", "PY", "BO",
        "VE", "PA", "CR", "GT", "SV", "HN", "DO", "PR", "JM", "CU",
        "AU", "NZ", "JP", "KR", "CN", "TW", "HK", "SG", "MY", "TH", "VN", "ID", "PH",
    })),
    "nordics": ("SE", "NO", "DK", "FI", "IS"),
    "dach": ("DE", "AT", "CH"),
    "benelux": ("NL", "BE", "LU"),
    "uk": ("GB",),
    "united kingdom": ("GB",),
    "britain": ("GB",),
    "middle east": ("AE", "SA", "QA", "KW", "BH", "OM", "JO", "LB", "IL", "IQ", "TR"),
    "north america": ("US", "CA", "MX"),
    "latin america": ("BR", "AR", "CL", "CO", "PE", "EC", "UY", "PY", "BO", "VE", "MX"),
    "americas": ("US", "CA", "MX", "BR", "AR", "CL", "CO", "PE", "EC", "UY", "PY", "BO",
                 "VE", "PA", "CR", "GT", "SV", "HN", "DO", "PR", "JM", "CU"),
    "apac": ("AU", "NZ", "JP", "KR", "CN", "TW", "HK", "SG", "MY", "TH", "VN", "ID", "PH", "IN"),
    "asia": ("IN", "PK", "BD", "LK", "CN", "JP", "KR", "TW", "HK", "SG", "MY",
             "TH", "VN", "ID", "PH", "KZ", "UZ", "NP"),
    "africa": ("ZA", "NG", "KE", "EG", "MA", "GH", "TN", "DZ", "ET", "TZ", "UG",
               "ZM", "ZW", "SN", "CI", "CM"),
}

# Country names as a person writes them. Only the ones a user of this tool is
# plausibly going to type; anything else falls back to an exact ISO code.
COUNTRY_NAMES: dict[str, str] = {
    "turkey": "TR", "türkiye": "TR", "turkiye": "TR",
    "germany": "DE", "deutschland": "DE",
    "netherlands": "NL", "holland": "NL",
    "united states": "US", "usa": "US", "america": "US",
    "united arab emirates": "AE", "uae": "AE",
    "czechia": "CZ", "czech republic": "CZ",
    "south korea": "KR", "korea": "KR",
    "spain": "ES", "france": "FR", "italy": "IT", "poland": "PL",
    "portugal": "PT", "ireland": "IE", "sweden": "SE", "norway": "NO",
    "denmark": "DK", "finland": "FI", "switzerland": "CH", "austria": "AT",
    "belgium": "BE", "greece": "GR", "romania": "RO", "bulgaria": "BG",
    "ukraine": "UA", "israel": "IL", "india": "IN", "canada": "CA",
    "australia": "AU", "brazil": "BR", "singapore": "SG", "japan": "JP",
    "estonia": "EE", "latvia": "LV", "lithuania": "LT", "hungary": "HU",
    "croatia": "HR", "serbia": "RS", "cyprus": "CY", "malta": "MT",
}


# The first spelling listed for each code above, which is the one to show a
# person or hand to another service. Built by walking COUNTRY_NAMES in order
# and keeping the first name seen per code, so "united states" wins over "usa"
# and "america" without a second table to maintain alongside the first.
_NAME_OF_CODE: dict[str, str] = {}
for _name, _code in COUNTRY_NAMES.items():
    _NAME_OF_CODE.setdefault(_code, _name.title())


def country_name(code: str | None) -> str | None:
    """An ISO code spelled out, or None if this module cannot spell it.

    The inverse of the table above, for the places a code has to leave this
    program: a two-letter code is ambiguous to anything that does not know it
    is a country code - LinkedIn's own location lookup reads "CA" as Canada
    and "IN" as India - so it is spelled out before it is sent anywhere.
    """
    if not code:
        return None
    return _NAME_OF_CODE.get(code.strip().upper())


def resolve(names: list[str]) -> tuple[list[str], bool]:
    """Names -> (sorted ISO codes, worldwide_allowed).

    Unrecognised names are dropped rather than guessed at. A silently invented
    country would filter the corpus down to nothing with no visible cause; the
    wizard reports what it could not resolve instead.
    """
    codes: set[str] = set()
    worldwide = False
    for raw in names:
        name = str(raw).strip().lower()
        if not name:
            continue
        if name in WORLDWIDE_NAMES:
            worldwide = True
            continue
        if name in REGIONS:
            codes.update(REGIONS[name])
            continue
        if name in COUNTRY_NAMES:
            codes.add(COUNTRY_NAMES[name])
            continue
        upper = name.upper()
        if len(upper) == 2 and upper in COUNTRY_OFFSETS:
            codes.add(upper)
    return sorted(codes), worldwide


def unresolved(names: list[str]) -> list[str]:
    """Names the resolver could not place, for the wizard to report back."""
    missing: list[str] = []
    for raw in names:
        name = str(raw).strip().lower()
        if not name:
            continue
        known = (
            name in WORLDWIDE_NAMES
            or name in REGIONS
            or name in COUNTRY_NAMES
            or (len(name) == 2 and name.upper() in COUNTRY_OFFSETS)
        )
        if not known:
            missing.append(str(raw).strip())
    return missing
