"""Put the pieces together: locations in, route and fuel plan out.

Both the JSON endpoint and the map page go through here, so they can never disagree about what the
cheapest plan is. This is also the one place that counts outbound HTTP calls, which is the
constraint the whole design is built around.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings

from route.services.corridor import stations_along_route, thin_route
from route.services.geocode import Location, resolve
from route.services.planner import FuelPlan, plan_refuelling
from route.services.routing import Route, fetch_route
from stations.repository import all_stations


@dataclass(frozen=True, slots=True)
class Trip:
    start: Location
    finish: Location
    route: Route
    plan: FuelPlan
    start_full: bool
    stations_considered: int
    routing_api_calls: int
    elapsed_ms: float


def plan_trip(
    start_text: str,
    finish_text: str,
    start_full: bool = True,
    min_purchase_gallons: float | None = None,
) -> Trip:
    """Resolve both endpoints, fetch the route, and work out the cheapest way to fuel it."""
    started = time.perf_counter()

    # Both of these normally resolve from the committed city table, at no HTTP cost.
    start = resolve(start_text)
    finish = resolve(finish_text)

    route, api_calls = fetch_route(
        (start.latitude, start.longitude), (finish.latitude, finish.longitude)
    )

    candidates = stations_along_route(route, all_stations(), settings.CORRIDOR_MILES)
    plan = plan_refuelling(
        candidates,
        route.total_miles,
        settings.FUEL_RANGE_MILES,
        settings.FUEL_MPG,
        start_full,
        **({} if min_purchase_gallons is None else {"min_purchase_gallons": min_purchase_gallons}),
    )

    return Trip(
        start=start,
        finish=finish,
        route=route,
        plan=plan,
        start_full=start_full,
        stations_considered=len(candidates),
        routing_api_calls=api_calls,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def to_payload(trip: Trip, *, include_geometry: bool = True) -> dict:
    """Shape a trip as the JSON the API returns."""
    plan: FuelPlan = trip.plan

    # Money is rounded once, here, and every total is derived from the rounded per-stop figures.
    # Otherwise the payload does not reconcile: a reader multiplying the printed gallons by the
    # printed price would not get the printed cost, which looks like a bug in a billing number.
    stops = []
    for order, stop in enumerate(plan.stops, start=1):
        price = round(stop.price, 3)
        gallons = round(stop.gallons, 2)
        stops.append(
            {
                "order": order,
                "name": stop.candidate.station.name,
                "address": stop.candidate.station.address,
                "city": stop.candidate.station.city,
                "state": stop.candidate.station.state,
                "latitude": stop.candidate.station.latitude,
                "longitude": stop.candidate.station.longitude,
                "price_per_gallon": price,
                "gallons": gallons,
                "cost_usd": round(gallons * price, 2),
                "distance_from_start_miles": round(stop.candidate.miles_from_start, 1),
                "miles_off_route": round(stop.candidate.miles_off_route, 1),
            }
        )

    purchased_cost = round(sum(stop["cost_usd"] for stop in stops), 2)
    starting_tank_cost = plan.all_fuel_cost - plan.purchased_cost

    payload = {
        "start": _place(trip.start),
        "finish": _place(trip.finish),
        "total_distance_miles": round(trip.route.total_miles, 1),
        "total_gallons_purchased": round(sum(stop["gallons"] for stop in stops), 2),
        "total_cost_usd": purchased_cost,
        "total_fuel_burned_gallons": round(plan.burned_gallons, 2),
        "total_cost_including_starting_tank_usd": round(purchased_cost + starting_tank_cost, 2),
        "fuel_stops": stops,
        "assumptions": {
            "vehicle_range_miles": settings.FUEL_RANGE_MILES,
            "miles_per_gallon": settings.FUEL_MPG,
            "started_with_full_tank": trip.start_full,
            "max_detour_off_route_miles": settings.CORRIDOR_MILES,
        },
        "performance": {
            "routing_api_calls": trip.routing_api_calls,
            "stations_in_corridor": trip.stations_considered,
            "elapsed_ms": trip.elapsed_ms,
        },
    }

    if include_geometry:
        payload["route"] = {
            "type": "LineString",
            "coordinates": route_geometry(trip.route),
        }
    return payload


def route_geometry(route: Route) -> list[list[float]]:
    """The route as GeoJSON coordinates, thinned to roughly one point per mile.

    OSRM returns about 21,000 points for a cross-country trip: a megabyte of JSON that draws the
    same line on screen as 1,400 points do. The corridor scan already thins the route for its own
    reasons, so the response reuses exactly that, and full precision is never sent over the wire.
    """
    return [
        [round(longitude, 5), round(latitude, 5)]
        for latitude, longitude, _ in thin_route(route)
    ]


def _place(location: Location) -> dict:
    return {
        "name": location.label,
        "latitude": location.latitude,
        "longitude": location.longitude,
        "resolved_by": location.source,
    }
