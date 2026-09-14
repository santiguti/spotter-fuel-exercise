"""The corridor scan decides which stations the planner is even allowed to consider.

A station wrongly excluded is a cheap stop silently lost; a station wrongly included is a stop the
driver cannot actually reach. Both are invisible in the response, so they are pinned here.
"""

import pytest

from route.services.corridor import haversine_miles, stations_along_route
from route.services.routing import Route
from stations.repository import Station

# A straight run due east along latitude 40, which is roughly Denver to Indianapolis.
ROUTE_LATITUDE = 40.0
START_LONGITUDE = -100.0


def make_route(degrees: float = 10.0, points: int = 400) -> Route:
    coordinates = []
    cumulative = [0.0]
    for index in range(points):
        longitude = START_LONGITUDE + degrees * index / (points - 1)
        coordinates.append((longitude, ROUTE_LATITUDE))
        if index:
            previous = coordinates[index - 1]
            cumulative.append(
                cumulative[-1]
                + haversine_miles(ROUTE_LATITUDE, previous[0], ROUTE_LATITUDE, longitude)
            )
    return Route(coordinates=tuple(coordinates), cumulative_miles=tuple(cumulative))


def make_station(latitude: float, longitude: float, price: float = 3.50, opis_id: int = 1) -> Station:
    return Station(
        opis_id=opis_id,
        name=f"STATION {opis_id}",
        address="",
        city="",
        state="XX",
        price=price,
        latitude=latitude,
        longitude=longitude,
    )


def test_station_on_the_route_is_found_with_its_mileage():
    route = make_route()
    midpoint_longitude = START_LONGITUDE + 5.0
    station = make_station(ROUTE_LATITUDE, midpoint_longitude)

    found = stations_along_route(route, (station,), corridor_miles=25)

    assert len(found) == 1
    assert found[0].miles_off_route < 1
    assert found[0].miles_from_start == pytest.approx(route.total_miles / 2, rel=0.01)


def test_station_far_from_the_route_is_excluded():
    route = make_route()
    # Three degrees of latitude is about 207 miles north of the road.
    station = make_station(ROUTE_LATITUDE + 3.0, START_LONGITUDE + 5.0)

    assert stations_along_route(route, (station,), corridor_miles=25) == []


def test_corridor_width_is_respected_on_both_sides_of_the_boundary():
    route = make_route()
    # One degree of latitude is ~69 miles, so these sit ~14 and ~35 miles off the road.
    just_inside = make_station(ROUTE_LATITUDE + 0.2, START_LONGITUDE + 5.0, opis_id=1)
    just_outside = make_station(ROUTE_LATITUDE + 0.5, START_LONGITUDE + 5.0, opis_id=2)

    found = stations_along_route(route, (just_inside, just_outside), corridor_miles=25)

    assert [candidate.station.opis_id for candidate in found] == [1]


def test_results_are_ordered_by_distance_from_the_start():
    route = make_route()
    stations = (
        make_station(ROUTE_LATITUDE, START_LONGITUDE + 8.0, opis_id=3),
        make_station(ROUTE_LATITUDE, START_LONGITUDE + 2.0, opis_id=1),
        make_station(ROUTE_LATITUDE, START_LONGITUDE + 5.0, opis_id=2),
    )

    found = stations_along_route(route, stations, corridor_miles=25)

    assert [candidate.station.opis_id for candidate in found] == [1, 2, 3]
    assert found == sorted(found, key=lambda candidate: candidate.miles_from_start)


def test_grid_lookup_agrees_with_brute_force():
    """The grid is an optimisation; it must not change the answer."""
    route = make_route()
    stations = tuple(
        make_station(ROUTE_LATITUDE + offset / 10, START_LONGITUDE + offset, opis_id=offset)
        for offset in range(1, 10)
    )

    found = {candidate.station.opis_id for candidate in stations_along_route(route, stations, 25)}
    expected = {
        station.opis_id
        for station in stations
        if min(
            haversine_miles(station.latitude, station.longitude, latitude, longitude)
            for longitude, latitude in route.coordinates
        )
        <= 25
    }

    assert found == expected
