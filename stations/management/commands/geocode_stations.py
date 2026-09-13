"""Attach coordinates to every fuel station, once, offline.

Run by hand during development, never from a web request:

    python manage.py geocode_stations

Reads the raw price list, resolves each station's city against the GeoNames US dump, and writes
stations/data/stations_geocoded.csv. That output file is committed, so a fresh clone needs neither
the 71 MB dump nor any network access to run the API.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stations.geonames import (
    CANADIAN_PROVINCES,
    build_city_index,
    download_dump,
    normalize_city,
)

DEFAULT_SOURCE = Path(settings.BASE_DIR) / "fuel-prices-for-be-assessment.csv"
DEFAULT_OUTPUT = Path(settings.BASE_DIR) / "stations" / "data" / "stations_geocoded.csv"
DEFAULT_DUMP = Path(settings.BASE_DIR) / "stations" / "data" / "geonames" / "US.zip"

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

        for row in rows:
            stats["read"] += 1
            state = row["State"].strip().upper()
            if state in CANADIAN_PROVINCES:
                stats["dropped_non_us"] += 1
                continue

            city = row["City"].strip()
            coordinates = city_index.get((normalize_city(city), state))
            if coordinates is None:
                stats["dropped_ungeocoded"] += 1
                unmatched[(city.upper(), state)] += 1
                continue

            opis_id = int(row["OPIS Truckstop ID"])
            price = float(row["Retail Price"])
            existing = stations.get(opis_id)
            if existing is not None:
                stats["deduplicated"] += 1
                if price >= existing["retail_price"]:
                    continue

            latitude, longitude = coordinates
            stations[opis_id] = {
                "opis_id": opis_id,
                "name": row["Truckstop Name"].strip(),
                "address": row["Address"].strip(),
                "city": city,
                "state": state,
                "retail_price": round(price, 3),
                "latitude": round(latitude, 6),
                "longitude": round(longitude, 6),
            }

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
