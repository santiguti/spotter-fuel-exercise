from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from route import views

urlpatterns = [
    path("api/route/", views.route_api, name="route-api"),
    path("map/", views.route_map, name="route-map"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/map/", permanent=False)),
]
