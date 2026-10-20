"""The location-to-country rule behind the US-only filter, on labels the providers emit."""

from __future__ import annotations

import pytest

from job_alert.geo import explicit_location_country, location_country

US = [
    "Cupertino, United States of America",  # Apple
    "Sunnyvale, California, USA",  # Amazon
    "Ann Arbor, MI, USA",  # Google
    "Los Gatos,California,United States of America",  # Eightfold
    "Chicago, IL",  # Lever
    "Austin, TX | Remote-Friendly, United States",  # Greenhouse
    "Afton, Texas",  # Greenhouse, full state name
    "Albion, IN, US",  # SmartRecruiters: Indiana, then country code
    "Minneapolis,MN 55403-2542",  # Workday street address with ZIP
    "1530 FM 973 Taylor, TX, USA",
    "US CA Santa Clara",  # Workday posting-path location
    "San Francisco, California, United States",  # Ashby postal address
    "Remote - US",
    "San Francisco, CA",
    "California",
    "Huntsville AL",  # Blue Origin: state code as the last word
    "Billerica (MA)",  # Boston Dynamics
    "Cary North Carolina",  # Caterpillar: state name without a comma
    "California - HQ",  # Nuro
    "New Mexico",  # a state name wins over the country word inside it
    "FL-TAMPA, 5411 SKYCENTER DR, STE 700 & 800",  # Elevance: leading state code and hyphen
    "PA - Philadelphia, 3601 S Broad St",  # Comcast
    "1 Mifflin St PhiladelphiaPA 19148 2017",  # Target: state code glued to a ZIP
    "Lancaster, Tx",  # Kodiak
    "Kermit, TX or Odessa, TX",  # Kodiak: places joined by "or"
    "North Chicago, IL, us",  # AbbVie: a lower-case country code
    "Boise, ID - Main Site",  # Micron: a state code, then a site name
    "Mooresville, NC (SSC) 1999",  # Lowe's
    "Remote-MO",  # Centene
    "McGregor TX  182 Cotton Belt",  # Trane: city, state code, street number
    "San Francisco",  # a bare city on the US list
    "Evendale",  # GE Aerospace's Workday site name
    "US-CT-EAST HARTFORD-ETC ~ 400 Main St ~ BLDG ETC",  # RTX
]
OTHER = [
    "Ahmedabad, GUJARAT, IN",  # India: a three-part SmartRecruiters label ends in the country
    "A Coruña, Galicia, ESP",  # Amazon ISO-3
    "Prague, Czechia",
    "Vancouver,British Columbia,Canada",
    "Alberta, CAN | Remote-Friendly, Canada",
    "Toronto, ON, CA",
    "London, United Kingdom",
    "UK Bristol",
    "Seoul, Korea, Republic of",
    "84030, BAYERN, DE",
    "Singapore,Singapore",
    "Darmstadt (HQ) - Germany",  # BorgWarner: country word after a hyphen
    "Brampton Ontario Canada",  # Rockwell
    "United Kingdom-Cheltenham",  # Northrop Grumman
    "CAN Kanata (2), ON - WR",  # Aptiv: leading ISO-3 code
    "PRC Shanghai",  # Intel
    "SGP",  # Amazon
    "Ludwigshafen, RP, de",  # AbbVie: a lower-case country code
    "CA-QC-LONGUEUIL-J01 ~ 1000 Blvd Marie-Victorin ~ J01 BLDG",  # RTX: Canada, not California
]
UNKNOWN = [
    "Austin - ATX1", "APAC", "", "2 Locations", "Remote", "London", "Melbourne", "San Jose",
    "Greater Seattle Area", "Chennai Tamil Nadu", "Toronto, On",
]


@pytest.mark.parametrize("text", US)
def test_us_locations(text: str) -> None:
    assert location_country(text) == "us"


@pytest.mark.parametrize("text", OTHER)
def test_other_countries(text: str) -> None:
    assert location_country(text) == "other"


@pytest.mark.parametrize("text", UNKNOWN)
def test_ambiguous_locations_are_unknown(text: str) -> None:
    assert location_country(text) is None


def test_a_bare_city_places_a_posting_but_never_overrides_a_stated_country() -> None:
    # Filters read "San Francisco" as the US; adapters still append a country the provider states.
    assert location_country("San Francisco") == "us"
    assert explicit_location_country("San Francisco") is None
    assert explicit_location_country("San Francisco, CA") == "us"
