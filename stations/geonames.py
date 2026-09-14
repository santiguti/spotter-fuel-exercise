"""Offline geocoding dictionary built from the free GeoNames US dump.

The fuel price CSV gives a city and a state but no coordinates, and the route planner needs
coordinates for all ~7,500 stations. Geocoding them through a web service at request time would
mean thousands of HTTP calls per route, so instead they are resolved once, offline, against a
downloadable dataset, and the result is frozen into a committed CSV.

Nothing in this module runs during a web request.
"""

from __future__ import annotations

import io
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"

# us_cities.csv resolves the start and finish the caller types, offline. It is the union of:
#
#   1. every town that has a station in the price list, whatever its size. Big Cabin, OK has a
#      population of ~300 and a truckstop, and is exactly the sort of place this API gets asked
#      about. The CSV decides this set, not a population threshold.
#   2. cities above this population, which exist only to cover endpoints the price list cannot
#      know about: 36 of the 100 largest US cities have no truckstop at all, because truckstops
#      sit on interstates outside the city. New York, Los Angeles and Houston are all in that gap.
#
# So population is not the filter. It only decides how many extra endpoint-only cities to carry.
CITY_INDEX_MIN_POPULATION = 1000

# Column positions in the GeoNames dump (tab separated, no header).
# See https://download.geonames.org/export/dump/readme.txt
_COL_ASCII_NAME = 2
_COL_LATITUDE = 4
_COL_LONGITUDE = 5
_COL_FEATURE_CLASS = 6
_COL_ADMIN1 = 10  # two letter state code for US rows
_COL_POPULATION = 14

# "P" is GeoNames' feature class for populated places: cities, towns, villages.
_POPULATED_PLACE = "P"

# Canadian provinces and territories. The CSV mixes them in with the US stations
# (Edmonton AB on the Trans-Canada, Whitehorse YT, Moncton NB...) and their prices sit in a
# visibly different regime, so they are dropped rather than silently priced in US dollars.
CANADIAN_PROVINCES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)

_PUNCTUATION = re.compile(r"[^A-Z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_city(name: str) -> str:
    """Reduce a city name to a form that matches across both datasets.

    The CSV and GeoNames disagree on punctuation and on how saint/mac names are spelled:
    "SAINT LOUIS" vs "ST. LOUIS", "MC CALLA" vs "MCCALLA", "WINSTON SALEM" vs "WINSTON-SALEM".
    Collapsing both sides the same way recovers those matches.
    """
    text = _PUNCTUATION.sub(" ", name.upper())
    text = _WHITESPACE.sub(" ", text).strip()
    text = re.sub(r"^SAINTE\b", "STE", text)
    text = re.sub(r"^SAINT\b", "ST", text)
    text = re.sub(r"^ST\b\s+", "ST ", text)
    text = re.sub(r"^MC\b\s+", "MC", text)
    return text.replace(" ", "")


def name_aliases(name: str) -> set[str]:
    """Normalized spellings a caller might reasonably type for this place.

    GeoNames calls it "New York City"; people type "New York". Indexing the trailing-"City" form
    under both costs nothing and no real place collides with another by losing that suffix.
    """
    normalized = normalize_city(name)
    aliases = {normalized}
    if normalized.endswith("CITY") and len(normalized) > 4:
        aliases.add(normalized.removesuffix("CITY"))
    return aliases


def download_dump(destination: Path) -> Path:
    """Fetch the GeoNames US dump, reusing the local copy when it already exists."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    with urllib.request.urlopen(GEONAMES_URL, timeout=300) as response:
        destination.write_bytes(response.read())
    return destination


@dataclass(frozen=True, slots=True)
class Place:
    name: str
    state: str
    latitude: float
    longitude: float
    population: int


def build_city_index(dump_path: Path) -> dict[tuple[str, str], Place]:
    """Map (normalized city, state) to a Place, for every US populated place.

    Where a state has several places with the same normalized name, the most populous wins:
    "Springfield, IL" should be the city of 115,000, not the hamlet that shares its name.
    """
    index: dict[tuple[str, str], Place] = {}
    with zipfile.ZipFile(dump_path) as archive:
        with archive.open("US.txt") as handle:
            for raw_line in io.TextIOWrapper(handle, encoding="utf-8"):
                fields = raw_line.split("\t")
                if len(fields) <= _COL_POPULATION or fields[_COL_FEATURE_CLASS] != _POPULATED_PLACE:
                    continue
                state = fields[_COL_ADMIN1].strip().upper()
                place = Place(
                    name=fields[_COL_ASCII_NAME],
                    state=state,
                    latitude=float(fields[_COL_LATITUDE]),
                    longitude=float(fields[_COL_LONGITUDE]),
                    population=int(fields[_COL_POPULATION] or 0),
                )
                key = (normalize_city(place.name), state)
                incumbent = index.get(key)
                if incumbent is None or place.population > incumbent.population:
                    index[key] = place
    return index
