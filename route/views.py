"""The public API: one JSON endpoint, and a map page that renders the same result."""

from __future__ import annotations


from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from route.services.geocode import LocationNotFound
from route.services.planner import PlanningError
from route.services.routing import NoRouteFound, RoutingError
from route.services.trip import plan_trip, to_payload

TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


class BadRequest(ValueError):
    """The query string was not usable."""


def _read_parameters(request: HttpRequest) -> dict:
    """Validate the query string before anything expensive happens."""
    start = request.GET.get("start", "").strip()
    finish = request.GET.get("finish", "").strip()
    missing = [name for name, value in (("start", start), ("finish", finish)) if not value]
    if missing:
        raise BadRequest(
            f"Missing required parameter(s): {', '.join(missing)}. "
            'Example: ?start=Dallas, TX&finish=New York, NY'
        )

    raw_start_full = request.GET.get("start_full", "true").strip().lower()
    if raw_start_full not in TRUTHY | FALSY:
        raise BadRequest("start_full must be true or false")

    parameters = {
        "start_text": start,
        "finish_text": finish,
        "start_full": raw_start_full in TRUTHY,
    }

    raw_minimum = request.GET.get("min_purchase_gallons")
    if raw_minimum is not None:
        try:
            minimum = float(raw_minimum)
        except ValueError:
            raise BadRequest("min_purchase_gallons must be a number") from None
        if minimum < 0:
            raise BadRequest("min_purchase_gallons cannot be negative")
        parameters["min_purchase_gallons"] = minimum

    return parameters


@require_GET
def route_api(request: HttpRequest) -> JsonResponse:
    """GET /api/route/?start=Dallas, TX&finish=New York, NY

    Returns the driving route, the cheapest set of fuel stops for a 500 mile / 10 mpg vehicle, and
    what the fuel costs. Costs exactly one call to the routing service, or zero on a cache hit.
    """
    try:
        parameters = _read_parameters(request)
    except BadRequest as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        trip = plan_trip(**parameters)
    except LocationNotFound as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except (PlanningError, NoRouteFound) as exc:
        # The request was fine; this trip just cannot be driven by this vehicle.
        return JsonResponse({"error": str(exc)}, status=422)
    except RoutingError as exc:
        # The routing service itself is the problem, so this is not the caller's fault.
        return JsonResponse({"error": str(exc)}, status=502)

    payload = to_payload(trip)
    payload["map_url"] = f"{request.build_absolute_uri('/map/')}?{request.GET.urlencode()}"
    return JsonResponse(payload)


@require_GET
def route_map(request: HttpRequest) -> HttpResponse:
    """GET /map/?start=Dallas, TX&finish=New York, NY — the same result, drawn."""
    context: dict = {"start": request.GET.get("start", ""), "finish": request.GET.get("finish", "")}

    if context["start"] and context["finish"]:
        try:
            trip = plan_trip(**_read_parameters(request))
            context["trip"] = to_payload(trip)
        except (BadRequest, LocationNotFound, PlanningError, RoutingError) as exc:
            context["error"] = str(exc)

    return render(request, "route/map.html", context)
