"""Find the stations that sit on the route, and how far along the route each one is.

The naive version compares every station against every point of the route: 6,605 stations against
the ~21,000 points OSRM returns for a cross-country trip is 139 million distance calculations, which
is far too slow to do inside a request.

Two things fix that. The route is thinned to about one point per mile, and those points are dropped
into a coordinate grid, so each station only ever looks at the handful of route points that could
possibly be near it. The work becomes linear in the number of stations, and most stations are
nowhere near the route and cost nine failed dictionary lookups.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from route.services.routing import Route
from stations.repository import Station

EARTH_RADIUS_MILES = 3958.8

# Degrees of longitude are shortest at the northern edge of the continental US (~49 N), where one
# degree is about 45 miles. Sizing cells by that keeps them large enough that a 3x3 block around a
# station is guaranteed to contain every route point within the corridor, anywhere in the country.
MILES_PER_DEGREE_LONGITUDE_MIN = 45.0

# Thinning the route to this spacing is what makes the scan cheap. It also sets how precisely a
# stop's distance-along-route is known, which is irrelevant against a 500 mile tank.
ROUTE_POINT_SPACING_MILES = 1.0


@dataclass(frozen=True, slots=True)
class CandidateStation:
    station: Station
    miles_from_start: float
    miles_off_route: float


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def thin_route(route: Route) -> list[tuple[float, float, float]]:
    """Reduce the route to (latitude, longitude, miles_from_start), about one point per mile."""
    points: list[tuple[float, float, float]] = []
    last_kept = -ROUTE_POINT_SPACING_MILES
    for (longitude, latitude), miles in zip(route.coordinates, route.cumulative_miles):
        if miles - last_kept >= ROUTE_POINT_SPACING_MILES:
            points.append((latitude, longitude, miles))
            last_kept = miles
    # Always keep the destination, so a station near the finish measures against it.
    final_longitude, final_latitude = route.coordinates[-1]
    points.append((final_latitude, final_longitude, route.total_miles))
    return points


def stations_along_route(
    route: Route, stations: tuple[Station, ...], corridor_miles: float
) -> list[CandidateStation]:
    """Every station within corridor_miles of the route, ordered by distance from the start."""
    points = thin_route(route)
    cell_size = corridor_miles / MILES_PER_DEGREE_LONGITUDE_MIN

    grid: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for point in points:
        grid.setdefault((int(point[0] // cell_size), int(point[1] // cell_size)), []).append(point)

    # Comparing in degrees avoids trigonometry per candidate point. Longitude degrees are squeezed
    # by latitude, so scale them before comparing; it only has to rank points, not measure them.
    longitude_scale = math.cos(math.radians(sum(p[0] for p in points) / len(points)))

    candidates: list[CandidateStation] = []
    for station in stations:
        cell_row = int(station.latitude // cell_size)
        cell_column = int(station.longitude // cell_size)

        nearest = None
        smallest = float("inf")
        for row in (cell_row - 1, cell_row, cell_row + 1):
            for column in (cell_column - 1, cell_column, cell_column + 1):
                for point in grid.get((row, column), ()):
                    dy = station.latitude - point[0]
                    dx = (station.longitude - point[1]) * longitude_scale
                    squared = dy * dy + dx * dx
                    if squared < smallest:
                        smallest, nearest = squared, point

        if nearest is None:
            continue

        # One real distance calculation, for the single closest point.
        off_route = haversine_miles(
            station.latitude, station.longitude, nearest[0], nearest[1]
        )
        if off_route <= corridor_miles:
            candidates.append(CandidateStation(station, nearest[2], off_route))

    candidates.sort(key=lambda candidate: candidate.miles_from_start)
    return candidates
