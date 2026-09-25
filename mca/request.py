"""Shared Django request construction for internal MCA execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from django.http import HttpRequest


def build_request(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
) -> HttpRequest:
    """Build a synthetic Django request with anonymous-user defaults."""
    from django.test import RequestFactory

    request_factory = RequestFactory()
    request_headers = dict(headers or {})
    if body is None:
        request = request_factory.generic(method, path, headers=request_headers)
    else:
        request = request_factory.generic(
            method,
            path,
            data=json.dumps(body).encode("utf-8"),
            content_type="application/json",
            headers=request_headers,
        )

    try:
        from django.contrib.auth.models import AnonymousUser
    except (ImportError, RuntimeError):
        request.user = None
    else:
        request.user = AnonymousUser()
    return request
