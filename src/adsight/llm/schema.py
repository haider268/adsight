"""Turn a Pydantic model into a JSON schema every provider's structured output accepts."""

from typing import Any

from pydantic import BaseModel

DROPPED_KEYS = frozenset({"title", "default", "format"})


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = str(node["$ref"]).rsplit("/", 1)[-1]
        merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
        return _resolve(merged, defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROPPED_KEYS or key == "$defs":
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _resolve(prop, defs) for name, prop in value.items()}
        else:
            out[key] = _resolve(value, defs)
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties", {})
        out["type"] = "object"
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def portable_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Inline every $ref; every object closed (additionalProperties false) with every
    property required; no default, title or format keys. Optional values are modelled by
    the caller as "" or [] (never null)."""
    raw = model.model_json_schema()
    result = _resolve(raw, raw.get("$defs", {}))
    assert isinstance(result, dict)
    return result
