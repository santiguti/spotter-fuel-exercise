# Fuel Route Optimizer

A Django API that takes a start and finish in the USA and returns the driving route, the
cost-optimal places to buy fuel along it, and what that fuel costs — for a vehicle with a 500 mile
range doing 10 miles per gallon.

Every request costs **exactly one call** to the external routing service, and zero on a cache hit.

```
GET /api/route/?start=Dallas, TX&finish=New York, NY
GET /map/?start=Dallas, TX&finish=New York, NY      ← the same result, drawn
```

---

## Setup

Requires Python 3.12 or newer (Django 6.1).

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python manage.py migrate
./.venv/bin/python manage.py load_stations     # loads 6,576 stations, ~1 second
./.venv/bin/python manage.py runserver
```

Then open <http://localhost:8000/map/>, or:

```bash
curl "http://localhost:8000/api/route/?start=Dallas,%20TX&finish=New%20York,%20NY"
```

No API keys and no accounts: the routing service is keyless and the station coordinates ship with
the repository. Nothing needs configuring, though `.env.example` documents every setting.

Run the tests with `./.venv/bin/pytest`.

---

## The API

### `GET /api/route/`

| Parameter | Required | Default | Meaning |
|---|---|---|---|
| `start` | yes | — | `"Dallas, TX"`, `"Dallas, Texas"`, or `"32.77,-96.79"` |
| `finish` | yes | — | same formats |
| `start_full` | no | `true` | `true`: the vehicle leaves with a full tank. `false`: it leaves empty, so every gallon burned is paid for |
| `min_purchase_gallons` | no | `5` | Smallest purchase worth stopping for. `0` gives the strict mathematical optimum |

```jsonc
{
  "start":  { "name": "Dallas, TX",        "latitude": 32.78306, "longitude": -96.80667, "resolved_by": "local index" },
  "finish": { "name": "New York City, NY", "latitude": 40.71427, "longitude": -74.00597, "resolved_by": "local index" },

  "total_distance_miles": 1548.9,
  "total_gallons_purchased": 104.88,
  "total_cost_usd": 300.74,

  "total_fuel_burned_gallons": 154.89,
  "total_cost_including_starting_tank_usd": 440.79,

  "fuel_stops": [
    {
      "order": 1,
      "name": "EXTRA MILE TRUCK STOP",
      "address": "I-30 EXIT 208",
      "city": "Hooks", "state": "TX",
      "latitude": 33.46623, "longitude": -94.28853,
      "price_per_gallon": 2.817,
      "gallons": 16.32,
      "cost_usd": 45.97,
      "distance_from_start_miles": 163.2,
      "miles_off_route": 0.7
    }
  ],

  "route": { "type": "LineString", "coordinates": [[-96.80667, 32.78306], "..."] },
  "assumptions":  { "vehicle_range_miles": 500.0, "miles_per_gallon": 10.0,
                    "started_with_full_tank": true, "max_detour_off_route_miles": 10.0 },
  "performance":  { "routing_api_calls": 1, "stations_in_corridor": 335, "elapsed_ms": 412.0 },
  "map_url": "http://localhost:8000/map/?start=Dallas,+TX&finish=New+York,+NY"
}
```

**Two totals, because the brief is ambiguous.** It fixes the range at 500 miles but never says what
is in the tank at the start, and that is a $301 versus $441 difference on the same drive. Rather than
pick one silently, the response reports both and states which model produced the stop list.

### Status codes

| Code | Meaning |
|---|---|
| `200` | Route planned |
| `400` | Unusable input — missing parameter, or a place that cannot be resolved |
| `422` | The request was fine, but the trip cannot be driven: no road connects the two places, or there is a gap longer than the vehicle's range |
| `502` | The routing service itself failed |

`422` and `502` are deliberately distinct. Asking to drive from Honolulu to Dallas is an impossible
request, not an outage, and the caller should be able to tell whose problem it is.

---

## How it stays at one API call

The price list gives a city and a state for each of 8,151 stations — **no coordinates**. Geocoding
3,893 distinct towns per request would be thousands of calls against a budget of one, so none of it
happens at request time:

| Step | Cost at request time |
|---|---|
| Geocode 6,576 stations | **0** — done once, offline, committed as `stations/data/stations_geocoded.csv` |
| Resolve the caller's start and finish | **0** — a committed 17,453-city lookup table |
| Fetch the driving route | **1** — OSRM, cached for an hour |
| Find stations along the route | **0** — in memory |
| Choose the stops | **0** — in memory |

`manage.py geocode_stations` is the offline step. It resolves the price list against the free
GeoNames US dump and writes the two data files the app reads. It is run by hand during development
and is not reachable from any URL; you never need to run it, because its output is committed.

A test asserts the call count, because this is the kind of constraint that regresses silently.

### Performance

| | cold | cached |
|---|---|---|
| Phoenix → Chicago | 2.22 s | **0.025 s** |

About 2.1 s of the cold figure is OSRM's own response time. Everything this service does — scanning
6,576 stations against the route and planning the stops — is roughly 25 ms.

Two things make that possible. The 6,576 stations are read into memory once per process rather than
queried per request. And the corridor scan, which would naively be 6,576 stations × 21,000 route
points ≈ 139 million distance calculations, thins the route to one point per mile and buckets those
points into a coordinate grid, so each station only examines the few points that could be near it.
A station in Florida never looks at a route point in Montana.

---

## Choosing the stops

The minimum-cost refuelling problem has a greedy solution that is optimal, not approximate. At every
station the vehicle is in one of two situations, and each has a forced answer:

- **Something cheaper is within range** — buy the minimum needed to reach the nearest cheaper
  station. Any extra bought here could have been bought there for less.
- **Nothing cheaper is within range** — this is the best fuel for a while, so fill the tank and aim
  for the cheapest station still reachable.

Because both moves are locally forced, the greedy result is globally optimal, which is why there is
no dynamic programming table and no search. `tests/test_planner.py` checks it against an exhaustive
search over 40 random routes.

On Dallas → New York this pays **$2.87 a gallon against a $3.40 average** for the stations it passed.

### One deliberate departure from the strict optimum

The pure optimum tops the tank up at every marginally cheaper station, which on Miami → Seattle
means 21 stops, some buying **a tenth of a gallon**. Those are optimal on paper and not plans anyone
would follow. So a stop has to be worth making: trivial top-ups are skipped where the vehicle can
reach the next station without them, and genuinely necessary purchases are rounded up rather than
skipped, since skipping those would strand the vehicle.

It costs **0.18%** — $1.52 on an $849 trip — and turns 21 stops into 13. Pass
`min_purchase_gallons=0` for the strict optimum; both are tested.

---

## Assumptions and limitations

**Station coordinates are town centroids**, not the pumps themselves, so a station can sit a few
miles from where it really is. The 10-mile corridor absorbs this. The better source is the
`Address` column — `I-44, EXIT 283 & US-69` names the exact interstate and exit — but converting
exits to coordinates needs a highway-exit dataset that no free service exposes as cleanly as
GeoNames exposes towns. That is the first thing to improve with more time.

**Stations whose town cannot be identified are dropped, not guessed at.** Tennessee has twelve
places called Antioch, all recorded with a population of zero, so a "most populous wins" tie-break
would fall back to file order. Measured: 470 of 6,605 candidate stations (7.1%) sit in a town whose
name is ambiguous within its state, and population settles 94% of those. The remaining **29 (0.4%)**
are tied between places more than 25 miles apart, and those are excluded.

This was worth doing rather than documenting. `THORNTONS #607` is listed at `Antioch, TN` with the
address `I-24 EXIT 62`, and the arbitrary tie-break placed it 130 miles from that interstate — far
enough to land it in the corridor of a route it is nowhere near, where it displaced a real stop.
Missing data is honest; confidently wrong coordinates are not.

**620 Canadian rows were dropped.** The nine non-US state codes (ON, AB, BC, MB, SK, YT, QC, NS, NB)
are Trans-Canada highway addresses with Petro-Canada and Husky branding. The brief specifies routes
within the USA, and their median price is 31% higher than the US rows, suggesting different units or
currency — mixing that into a dollar total would corrupt the result.

**Duplicate stations were merged.** 904 rows repeat an OPIS Truckstop ID under a different name
(`PILOT TRAVEL CENTER #1243` and `PILOT #1243`), so the loader keeps one row per ID at the cheapest
price. 8,151 rows become 6,605, and 6,576 after the ambiguous ones above are excluded.

**Geocoding coverage is 99.32%** — 7,480 of 7,531 US rows, yielding 6,576 stations. The 51 failures
are towns GeoNames does not list under that name (`WILLOW BEACH, AZ`) or cannot pin down (`ANTIOCH, TN`). Re-run `manage.py geocode_stations` to see the
full report.

**Map tiles come from Esri, not OpenStreetMap.** Two providers were tried first and both failed
the same way — an HTTP 200 carrying a valid PNG that is not a map. openstreetmap.org returns
`x-blocked: Access denied` with a placeholder (its tile policy, enforced), and CARTO returns tiles
stamped "API KEY REQUIRED". Neither is detectable from the response status, so a tile has to be
looked at rather than checked. Esri's basemaps are free with attribution, need no key, and draw
interstate shields, which suits stations addressed as `I-40 EXIT 172`. `MAP_TILE_URL` accepts any
raster provider. Tiles are fetched by the browser, so they cost the API nothing.

**OSRM's public demo server** has no uptime guarantee. It is free and needs no key, which suits an
exercise; production would want a self-hosted instance or a paid provider. Set `OSRM_BASE_URL` to
point elsewhere.

---

## Layout

```
fuelroute/settings.py                  vehicle constants, corridor width, service URLs
stations/
  geonames.py                          offline geocoding dictionary + name normalisation
  models.py  repository.py             the model, and the in-memory index the hot path reads
  management/commands/
    geocode_stations.py                OFFLINE: price list + GeoNames -> committed CSVs
    load_stations.py                   committed CSV -> database
  data/                                the two committed datasets
route/
  services/geocode.py                  "Dallas, TX" -> coordinates, without an API call
  services/routing.py                  the single OSRM call
  services/corridor.py                 which stations are on the route, and how far along
  services/planner.py                  the refuelling algorithm
  services/trip.py                     orchestration + response shaping
  views.py                             /api/route/ and /map/
tests/                                 99 tests, mirroring the source layout
```

`postman_collection.json` covers the demo requests, including the error cases.
