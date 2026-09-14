"""Turn what the caller typed into coordinates, without spending the routing API budget.

The exercise allows one to three calls to the map service per request, and the route itself needs
one. So "Dallas, TX" is resolved from a committed lookup table (stations/data/us_cities.csv,
16,709 cities) rather than a geocoding request. Only free text the table cannot handle falls back
to Nominatim.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings

from stations.geonames import name_aliases, normalize_city

CITY_DATASET = Path(settings.BASE_DIR) / "stations" / "data" / "us_cities.csv"

STATE_CODES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_COORDINATE_PAIR = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class LocationNotFound(ValueError):
    """The caller's text could not be resolved to a point in the USA."""


@dataclass(frozen=True, slots=True)
class Location:
    label: str
    latitude: float
    longitude: float
    source: str  # "coordinates", "local index", or "nominatim"


@lru_cache(maxsize=1)
def _city_index() -> dict[tuple[str, str], tuple[str, float, float]]:
    """(normalized city, state code) -> (display name, latitude, longitude).

    Each city is registered under every spelling someone might type for it, so "New York, NY"
    finds the place GeoNames calls "New York City".
    """
    index: dict[tuple[str, str], tuple[str, float, float]] = {}
    with CITY_DATASET.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entry = (row["city"], float(row["latitude"]), float(row["longitude"]))
            for alias in name_aliases(row["city"]):
                index.setdefault((alias, row["state"]), entry)
    return index


def _parse_coordinates(text: str) -> Location | None:
    match = _COORDINATE_PAIR.match(text)
    if match is None:
        return None
    latitude, longitude = float(match.group(1)), float(match.group(2))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise LocationNotFound(f"Coordinates out of range: {text}")
    return Location(text.strip(), latitude, longitude, "coordinates")


def _resolve_locally(text: str) -> Location | None:
    """Resolve "City, ST" or "City, State Name" from the committed dataset. No network."""
    if "," not in text:
        return None
    city_part, _, state_part = text.rpartition(",")
    state = state_part.strip().upper()
    state = STATE_CODES.get(state, state)
    if len(state) != 2:
        return None

    found = _city_index().get((normalize_city(city_part), state))
    if found is None:
        return None
    name, latitude, longitude = found
    return Location(f"{name}, {state}", latitude, longitude, "local index")


def _resolve_via_nominatim(text: str) -> Location:
    """Last resort for free text the local table cannot handle."""
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={"q": text, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "fuel-route-optimizer/1.0"},
            timeout=settings.OSRM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        results = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise LocationNotFound(f"Could not look up {text!r}: {exc}") from exc

    # Anything unexpected from a third party reads as "not found" rather than a 500.
    try:
        best = results[0]
        return Location(
            best.get("display_name", text), float(best["lat"]), float(best["lon"]), "nominatim"
        )
    except (KeyError, IndexError, TypeError, ValueError):
        raise LocationNotFound(f"No location in the USA matches {text!r}") from None


def resolve(text: str) -> Location:
    """Resolve a location, preferring the offline paths that cost no API call."""
    if not text or not text.strip():
        raise LocationNotFound("Location is required")

    return (
        _parse_coordinates(text)
        or _resolve_locally(text)
        or _resolve_via_nominatim(text)
    )
