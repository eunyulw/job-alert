"""Which country a provider's location label names: ``"us"``, ``"other"``, or ``None`` (unknown).

Providers label locations in incompatible ways ("Cupertino, United States of America",
"US, CA, Santa Clara", "Chicago, IL", "Albion, IN, US", "Minneapolis,MN 55403", "Austin"). The rule
is deliberately conservative so a US-only filter can be strict: a label is US only on positive
evidence, is ``"other"`` when it names another country, and is unknown otherwise — a bare city such
as "Austin" or a count such as "2 Locations" proves nothing.

One label may hold several places separated by ``;`` or ``|``; it is US if any place is, otherwise
``"other"`` if any place is.

Ambiguities settled here: a trailing two-letter code ends a three-part label as a *country* code
("Ahmedabad, GUJARAT, IN" is India, "Toronto, ON, CA" is Canada), but ends a two-part label as a US
*state* ("Chicago, IL", "San Francisco, CA"). "Georgia" is read as the US state.
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

_UNITED_STATES = re.compile(r"\bunited states\b", re.IGNORECASE)
# Upper-case only, as a standalone token, so the English word "us" never counts.
_US_TOKEN = re.compile(r"(?<![A-Za-z])(US|USA|U\.S\.A?\.?)(?![A-Za-z])")
_STATE_AND_ZIP = re.compile(r"^([A-Z]{2})\s+\d{5}(?:-\d{4})?$")
_TWO_LETTERS = re.compile(r"^[A-Z]{2}$")
_THREE_LETTERS = re.compile(r"^[A-Z]{3}$")


def location_country(label: str | None) -> str | None:
    """``"us"``, ``"other"``, or ``None`` for one provider location label."""
    if not label or not label.strip():
        return None
    verdicts = [_place_country(place) for place in re.split(r"[;|]", label)]
    if "us" in verdicts:
        return "us"
    if "other" in verdicts:
        return "other"
    return None


def _place_country(place: str) -> str | None:
    text = " ".join(place.split())
    if not text:
        return None
    if _UNITED_STATES.search(text) or _US_TOKEN.search(text):
        return "us"
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if any(part.casefold() in OTHER_COUNTRIES for part in parts) or (
        text.split()[0].casefold() in OTHER_COUNTRIES
    ):
        return "other"
    last = parts[-1]
    if _THREE_LETTERS.match(last) and len(parts) >= 2:
        return "other"  # an ISO-3 country code that is not USA ("ESP", "CAN")
    if _TWO_LETTERS.match(last) and len(parts) >= 3:
        return "other"  # a country code ending "City, Region, CC"; "US" was caught above
    zipped = _STATE_AND_ZIP.match(last)
    if zipped and zipped.group(1) in US_STATE_CODES:
        return "us"
    if len(parts) == 2 and _TWO_LETTERS.match(last) and last in US_STATE_CODES:
        return "us"
    if any(part.casefold() in US_STATE_NAMES for part in parts):
        return "us"
    return None
