"""Django middleware used by the composition example."""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, JsonResponse
from django.utils.deprecation import MiddlewareMixin

DEMO_PRINCIPALS: dict[str, dict[str, Any]] = {
    "demo-token": {
        "subject": "demo-user",
        "scopes": {"billing:read", "billing:write", "billing:delete"},
        "invoice_ids": {7},
    },
    "limited-token": {
        "subject": "limited-user",
        "scopes": {"billing:read"},
        "invoice_ids": set(),
    },
}


def authenticate_demo_token(request: HttpRequest) -> dict[str, Any] | None:
    """Return the demo principal associated with the request token."""
    return DEMO_PRINCIPALS.get(request.headers.get("X-Demo-Token"))


class DemoAuthenticationMiddleware(MiddlewareMixin):
    """Authenticate demo API operations while leaving discovery public."""

    @staticmethod
    def _is_public_discovery(request: HttpRequest) -> bool:
        """Return whether the request is the public MCA discovery route."""
        return request.path.rstrip("/") == "/api"

    def process_request(self, request: HttpRequest):
        """Attach a principal to API requests or return a structured 401."""
        if not request.path.startswith("/api/") or self._is_public_discovery(request):
            return None

        principal = authenticate_demo_token(request)
        if principal is None:
            return JsonResponse(
                {
                    "code": "authentication_required",
                    "detail": "A valid X-Demo-Token header is required.",
                    "field": "X-Demo-Token",
                },
                status=401,
            )

        request.auth = principal
        request.demo_middleware_authenticated = True
        return None

    def process_response(self, request: HttpRequest, response):
        """Expose middleware traversal in the example response."""
        if getattr(request, "demo_middleware_authenticated", False):
            response["X-Demo-Middleware"] = "authenticated"
        return response
