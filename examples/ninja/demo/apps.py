"""Django application configuration for the MCA composition example."""

from importlib import import_module

from django.apps import AppConfig


class DemoConfig(AppConfig):
    """Load the app API so it can register its MCP router."""

    name = "demo"

    def ready(self) -> None:
        import_module(f"{self.name}.api")
