"""Decide where to stop for fuel, and how much to buy at each stop.

This is the minimum-cost refuelling problem, and the greedy rule below is the known optimal
solution for it, not an approximation. At any station you are in exactly one of two situations:

  * Something cheaper is within range. Then buy the minimum needed to reach the nearest cheaper
    station and nothing more, because every extra gallon bought here could have been bought there
    for less.
  * Nothing cheaper is within range. Then this is the cheapest fuel you will see for a while, so
    fill the tank completely, and aim for the cheapest station you can still reach.

Both moves are locally forced, which is why the greedy result is globally optimal. It also means no
dynamic programming table and no search: one pass down the route.
"""

from __future__ import annotations

from dataclasses import dataclass

from route.services.corridor import CandidateStation

# For the start-empty model, the "fill up before leaving" stop is priced from the cheapest station
# within this distance of the origin, which is what a driver would actually do.
ORIGIN_FILL_RADIUS_MILES = 50.0

# The pure greedy tops the tank up at every cheap station it passes, even when it arrived nearly
# full, which produces stops that buy a tenth of a gallon. Those are optimal on paper and nonsense
# in practice: nobody pulls into a truckstop for 40 cents of fuel. A stop has to be worth making,
# so a top-up smaller than this is skipped whenever the vehicle can reach the next station without
# it. Purchases that are actually needed to keep going are never skipped.
MIN_PURCHASE_GALLONS = 5.0


class PlanningError(RuntimeError):
    """The route cannot be driven with this vehicle."""


@dataclass(frozen=True, slots=True)
class FuelStop:
    candidate: CandidateStation
    gallons: float
    cost: float

    @property
    def price(self) -> float:
        return self.candidate.station.price


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: tuple[FuelStop, ...]
    total_miles: float
    #: Fuel actually bought on this trip, at the stops listed above.
    purchased_gallons: float
    purchased_cost: float
    #: Every gallon the trip burns, including any the vehicle started with.
    burned_gallons: float
    #: What the whole trip's fuel costs, including the starting tank. Equal to purchased_cost when
    #: the vehicle starts empty, higher when it starts full and the first tank was not paid for.
    all_fuel_cost: float


@dataclass(frozen=True, slots=True)
class _Node:
    """A point on the route where fuel may be bought. Node 0 is the origin."""

    miles: float
    price: float
    candidate: CandidateStation | None
    can_buy: bool


def plan_refuelling(
    candidates: list[CandidateStation],
    total_miles: float,
    range_miles: float,
    mpg: float,
    start_full: bool,
    min_purchase_gallons: float = MIN_PURCHASE_GALLONS,
) -> FuelPlan:
    """Work out the cheapest set of fuel stops for this route.

    With ``start_full`` the vehicle leaves with a full tank, so a trip shorter than its range needs
    no stops at all. Without it the tank starts empty and every gallon burned is paid for, which
    adds a fill-up before departure.
    """
    nodes = _build_nodes(candidates, total_miles, start_full)
    fuel = range_miles if start_full else 0.0

    stops: list[FuelStop] = []
    purchased_miles = 0.0
    index = 0
    destination = len(nodes) - 1

    while index != destination:
        node = nodes[index]
        reach = node.miles + (range_miles if node.can_buy else fuel)
        reachable = [
            other for other in range(index + 1, len(nodes)) if nodes[other].miles <= reach
        ]
        if not reachable:
            raise _unreachable(nodes, index, range_miles)

        cheaper = [other for other in reachable if nodes[other].price < node.price]
        if cheaper:
            # Buy only enough to get to the nearest cheaper fuel: anything more could have been
            # bought there for less. The exception is a purchase too small to be worth stopping
            # for, which is rounded up to something a driver would actually buy — the extra fuel
            # is not wasted, it just displaces fuel bought later.
            target = cheaper[0]
            shortfall = (nodes[target].miles - node.miles) - fuel
            if shortfall > 0:
                amount = min(max(shortfall, min_purchase_gallons * mpg), range_miles - fuel)
                purchased_miles += _buy(stops, node, amount, mpg)
                fuel += amount
        else:
            # Nothing cheaper is within range, so this is the best fuel for a while: fill the tank
            # and aim for the cheapest station still reachable.
            without_buying = [
                other for other in reachable if nodes[other].miles <= node.miles + fuel
            ]
            top_up = range_miles - fuel
            # Skip a trivial top-up, but only while the vehicle can still get somewhere without it.
            worth_stopping = top_up / mpg >= min_purchase_gallons or not without_buying
            if node.can_buy and worth_stopping and top_up > 0:
                purchased_miles += _buy(stops, node, top_up, mpg)
                fuel = range_miles
                target = min(reachable, key=lambda other: nodes[other].price)
            else:
                target = min(without_buying, key=lambda other: nodes[other].price)

        fuel -= nodes[target].miles - node.miles
        index = target

    purchased_gallons = purchased_miles / mpg
    purchased_cost = sum(stop.cost for stop in stops)
    burned_gallons = total_miles / mpg

    # The starting tank, if there was one, is fuel the trip burns but never paid for. Price it at
    # the cheapest fuel available near the origin so the "all fuel" figure means something.
    unpaid_gallons = burned_gallons - purchased_gallons
    unpaid_price = _origin_price(candidates) if unpaid_gallons > 1e-9 else 0.0

    return FuelPlan(
        stops=tuple(stops),
        total_miles=total_miles,
        purchased_gallons=purchased_gallons,
        purchased_cost=purchased_cost,
        burned_gallons=burned_gallons,
        all_fuel_cost=purchased_cost + unpaid_gallons * unpaid_price,
    )


def _build_nodes(
    candidates: list[CandidateStation], total_miles: float, start_full: bool
) -> list[_Node]:
    """Origin, then every station, then the destination.

    The destination is priced at zero and cannot be bought from, which is what makes the greedy
    rule handle the end of the trip without a special case: fuel there is free, so "is anything
    cheaper within range?" is true as soon as the destination is reachable, and the answer is to
    buy exactly enough to arrive and no more.
    """
    if not candidates:
        raise PlanningError("No fuel stations found along this route")

    stations = [
        _Node(candidate.miles_from_start, candidate.station.price, candidate, can_buy=True)
        for candidate in candidates
    ]
    finish = _Node(total_miles, 0.0, None, can_buy=False)

    if start_full:
        # The origin is not a station either: the vehicle arrives with fuel it cannot top up.
        # Pricing it at zero means nothing ahead reads as "cheaper", so the vehicle simply drives
        # to the cheapest station it can reach on the tank it already has.
        return [_Node(0.0, 0.0, None, can_buy=False), *stations, finish]

    # Starting empty means filling up before departure, at whatever is cheapest nearby.
    departure = min(
        (c for c in candidates if c.miles_from_start <= ORIGIN_FILL_RADIUS_MILES),
        key=lambda c: c.station.price,
        default=candidates[0],
    )
    return [_Node(0.0, departure.station.price, departure, can_buy=True), *stations, finish]


def _buy(stops: list[FuelStop], node: _Node, miles_of_range: float, mpg: float) -> float:
    """Record a purchase at this node and return the miles of range bought."""
    gallons = miles_of_range / mpg
    stops.append(
        FuelStop(candidate=node.candidate, gallons=gallons, cost=gallons * node.price)
    )
    return miles_of_range


def _origin_price(candidates: list[CandidateStation]) -> float:
    near_origin = [c for c in candidates if c.miles_from_start <= ORIGIN_FILL_RADIUS_MILES]
    return min(c.station.price for c in near_origin or candidates)


def _unreachable(nodes: list[_Node], index: int, range_miles: float) -> PlanningError:
    node = nodes[index]
    nxt = nodes[index + 1]
    gap = nxt.miles - node.miles

    if nxt is nodes[-1]:
        return PlanningError(
            f"The last station on this route is at mile {node.miles:,.0f}, but the destination is "
            f"{gap:,.0f} miles further, beyond the vehicle's {range_miles:,.0f} mile range"
        )
    return PlanningError(
        f"No station within range between mile {node.miles:,.0f} and mile {nxt.miles:,.0f}: "
        f"that is a {gap:,.0f} mile gap and the vehicle's range is {range_miles:,.0f} miles"
    )
