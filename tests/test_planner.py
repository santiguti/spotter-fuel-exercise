"""The planner decides what the trip costs, so every rule it follows is pinned here.

The last test is the important one: it checks the greedy against an exhaustive search on random
routes, which is what justifies calling the result optimal rather than merely plausible.
"""

import random

import pytest

from route.services.corridor import CandidateStation
from route.services.planner import PlanningError, plan_refuelling
from stations.repository import Station

RANGE = 500.0
MPG = 10.0


def candidates(*positions_and_prices: tuple[float, float]) -> list[CandidateStation]:
    """Build stations at given (mile, price) points along a route."""
    return [
        CandidateStation(
            station=Station(
                opis_id=index,
                name=f"STATION {index}",
                address="",
                city="",
                state="XX",
                price=price,
                latitude=0.0,
                longitude=0.0,
            ),
            miles_from_start=miles,
            miles_off_route=0.0,
        )
        for index, (miles, price) in enumerate(positions_and_prices)
    ]


def plan(stations, total_miles, start_full=True, min_purchase_gallons=0.0):
    return plan_refuelling(
        stations, total_miles, RANGE, MPG, start_full, min_purchase_gallons
    )


def test_trip_inside_one_tank_needs_no_stops():
    result = plan(candidates((100.0, 3.00), (200.0, 2.50)), total_miles=400.0)

    assert result.stops == ()
    assert result.purchased_cost == 0.0


def test_buys_the_minimum_at_an_expensive_station_when_something_cheaper_is_ahead():
    """Expensive now, cheap in 100 miles: buy exactly enough to get there, not a drop more."""
    stations = candidates((450.0, 4.00), (550.0, 2.00))
    result = plan(stations, total_miles=900.0)

    first = result.stops[0]
    assert first.price == 4.00
    # 450 miles used of a 500 mile tank, 100 miles to the cheap station: buy 50 miles of range.
    assert first.gallons == pytest.approx(5.0)


def test_fills_the_tank_when_nothing_cheaper_is_within_range():
    """Cheap now, expensive later: take as much as the tank holds."""
    stations = candidates((450.0, 2.00), (900.0, 4.00))
    result = plan(stations, total_miles=1300.0)

    first = result.stops[0]
    assert first.price == 2.00
    # It arrives at mile 450 with 50 miles of range left, so filling the tank buys the other 450.
    assert first.gallons == pytest.approx(450.0 / MPG)


def test_prefers_the_cheaper_of_two_reachable_stations():
    stations = candidates((400.0, 3.50), (450.0, 2.50))
    result = plan(stations, total_miles=800.0)

    assert [stop.price for stop in result.stops] == [2.50]


def test_detour_of_more_than_one_tank_is_rejected():
    stations = candidates((100.0, 3.00), (700.0, 3.00))
    with pytest.raises(PlanningError, match="600 mile gap"):
        plan(stations, total_miles=1200.0)


def test_destination_beyond_the_last_station_is_rejected():
    stations = candidates((100.0, 3.00))
    with pytest.raises(PlanningError, match="beyond the vehicle"):
        plan(stations, total_miles=900.0)


def test_route_with_no_stations_is_rejected():
    with pytest.raises(PlanningError, match="No fuel stations"):
        plan([], total_miles=900.0)


def test_starting_empty_pays_for_every_gallon_burned():
    stations = candidates((0.0, 3.00), (400.0, 3.00))
    result = plan(stations, total_miles=800.0, start_full=False)

    assert result.purchased_gallons == pytest.approx(result.burned_gallons)
    assert result.purchased_gallons == pytest.approx(800.0 / MPG)
    assert result.purchased_cost == pytest.approx(result.all_fuel_cost)


def test_starting_full_leaves_the_first_tank_unpaid_but_still_accounted_for():
    stations = candidates((0.0, 3.00), (400.0, 3.00))
    result = plan(stations, total_miles=800.0, start_full=True)

    assert result.burned_gallons == pytest.approx(80.0)
    assert result.purchased_gallons == pytest.approx(30.0)  # 800 miles less the free 500
    assert result.all_fuel_cost > result.purchased_cost


def test_reported_totals_match_the_individual_stops():
    stations = candidates((200.0, 3.10), (600.0, 2.90), (900.0, 3.40), (1300.0, 3.20))
    result = plan(stations, total_miles=1600.0)

    assert result.purchased_cost == pytest.approx(sum(stop.cost for stop in result.stops))
    assert result.purchased_gallons == pytest.approx(sum(stop.gallons for stop in result.stops))
    for stop in result.stops:
        assert stop.cost == pytest.approx(stop.gallons * stop.price)


def test_minimum_purchase_skips_pointless_stops():
    """Arriving nearly full at a cheap station should not produce a 0.2 gallon stop."""
    stations = candidates((10.0, 2.00), (480.0, 3.00), (900.0, 3.00))
    total = 1300.0

    without_minimum = plan(stations, total, min_purchase_gallons=0.0)
    with_minimum = plan(stations, total, min_purchase_gallons=5.0)

    assert min(stop.gallons for stop in without_minimum.stops) < 5.0
    assert min(stop.gallons for stop in with_minimum.stops) >= 5.0
    assert len(with_minimum.stops) < len(without_minimum.stops)


def test_no_stop_buys_a_pointlessly_small_amount():
    """Every stop in a plan should be one a driver would actually make."""
    stations = candidates(
        (150.0, 2.80), (530.0, 2.90), (670.0, 2.88), (880.0, 2.97), (1170.0, 2.90)
    )
    result = plan(stations, total_miles=1550.0, min_purchase_gallons=5.0)

    assert result.stops
    assert all(stop.gallons >= 5.0 for stop in result.stops), [
        round(stop.gallons, 2) for stop in result.stops
    ]


def test_a_forced_purchase_is_never_skipped_for_being_small():
    """The minimum must not strand the vehicle: fuel needed to continue is always bought."""
    stations = candidates((490.0, 3.00), (980.0, 3.00))
    result = plan(stations, total_miles=1400.0, min_purchase_gallons=5.0)

    assert result.stops, "the vehicle cannot reach mile 1400 without refuelling"
    assert result.purchased_gallons * MPG >= 1400.0 - RANGE - 1e-6


# --------------------------------------------------------------------------------------------
# Optimality
# --------------------------------------------------------------------------------------------


def _cheapest_possible(stations, total_miles, fuel, index=0, memo=None):
    """Exhaustive search over every legal refuelling choice, for small routes.

    At any station an optimal plan only ever buys one of two amounts: exactly enough to reach the
    station it is aiming for, or a full tank. Searching both, for every reachable station, covers
    the whole space.
    """
    positions = [0.0] + [c.miles_from_start for c in stations]
    prices = [0.0] + [c.station.price for c in stations]

    if memo is None:
        memo = {}
    key = (index, round(fuel, 6))
    if key in memo:
        return memo[key]

    remaining = total_miles - positions[index]
    if remaining <= fuel:
        return 0.0

    best = float("inf")
    if index > 0 and remaining <= RANGE:
        best = (remaining - fuel) / MPG * prices[index]

    for nxt in range(index + 1, len(positions)):
        distance = positions[nxt] - positions[index]
        if distance > (RANGE if index > 0 else fuel):
            break
        for purchase in {max(0.0, distance - fuel), max(0.0, RANGE - fuel)}:
            if index == 0 and purchase > 0:
                continue  # cannot buy fuel at the origin
            if fuel + purchase < distance:
                continue
            cost = purchase / MPG * prices[index]
            cost += _cheapest_possible(
                stations, total_miles, fuel + purchase - distance, nxt, memo
            )
            best = min(best, cost)

    memo[key] = best
    return best


@pytest.mark.parametrize("seed", range(40))
def test_greedy_matches_exhaustive_search(seed):
    """On random routes the greedy must find the true cheapest plan, not just a good one."""
    rng = random.Random(seed)
    # Build the route as a chain of drivable gaps, so every generated instance is feasible.
    positions = []
    mile = 0.0
    for _ in range(rng.randint(3, 7)):
        mile += rng.uniform(50, 450)
        positions.append(round(mile, 3))
    stations = candidates(*((p, round(rng.uniform(2.5, 4.5), 3)) for p in positions))
    total_miles = round(positions[-1] + rng.uniform(10, 450), 3)

    greedy = plan(stations, total_miles)
    exhaustive = _cheapest_possible(stations, total_miles, RANGE)

    assert greedy.purchased_cost == pytest.approx(exhaustive, rel=1e-6)
