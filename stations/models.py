from django.db import models


class FuelStation(models.Model):
    """A truckstop from the OPIS price list, with coordinates added offline.

    Coordinates are the centroid of the station's town, not the pump itself (see
    stations/management/commands/geocode_stations.py). That is typically a few miles out, which is
    immaterial against a 500 mile range and is absorbed by settings.CORRIDOR_MILES.
    """

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    retail_price = models.DecimalField(max_digits=6, decimal_places=3)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
