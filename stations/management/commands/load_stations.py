"""Load the geocoded station dataset into the database.

    python manage.py load_stations

Reads stations/data/stations_geocoded.csv, which is committed to the repo, so this needs no
network access and no API key.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.models import FuelStation

DEFAULT_DATASET = Path(settings.BASE_DIR) / "stations" / "data" / "stations_geocoded.csv"


class Command(BaseCommand):
    help = "Load stations/data/stations_geocoded.csv into the FuelStation table."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        dataset: Path = options["dataset"]
        if not dataset.exists():
            raise CommandError(
                f"{dataset} not found. Run `manage.py geocode_stations` first."
            )

        with dataset.open(newline="", encoding="utf-8") as handle:
            stations = [
                FuelStation(
                    opis_id=int(row["opis_id"]),
                    name=row["name"],
                    address=row["address"],
                    city=row["city"],
                    state=row["state"],
                    retail_price=Decimal(row["retail_price"]),
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                )
                for row in csv.DictReader(handle)
            ]

        FuelStation.objects.all().delete()
        FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(stations):,} fuel stations"))
