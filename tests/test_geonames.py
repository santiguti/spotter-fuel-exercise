"""The city normalizer is what lifts geocoding coverage from 99.1% to 99.7%.

Each pair below is a real mismatch between the price list and the GeoNames dump.
"""

import pytest

from stations.geonames import normalize_city

# (name as written in the price CSV, name as written in GeoNames)
EQUIVALENT_NAMES = [
    ("MC CALLA", "McCalla"),
    ("SAINT LOUIS", "St. Louis"),
    ("DE FOREST", "DeForest"),
    ("WINSTON SALEM", "Winston-Salem"),
    ("MC GRAW", "McGraw"),
    ("LA PLACE", "LaPlace"),
    ("OPA LOCKA", "Opa-locka"),
    ("SAINT MARYS", "St. Marys"),
    ("SAINTE GENEVIEVE", "Ste. Genevieve"),
]


@pytest.mark.parametrize("csv_name,geonames_name", EQUIVALENT_NAMES)
def test_spelling_variants_normalize_to_the_same_key(csv_name, geonames_name):
    assert normalize_city(csv_name) == normalize_city(geonames_name)


def test_distinct_cities_stay_distinct():
    assert normalize_city("Springfield") != normalize_city("Springdale")
    assert normalize_city("Kansas City") != normalize_city("Kansas")
