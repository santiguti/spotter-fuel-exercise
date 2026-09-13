"""In-memory view of the station table.

6,605 stations is small enough to keep in the process. The corridor scan touches every one of them
on every request, so reading them from the database each time would be the slowest thing the API
does, for data that changes only when someone runs `load_stations`.

The database stays the system of record; this is just the index the hot path reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from stations.models import FuelStation


@dataclass(frozen=True, slots=True)
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float


@lru_cache(maxsize=1)
def all_stations() -> tuple[Station, ...]:
    """Every geocoded station, loaded once per process."""
    rows = FuelStation.objects.values_list(
        "opis_id", "name", "address", "city", "state", "retail_price", "latitude", "longitude"
    )
    return tuple(
        Station(
            opis_id=opis_id,
            name=name,
            address=address,
            city=city,
            state=state,
            price=float(price),
            latitude=latitude,
            longitude=longitude,
        )
        for opis_id, name, address, city, state, price, latitude, longitude in rows
    )


def reload_stations() -> None:
    """Drop the cached snapshot. Used by tests and after reloading the dataset."""
    all_stations.cache_clear()
