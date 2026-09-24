"""Public URL configuration for the MCA composition example."""

from django.urls import path
from ninja import NinjaAPI

from .api import public_router

api = NinjaAPI(title="MCA Composition Example", version="1.0")
api.add_router("/api", public_router)

urlpatterns = [path("", api.urls)]
