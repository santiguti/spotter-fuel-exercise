"""The single external call the API makes per request.

OSRM's public demo server is free and needs no API key. Asking for `annotations=distance` makes it
return the distance of every segment of the geometry it already computed, so the distance travelled
at each point of the route is OSRM's own number rather than something re-derived from the polyline.
The fuel planner depends on those distances being right.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

METERS_PER_MILE = 1609.344
CACHE_TIMEOUT_SECONDS = 60 * 60


class RoutingError(RuntimeError):
    """The routing service could not produce a route."""


@dataclass(frozen=True, slots=True)
class Route:
    # (longitude, latitude) in GeoJSON order, straight from OSRM.
    coordinates: tuple[tuple[float, float], ...]
    # cumulative_miles[i] is how far along the route coordinates[i] sits.
    cumulative_miles: tuple[float, ...]

    @property
    def total_miles(self) -> float:
        return self.cumulative_miles[-1]


def fetch_route(
    start: tuple[float, float], finish: tuple[float, float]
) -> tuple[Route, int]:
    """Fetch driving directions. Returns the route and how many HTTP calls it cost (0 or 1).

    Coordinates are (latitude, longitude); OSRM wants them the other way round.
    """
    cache_key = "route:{:.4f},{:.4f}:{:.4f},{:.4f}".format(*start, *finish)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, 0

    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{start[1]},{start[0]};{finish[1]},{finish[0]}"
    )
    try:
        response = requests.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
                "annotations": "distance",
            },
            timeout=settings.OSRM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError(
            f"No drivable route between those locations ({payload.get('code', 'unknown')})"
        )

    route = _parse(payload["routes"][0])
    cache.set(cache_key, route, CACHE_TIMEOUT_SECONDS)
    return route, 1


def _parse(payload: dict) -> Route:
    coordinates = [(float(lon), float(lat)) for lon, lat in payload["geometry"]["coordinates"]]

    # One distance per gap between consecutive coordinates, so cumulative[0] is 0.
    segment_meters: list[float] = []
    for leg in payload["legs"]:
        segment_meters.extend(leg["annotation"]["distance"])

    if len(segment_meters) != len(coordinates) - 1:
        raise RoutingError("Routing service returned distances that do not match the geometry")

    cumulative = [0.0]
    for meters in segment_meters:
        cumulative.append(cumulative[-1] + meters / METERS_PER_MILE)

    return Route(coordinates=tuple(coordinates), cumulative_miles=tuple(cumulative))
