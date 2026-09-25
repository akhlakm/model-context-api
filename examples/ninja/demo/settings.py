"""Minimal Django settings for the MCA composition example."""

SECRET_KEY = "mca-example-only"
DEBUG = True
ALLOWED_HOSTS = ["*"]

ROOT_URLCONF = "demo.urls"
ASGI_APPLICATION = "demo.asgi.application"

INSTALLED_APPS: list[str] = ["demo"]
MIDDLEWARE: list[str] = []

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DEFAULT_CHARSET = "utf-8"
USE_TZ = True
