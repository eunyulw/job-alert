"""Which country a provider's location label names: ``"us"``, ``"other"``, or ``None`` (unknown).

Providers label locations in incompatible ways ("Cupertino, United States of America",
"US, CA, Santa Clara", "Chicago, IL", "Albion, IN, US", "Minneapolis,MN 55403", "Austin"). The rule
is deliberately conservative so a US-only filter can be strict: a label is US only on positive
evidence, is ``"other"`` when it names another country, and is unknown otherwise — a bare city such
as "Austin" or a count such as "2 Locations" proves nothing.

One label may hold several places separated by ``;``, ``|`` or `` or ``; it is US if any place
is, otherwise ``"other"`` if any place is.

Ambiguities settled here: a trailing two-letter code ends a three-part label as a *country* code,
in either case ("Ahmedabad, GUJARAT, IN" is India, "North Chicago, IL, us" is the US), but ends a
two-part label as a US *state* ("Chicago, IL", "San Francisco, CA", "Boise, ID - Main Site").
"Georgia" is read as the US state.

A bare city name is US only when it is on ``US_CITY_NAMES``: cities where US employers in the
registry post jobs, minus any name a large city abroad shares ("London", "Melbourne", "Cambridge",
"Waterloo", "San Jose", "Hamilton", "Richmond", "Newark", "Burlington" stay unknown).
"""

from __future__ import annotations

import re

US_STATE_CODES = frozenset(
    (
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
        "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
        "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
        "VT", "VA", "WA", "WV", "WI", "WY", "PR",
    )
)
US_STATE_NAMES = frozenset(
    name.casefold()
    for name in (
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
        "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
        "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
        "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
        "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
        "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
        "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
        "Puerto Rico",
    )
)
OTHER_COUNTRIES = frozenset(
    name.casefold()
    for name in (
        "Canada", "Mexico", "United Kingdom", "UK", "England", "Scotland", "Wales", "Ireland",
        "Germany", "France", "Spain", "Italy", "Netherlands", "Switzerland", "Austria",
        "Czechia", "Czech Republic", "Poland", "Sweden", "Norway", "Denmark", "Finland",
        "Belgium", "Portugal", "Luxembourg", "Greece", "Romania", "Hungary", "Bulgaria",
        "Ukraine", "Israel", "India", "China", "Japan", "Korea", "South Korea",
        "Korea, Republic of", "Taiwan", "Hong Kong", "Singapore", "Australia", "New Zealand",
        "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Costa Rica",
        "United Arab Emirates", "UAE", "Saudi Arabia", "Qatar", "Turkey", "Türkiye", "Egypt",
        "South Africa", "Nigeria", "Kenya", "Ghana", "Philippines", "Vietnam", "Viet Nam",
        "Thailand", "Malaysia", "Indonesia", "Kazakhstan", "Serbia", "Croatia", "Slovakia",
        "Estonia", "Latvia", "Lithuania",
    )
)

# Bare labels such as "San Francisco" or "Evendale" (a GE Aerospace site), compared casefolded.
US_CITY_NAMES = frozenset(
    name.casefold()
    for name in (
        "San Francisco", "South San Francisco", "Oakland", "Fremont", "Hayward", "Livermore",
        "Sunnyvale", "Mountain View", "Palo Alto", "Menlo Park", "Redwood City", "San Mateo",
        "Santa Clara", "Cupertino", "Milpitas", "Sacramento", "Los Angeles", "Pasadena",
        "Hawthorne", "El Segundo", "Long Beach", "Torrance", "Huntington Beach", "Redondo Beach",
        "Costa Mesa", "Irvine", "San Diego", "Mojave", "Seattle", "Redmond", "Bellevue",
        "Kirkland", "Everett", "Boston", "Somerville", "Waltham", "New York", "New York City",
        "Brooklyn", "Chicago", "Houston", "Dallas", "Fort Worth", "Austin", "San Antonio",
        "Starbase", "McGregor", "Denver", "Boulder", "Salt Lake City", "Phoenix", "Tempe",
        "Scottsdale", "Chandler", "Albuquerque", "Las Vegas", "Atlanta", "Pittsburgh",
        "Philadelphia", "Detroit", "Ann Arbor", "Dearborn", "Auburn Hills", "Grand Rapids",
        "Minneapolis", "Milwaukee", "Cleveland", "Columbus", "Cincinnati", "Dayton", "Evendale",
        "West Chester", "Indianapolis", "Huntsville", "Orlando", "Miami", "Tampa", "Palm Bay",
        "Raleigh", "Durham", "Charlotte", "Nashville", "Knoxville", "Arlington", "Reston",
        "Herndon", "Bethesda", "Baltimore", "Schenectady", "Greenville", "Peoria", "Moline",
        "Loves Park", "Hooksett", "Lynn", "Kansas City", "St. Louis", "Saint Louis", "Portland",
    )
)

# ISO-3 codes and "PRC" that open a label ("CAN Kanata", "GBR Bristol UK", "PRC Shanghai", "SGP").
LEADING_COUNTRY_CODES = frozenset(
    (
        "CAN", "MEX", "GBR", "IRL", "DEU", "FRA", "ESP", "ITA", "NLD", "CHE", "AUT", "POL", "CZE",
        "SWE", "BEL", "PRT", "HUN", "ROU", "TUR", "ISR", "IND", "CHN", "PRC", "JPN", "KOR", "SGP",
        "AUS", "NZL", "BRA", "ARG", "CHL", "COL", "PHL", "MYS", "THA", "VNM", "IDN", "ZAF",
    )
)

_UNITED_STATES = re.compile(r"\bunited states\b", re.IGNORECASE)
_STATE_NAME_WORDS = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, US_STATE_NAMES), key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
_COUNTRY_WORDS = re.compile(
    r"(?<![A-Za-z])(" + "|".join(sorted(map(re.escape, OTHER_COUNTRIES), key=len, reverse=True))
    + r")(?![A-Za-z])",
    re.IGNORECASE,
)
# "Huntsville AL", "Billerica (MA)": a state code as the last word, outside the comma rules.
_TRAILING_STATE = re.compile(r"\s\(?([A-Z]{2})\)?$")
# "FL-TAMPA, ...", "PA - Philadelphia": a state code, then a hyphen.
_LEADING_STATE = re.compile(r"^([A-Z]{2})\s*-\s*\S")
# "PhiladelphiaPA 19148 2017": a state code glued to a ZIP code, anywhere in a street address.
_GLUED_ZIP = re.compile(r"([A-Z]{2})\s\d{5}(?:[\s-]\d{4})?\b")
# Upper-case only, as a standalone token, so the English word "us" never counts.
_US_TOKEN = re.compile(r"(?<![A-Za-z])(US|USA|U\.S\.A?\.?)(?![A-Za-z])")
_STATE_AND_ZIP = re.compile(r"^([A-Z]{2})\s+\d{5}(?:-\d{4})?$")
_TWO_LETTERS = re.compile(r"^[A-Z]{2}$")
# "Boise, ID - Main Site", "Mooresville, NC (SSC) 1999": a state code, then a site suffix.
_STATE_THEN_SUFFIX = re.compile(r"^([A-Z]{2})(?:\s*[-–(]|\s+\d)")
# "US-CT-EAST HARTFORD-ETC", "CA-QC-LONGUEUIL-J01": country, region, then city (RTX's labels).
_COUNTRY_REGION_CITY = re.compile(r"^([A-Z]{2})-([A-Z]{2,3})-\S")
# "Remote-MO", "Remote - TX".
_REMOTE_STATE = re.compile(r"^remote\s*[-–:]\s*([A-Z]{2})$", re.IGNORECASE)
# "McGregor TX  182 Cotton Belt": a city, a state code, then a street number.
_CITY_STATE_STREET = re.compile(r"^[A-Z][A-Za-z .'-]* ([A-Z]{2})\s+\d")
_THREE_LETTERS = re.compile(r"^[A-Z]{3}$")


def location_country(label: str | None) -> str | None:
    """``"us"``, ``"other"``, or ``None`` for one provider location label."""
    return _label_country(label, bare_cities=True)


def explicit_location_country(label: str | None) -> str | None:
    """The same verdict from what the label writes out, ignoring ``US_CITY_NAMES``.

    Adapters use it to decide whether a label still needs the provider's own country appended:
    a country the provider states ("Portland" posted in Canada) outranks a guess from a city name.
    """
    return _label_country(label, bare_cities=False)


def _label_country(label: str | None, *, bare_cities: bool) -> str | None:
    if not label or not label.strip():
        return None
    verdicts = [
        _place_country(place, bare_cities=bare_cities)
        for place in re.split(r"[;|]| or ", label)
    ]
    if "us" in verdicts:
        return "us"
    if "other" in verdicts:
        return "other"
    return None


def _place_country(place: str, *, bare_cities: bool) -> str | None:
    text = " ".join(place.split())
    if not text:
        return None
    if _UNITED_STATES.search(text) or _US_TOKEN.search(text):
        return "us"
    if bare_cities and text.casefold() in US_CITY_NAMES:
        return "us"
    if _COUNTRY_REGION_CITY.match(text):
        # A country code leads, so "CA" is Canada here, not California; "US-..." was caught above.
        return "other"
    remote = _REMOTE_STATE.match(text)
    if remote and remote.group(1).upper() in US_STATE_CODES:
        return "us"
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if any(part.casefold() in OTHER_COUNTRIES for part in parts) or (
        text.split()[0].casefold() in OTHER_COUNTRIES
    ):
        return "other"
    last = parts[-1]
    if _THREE_LETTERS.match(last) and len(parts) >= 2:
        return "other"  # an ISO-3 country code that is not USA ("ESP", "CAN")
    if len(parts) >= 3 and re.fullmatch(r"[A-Za-z]{2}", last):
        # A country code ending "City, Region, CC", in either case: "IL, us" is the US.
        return "us" if last.casefold() == "us" else "other"
    zipped = _STATE_AND_ZIP.match(last)
    if zipped and zipped.group(1) in US_STATE_CODES:
        return "us"
    # "Chicago, IL"; also "Lancaster, Tx", since no country code is written in title case.
    if len(parts) == 2 and re.fullmatch(r"[A-Z][A-Za-z]", last) and last.upper() in US_STATE_CODES:
        return "us"
    suffixed = _STATE_THEN_SUFFIX.match(last) if len(parts) == 2 else None
    if suffixed and suffixed.group(1) in US_STATE_CODES:
        return "us"
    if any(part.casefold() in US_STATE_NAMES for part in parts):
        return "us"
    return _free_text_country(text)


def _free_text_country(text: str) -> str | None:
    """Labels that ignore the comma conventions: state or country words, codes without commas.

    A state name wins over a country word, so "New Mexico" is not Mexico.
    """
    if _STATE_NAME_WORDS.search(text):
        return "us"
    for pattern in (_TRAILING_STATE, _LEADING_STATE, _GLUED_ZIP, _CITY_STATE_STREET):
        match = pattern.search(text)
        if match and match.group(1) in US_STATE_CODES:
            return "us"
    if _COUNTRY_WORDS.search(text) or text.split()[0].strip(",") in LEADING_COUNTRY_CODES:
        return "other"
    return None
