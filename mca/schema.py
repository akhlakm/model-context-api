"""Shared JSON Schema helpers for MCA adapters."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from typing import Any, Callable

_COMPONENT_REF_PREFIX = "#/components/schemas/"


def is_generic_schema(schema: Any) -> bool:
    """Return whether a local schema carries no useful structural contract."""
    if not isinstance(schema, Mapping) or not schema:
        return True
    if "$ref" in schema:
        return False
    if schema.get("type") not in (None, "object"):
        return False
    return not any(
        key in schema
        for key in (
            "properties",
            "items",
            "enum",
            "const",
            "allOf",
            "anyOf",
            "oneOf",
        )
    )


def build_request_schema(
    sections: Mapping[str, Mapping[str, Any]],
    required_sections: Collection[str],
    *,
    body_schema: Mapping[str, Any] | None = None,
    body_required: bool = False,
    components: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build the common MCA request envelope from adapter-specific sections."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, section in sections.items():
        section_properties = section.get("properties")
        if not section_properties:
            continue
        properties[name] = {
            "description": section["description"],
            "type": "object",
            "properties": section_properties,
            "required": section.get("required", []),
        }
        if name in required_sections:
            required.append(name)

    if body_schema is not None:
        properties["body"] = {
            "description": "JSON request body.",
            **deepcopy(dict(body_schema)),
        }
        if body_required:
            required.append("body")

    if not properties:
        return None

    request_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    if components:
        request_schema["components"] = {"schemas": deepcopy(dict(components))}
    return request_schema


def attach_components(
    schema: dict[str, Any],
    components: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach referenced OpenAPI components to a copied schema."""
    if components:
        schema["components"] = {"schemas": deepcopy(dict(components))}
    return schema


def _transform_refs(
    value: Any,
    on_reference: Callable[[dict[str, Any], str], Any | None],
) -> Any:
    if isinstance(value, list):
        return [_transform_refs(item, on_reference) for item in value]
    if not isinstance(value, dict):
        return value

    reference = value.get("$ref")
    if isinstance(reference, str) and reference.startswith(_COMPONENT_REF_PREFIX):
        replacement = on_reference(value, reference.removeprefix(_COMPONENT_REF_PREFIX))
        if replacement is not None:
            return replacement
    return {
        key: _transform_refs(item, on_reference)
        for key, item in value.items()
    }


def rewrite_component_refs(value: Any, names: Mapping[str, str]) -> Any:
    """Rewrite component references after component names have been merged."""
    def on_reference(value: dict[str, Any], name: str) -> dict[str, Any]:
        rewritten = {
            key: _transform_refs(item, on_reference)
            for key, item in value.items()
        }
        rewritten["$ref"] = f"{_COMPONENT_REF_PREFIX}{names.get(name, name)}"
        return rewritten

    return _transform_refs(value, on_reference)


def merge_remote_fragment(
    target_schema: dict[str, Any],
    remote_schema: Mapping[str, Any],
    fragment: Mapping[str, Any],
    public_operation: str,
) -> dict[str, Any]:
    """Copy a remote fragment and its components into a public schema."""
    remote_components = (
        remote_schema.get("components", {}).get("schemas", {})
        if isinstance(remote_schema.get("components", {}), Mapping)
        else {}
    )
    target_components = target_schema.setdefault("components", {}).setdefault("schemas", {})
    names: dict[str, str] = {}
    for name, component in remote_components.items():
        candidate = name
        if candidate in target_components and target_components[candidate] != component:
            candidate = f"{public_operation}__{name}"
            suffix = 2
            while candidate in target_components and target_components[candidate] != component:
                candidate = f"{public_operation}__{name}_{suffix}"
                suffix += 1
        names[name] = candidate

    for name, component in remote_components.items():
        candidate = names[name]
        if candidate not in target_components:
            target_components[candidate] = rewrite_component_refs(
                deepcopy(component),
                names,
            )

    if not target_components:
        target_schema.pop("components", None)
    return rewrite_component_refs(deepcopy(fragment), names)


def materialize_schema(schema: Any) -> Any:
    """Return a self-contained JSON Schema fragment without MCA components."""
    if not isinstance(schema, Mapping):
        return schema

    materialized = deepcopy(dict(schema))
    component_container = materialized.pop("components", {})
    components = (
        component_container.get("schemas", {})
        if isinstance(component_container, Mapping)
        else {}
    )
    if not isinstance(components, Mapping) or not components:
        return materialized

    resolving: set[str] = set()
    recursive = False

    def definitions_reference(value: dict[str, Any], name: str) -> dict[str, Any] | None:
        if name not in components:
            return None
        rewritten = {"$ref": f"#/$defs/{name}"}
        rewritten.update(
            {
                key: _transform_refs(item, definitions_reference)
                for key, item in value.items()
                if key != "$ref"
            }
        )
        return rewritten

    def expanding_reference(value: dict[str, Any], name: str) -> dict[str, Any] | None:
        nonlocal recursive
        if name not in components:
            return None
        if name in resolving:
            recursive = True
            expanded: dict[str, Any] = {"$ref": f"#/$defs/{name}"}
        else:
            resolving.add(name)
            expanded = _transform_refs(deepcopy(components[name]), expanding_reference)
            resolving.remove(name)
        expanded.update(
            {
                key: _transform_refs(item, expanding_reference)
                for key, item in value.items()
                if key != "$ref"
            }
        )
        return expanded

    materialized = _transform_refs(materialized, expanding_reference)
    if recursive and isinstance(materialized, dict):
        materialized["$defs"] = {
            name: _transform_refs(component, definitions_reference)
            for name, component in components.items()
        }
    return materialized
