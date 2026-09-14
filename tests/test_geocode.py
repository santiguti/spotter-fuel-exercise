"""Start and finish must resolve without spending an API call.

Every case here is expected to be answered from the committed dataset. If one of them starts
reaching for Nominatim, the request budget has regressed.
"""

import pytest

from route.services.geocode import LocationNotFound, resolve

# Big cities with no truckstop of their own, so they exist in the index only because of the
# population floor, plus small towns that exist only because the price list mentions them.
LOCAL_CASES = [
    "Dallas, TX",
    "New York, NY",
    "Los Angeles, CA",
    "Houston, TX",
    "Boston, Massachusetts",
    "Washington, DC",
    "Big Cabin, OK",
    "Gila Bend, AZ",
    "Tomah, WI",
]


@pytest.mark.parametrize("text", LOCAL_CASES)
def test_resolves_without_a_network_call(text):
    assert resolve(text).source == "local index"


def test_new_york_matches_geonames_new_york_city():
    """GeoNames calls it "New York City"; nobody types that."""
    assert resolve("New York, NY").label == resolve("New York City, NY").label


def test_state_may_be_a_code_or_a_full_name():
    assert resolve("Dallas, TX") == resolve("Dallas, Texas")


def test_accepts_raw_coordinates():
    location = resolve("32.7767, -96.797")
    assert location.source == "coordinates"
    assert (round(location.latitude, 4), round(location.longitude, 4)) == (32.7767, -96.797)


def test_rejects_impossible_coordinates():
    with pytest.raises(LocationNotFound):
        resolve("999.0, -96.797")


def test_rejects_empty_input():
    with pytest.raises(LocationNotFound):
        resolve("   ")
