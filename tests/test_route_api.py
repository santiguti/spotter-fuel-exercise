"""End-to-end tests for the endpoint, with the routing service mocked.

The important one is test_costs_exactly_one_routing_api_call. "One call to the map API is ideal"
is the constraint the whole design exists to satisfy, and it is the kind of thing that regresses
silently — someone adds a geocoding lookup and nothing visibly breaks. So it is asserted.
"""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.urls import reverse

from stations.models import FuelStation
from stations.repository import reload_stations

# A straight run east along latitude 40, at the ~quarter-mile point spacing OSRM really returns,
# giving a route about 1,300 miles long.
ROUTE_LATITUDE = 40.0
POINT_SPACING_DEGREES = 0.005
POINT_COUNT = 5000
METERS_PER_DEGREE_AT_40N = 85_275.0  # 111,320 m * cos(40 degrees)


def osrm_payload():
    coordinates = [
        [-100.0 + POINT_SPACING_DEGREES * index, ROUTE_LATITUDE] for index in range(POINT_COUNT)
    ]
    distances = [METERS_PER_DEGREE_AT_40N * POINT_SPACING_DEGREES] * (POINT_COUNT - 1)
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": {"type": "LineString", "coordinates": coordinates},
                "legs": [{"annotation": {"distance": distances}}],
            }
        ],
    }


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture
def stations(db):
    """Stations every ~120 miles along the mocked route, cheap in the middle."""
    prices = [3.80, 3.60, 2.90, 3.10, 3.40, 3.70, 3.50, 3.30]
    FuelStation.objects.bulk_create(
        FuelStation(
            opis_id=index,
            name=f"TRUCKSTOP {index}",
            address=f"I-80 EXIT {index}",
            city=f"Town {index}",
            state="NE",
            retail_price=Decimal(str(price)),
            latitude=ROUTE_LATITUDE,
            longitude=-100.0 + 3.0 * index,
        )
        for index, price in enumerate(prices)
    )
    reload_stations()
    cache.clear()
    yield
    reload_stations()
    cache.clear()


@pytest.fixture
def osrm():
    with patch("route.services.routing.requests.get") as mocked:
        mocked.return_value = FakeResponse(osrm_payload())
        yield mocked


def get(client, **params):
    params.setdefault("start", "39.9, -100.0")
    params.setdefault("finish", "40.0, -75.1")
    return client.get(reverse("route-api"), params)


def test_costs_exactly_one_routing_api_call(client, stations, osrm):
    """The headline constraint. Endpoints are geocoded locally, so only the route costs a call."""
    response = get(client)

    assert response.status_code == 200
    assert osrm.call_count == 1
    assert response.json()["performance"]["routing_api_calls"] == 1


def test_repeating_a_request_costs_no_calls_at_all(client, stations, osrm):
    get(client)
    response = get(client)

    assert osrm.call_count == 1, "the second request should have been served from cache"
    assert response.json()["performance"]["routing_api_calls"] == 0


def test_response_has_the_shape_the_brief_asks_for(client, stations, osrm):
    body = get(client).json()

    assert body["route"]["type"] == "LineString"
    assert len(body["route"]["coordinates"]) > 1
    assert body["total_distance_miles"] > 0
    assert body["total_cost_usd"] > 0
    assert body["fuel_stops"], "a trip this long needs at least one stop"

    for expected_order, stop in enumerate(body["fuel_stops"], start=1):
        assert stop["order"] == expected_order
        assert stop["cost_usd"] == pytest.approx(stop["gallons"] * stop["price_per_gallon"], abs=0.01)
        assert stop["miles_off_route"] <= body["assumptions"]["max_detour_off_route_miles"]


def test_stops_are_ordered_and_within_range_of_each_other(client, stations, osrm):
    body = get(client).json()
    range_miles = body["assumptions"]["vehicle_range_miles"]

    miles = [stop["distance_from_start_miles"] for stop in body["fuel_stops"]]
    assert miles == sorted(miles)
    for previous, current in zip(miles, miles[1:]):
        assert current - previous <= range_miles


def test_totals_add_up(client, stations, osrm):
    body = get(client).json()

    assert body["total_cost_usd"] == pytest.approx(
        sum(stop["cost_usd"] for stop in body["fuel_stops"]), abs=0.01
    )
    assert body["total_gallons_purchased"] == pytest.approx(
        sum(stop["gallons"] for stop in body["fuel_stops"]), abs=0.01
    )
    assert body["total_fuel_burned_gallons"] == pytest.approx(
        body["total_distance_miles"] / body["assumptions"]["miles_per_gallon"], rel=1e-6
    )


def test_starting_empty_costs_more_and_reports_it(client, stations, osrm):
    full = get(client).json()
    empty = get(client, start_full="false").json()

    assert empty["total_cost_usd"] > full["total_cost_usd"]
    # Summing per-stop gallons that were each rounded to 2dp drifts a cent or two from the total.
    assert empty["total_gallons_purchased"] == pytest.approx(
        empty["total_fuel_burned_gallons"], abs=0.05
    )
    assert full["total_cost_including_starting_tank_usd"] > full["total_cost_usd"]


def test_the_route_payload_is_thinned(client, stations, osrm):
    """OSRM's full geometry is used for the corridor scan but never sent over the wire."""
    body = get(client).json()

    assert len(body["route"]["coordinates"]) < POINT_COUNT
    assert len(json.dumps(body)) < 200_000


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"finish": ""}, 400),
        ({"start": ""}, 400),
        ({"start_full": "maybe"}, 400),
        ({"min_purchase_gallons": "lots"}, 400),
        ({"min_purchase_gallons": "-5"}, 400),
        ({"start": "Nowheresville, ZZ"}, 400),
    ],
)
def test_bad_input_is_rejected_with_an_explanation(client, stations, osrm, params, expected):
    response = get(client, **params)

    assert response.status_code == expected
    assert response.json()["error"]


def test_unroutable_trip_is_the_callers_problem_not_an_outage(client, stations, osrm):
    """No road to Hawaii is a 422, not a 502: the routing service did its job."""
    osrm.return_value = FakeResponse({"code": "NoRoute", "message": "Impossible route"})

    response = get(client)

    assert response.status_code == 422
    assert "No drivable route" in response.json()["error"]


def test_routing_outage_is_reported_as_an_upstream_failure(client, stations, osrm):
    import requests

    osrm.side_effect = requests.ConnectionError("connection refused")

    response = get(client)

    assert response.status_code == 502


def test_route_with_no_reachable_stations_explains_why(client, db, osrm):
    """An empty station table means the trip cannot be planned, and the error should say so."""
    FuelStation.objects.all().delete()
    reload_stations()
    cache.clear()

    response = get(client)

    assert response.status_code == 422
    assert "station" in response.json()["error"].lower()
    reload_stations()


def test_map_page_renders_the_same_trip(client, stations, osrm):
    response = client.get(reverse("route-map"), {"start": "39.9, -100.0", "finish": "40.0, -75.1"})

    assert response.status_code == 200
    assert b"trip-data" in response.content


def test_map_page_escapes_station_names(client, stations, osrm, db):
    """Station and place names reach the page as text, never as markup.

    Names come from a third-party price list and, on the geocoding fallback, from OpenStreetMap.
    Django's json_script escapes them on the way into the page, and the script escapes them again
    on the way into innerHTML.
    """
    hostile = "<img src=x onerror=alert(1)>TRUCKSTOP"
    FuelStation.objects.filter(opis_id=2).update(name=hostile)
    reload_stations()
    cache.clear()

    response = client.get(reverse("route-map"), {"start": "39.9, -100.0", "finish": "40.0, -75.1"})

    assert response.status_code == 200
    assert hostile.encode() not in response.content
    assert b"<img src=x" not in response.content
