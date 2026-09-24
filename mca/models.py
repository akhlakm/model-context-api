"""Shared Pydantic models used by MCA transports."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorOut(BaseModel):
    code: str = Field(..., description="Stable machine-readable error code.")
    detail: str = Field(..., description="Human-readable explanation of the error.")
    field: str | None = Field(None, description="Related input field, when applicable.")


class MCAResponseOut(BaseModel):
    title: str = Field(..., description="Title of the Model Context API discovery document.")
    version: float = Field(..., description="Version of the discovery document format.")
    index: str | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="Markdown index describing the available API guides.",
    )
    help: str = Field(..., description="Instructions for requesting guide and schema details.")
    available_guides: list[str] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="Available packaged Markdown guide names.",
    )
    available_operations: dict[str, str] = Field(
        ...,
        description="Map from operation name to relative route and description.",
    )


class APIRouteSchemaOut(BaseModel):
    route: str = Field(..., description="HTTP method and relative route template.")
    description: str = Field(..., description="Human-readable operation description.")
    guides: list[str] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="Relevant guide names for this operation.",
    )
    request_schema: dict[str, Any] | None = Field(None, description="Logical operation input schema.")
    response_schema: dict[str, Any] | None = Field(None, description="Successful response schema.")


class MCADiscoveryOut(BaseModel):
    guides: dict[str, str] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="Requested guide names and Markdown content.",
    )
    operations: dict[str, APIRouteSchemaOut] | None = Field(
        None,
        exclude_if=lambda value: value is None,
        description="Requested operation schemas.",
    )


class DiscoveryParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    guide: str | None = Field(None, description="Comma-separated guide names to read.")
    operation_name: str | None = Field(
        None,
        alias="operation",
        description="Comma-separated operation names whose schemas should be read.",
    )
