"""Attach coordinates to every fuel station, once, offline.

Run by hand during development, never from a web request:

    python manage.py geocode_stations

Reads the raw price list and the GeoNames US dump, then writes the two committed data files the
API runs on:

  stations/data/stations_geocoded.csv  every station, with coordinates
  stations/data/us_cities.csv          city -> coordinates, for resolving the caller's start/finish

Both are committed, so a fresh clone needs neither the 68 MB dump nor any network access to
geocode anything.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stations.geonames import (
    CANADIAN_PROVINCES,
    CITY_INDEX_MIN_POPULATION,
    build_city_index,
    download_dump,
    normalize_city,
)

DATA_DIR = Path(settings.BASE_DIR) / "stations" / "data"
DEFAULT_SOURCE = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"
DEFAULT_OUTPUT = DATA_DIR / "stations_geocoded.csv"
DEFAULT_CITIES = DATA_DIR / "us_cities.csv"
DEFAULT_DUMP = DATA_DIR / "geonames" / "US.zip"

OUTPUT_FIELDS = [
    "opis_id",
    "name",
    "address",
    "city",
    "state",
    "retail_price",
    "latitude",
    "longitude",
]


class Command(BaseCommand):
    help = "Geocode the fuel price CSV against GeoNames and write the committed station dataset."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
        parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        parser.add_argument("--cities", type=Path, default=DEFAULT_CITIES)
        parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP)

    def handle(self, *args, **options) -> None:
        source: Path = options["source"]
        output: Path = options["output"]

        if not source.exists():
            raise CommandError(f"Price list not found: {source}")

        self.stdout.write("Fetching GeoNames US dump (cached after the first run)...")
        dump_path = download_dump(options["dump"])
        city_index = build_city_index(dump_path)
        self.stdout.write(f"  indexed {len(city_index):,} US populated places")

        with source.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        stats = Counter()
        # Keyed by OPIS id: the same truckstop appears several times under slightly different
        # names ("PILOT TRAVEL CENTER #1243" / "PILOT #1243"). Keep the cheapest price for each.
        stations: dict[int, dict] = {}
        unmatched: Counter = Counter()
        # Every town the price list mentions, so the city index can cover all of them.
        station_towns: set[tuple[str, str]] = set()

        for row in rows:
            stats["read"] += 1
            state = row["State"].strip().upper()
            if state in CANADIAN_PROVINCES:
                stats["dropped_non_us"] += 1
                continue

            city = row["City"].strip()
            town_key = (normalize_city(city), state)
            place = city_index.get(town_key)
            if place is None:
                stats["dropped_ungeocoded"] += 1
                unmatched[(city.upper(), state)] += 1
                continue

            station_towns.add(town_key)
            # ponytail: coordinates are the town centroid, so a station lands a few miles from its
            # actual pump. settings.CORRIDOR_MILES is wide enough to absorb that. The better source
            # is row["Address"] ("I-44, EXIT 283 & US-69"), which names the exact interstate and
            # exit; upgrade to that if stop positions ever need to be street-accurate, but it needs
            # a highway-exit dataset that no free service exposes as cleanly as GeoNames does.
            opis_id = int(row["OPIS Truckstop ID"])
            price = float(row["Retail Price"])
            existing = stations.get(opis_id)
            if existing is not None:
                stats["deduplicated"] += 1
                if price >= existing["retail_price"]:
                    continue

            stations[opis_id] = {
                "opis_id": opis_id,
                "name": row["Truckstop Name"].strip(),
                "address": row["Address"].strip(),
                "city": city,
                "state": state,
                "retail_price": round(price, 3),
                "latitude": round(place.latitude, 6),
                "longitude": round(place.longitude, 6),
            }

        self._write_cities(options["cities"], city_index, station_towns)

        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            writer.writerows(sorted(stations.values(), key=lambda s: s["opis_id"]))

        us_rows = stats["read"] - stats["dropped_non_us"]
        geocoded = us_rows - stats["dropped_ungeocoded"]
        self.stdout.write("")
        self.stdout.write(f"  rows read              {stats['read']:>7,}")
        self.stdout.write(f"  dropped, not US        {stats['dropped_non_us']:>7,}")
        self.stdout.write(
            f"  geocoded               {geocoded:>7,}  "
            f"({geocoded / us_rows:.2%} of US rows)"
        )
        self.stdout.write(f"  dropped, no match      {stats['dropped_ungeocoded']:>7,}")
        self.stdout.write(f"  duplicate ids merged   {stats['deduplicated']:>7,}")
        self.stdout.write(f"  stations written       {len(stations):>7,}")

        if unmatched:
            self.stdout.write("")
            self.stdout.write(f"  unmatched towns ({len(unmatched)}):")
            for (city, state), count in unmatched.most_common(20):
                self.stdout.write(f"    {city}, {state} ({count})")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Wrote {output}"))

    def _write_cities(self, path: Path, city_index: dict, station_towns: set) -> None:
        """Write the lookup table that resolves the caller's start and finish, with no API call.

        The price list decides most of this: every town with a station goes in regardless of size,
        because those are the places this API gets asked about. The population floor only adds the
        big cities the price list cannot know about, since truckstops sit outside them. Anything
        in neither set still resolves through the Nominatim fallback.
        """
        from_stations = {
            key: place for key, place in city_index.items() if key in station_towns
        }
        from_population = {
            key: place
            for key, place in city_index.items()
            if place.population >= CITY_INDEX_MIN_POPULATION
        }
        cities = sorted(
            (from_stations | from_population).values(),
            key=lambda place: (place.state, place.name),
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["city", "state", "latitude", "longitude"])
            for place in cities:
                writer.writerow(
                    [place.name, place.state, round(place.latitude, 5), round(place.longitude, 5)]
                )

        station_only = len(from_stations.keys() - from_population.keys())
        self.stdout.write(
            f"  wrote {len(cities):,} cities to {path.name}: "
            f"{len(from_stations):,} station towns from the price list "
            f"({station_only:,} of them below the population floor and only present because the "
            f"CSV asked for them), plus cities over {CITY_INDEX_MIN_POPULATION:,} for endpoints"
        )
