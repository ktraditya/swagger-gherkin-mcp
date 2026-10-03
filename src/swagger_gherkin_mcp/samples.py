"""Generate valid sample values, invalid values and boundary values from schemas.

Everything here is deterministic: the same spec always yields the same scenarios,
which keeps generated feature files diff-friendly in version control.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FORMAT_SAMPLES = {
    "date": "2026-01-15",
    "date-time": "2026-01-15T10:30:00Z",
    "time": "10:30:00",
    "email": "test.user@example.com",
    "uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "uri": "https://example.com/resource",
    "url": "https://example.com/resource",
    "hostname": "example.com",
    "ipv4": "192.168.0.1",
    "ipv6": "2001:db8::1",
    "password": "P@ssw0rd!2026",
    "byte": "U3dhZ2dlcg==",
    "iban": "BE68539007547034",
}

INVALID_FORMATS = {
    "date": "2026-13-45",
    "date-time": "not-a-date-time",
    "email": "not-an-email",
    "uuid": "not-a-uuid",
    "uri": "not a uri",
    "url": "not a url",
    "ipv4": "999.999.999.999",
}


def schema_type(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # OAS 3.1 style ["string", "null"]
        t = next((x for x in t if x != "null"), "string")
    if t:
        return t
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return "string"


def _first_variant(schema: dict) -> dict:
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return schema[key][0]
    return schema


def sample_value(schema: dict | None, name: str = "value", for_request: bool = True, depth: int = 0) -> Any:
    """A valid, realistic value for the schema."""
    if not schema:
        return f"sample-{name}"
    schema = _first_variant(schema)
    for key in ("example", "x-example", "default", "const"):
        if key in schema:
            return schema[key]
    if schema.get("examples") and isinstance(schema["examples"], list):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]

    t = schema_type(schema)
    if t == "object":
        if schema.get("x-circular") or depth > 4:
            return {}
        out = {}
        for prop, sub in (schema.get("properties") or {}).items():
            if for_request and sub.get("readOnly"):
                continue
            if not for_request and sub.get("writeOnly"):
                continue
            out[prop] = sample_value(sub, prop, for_request, depth + 1)
        if not out and isinstance(schema.get("additionalProperties"), dict):
            out["key"] = sample_value(schema["additionalProperties"], "key", for_request, depth + 1)
        return out
    if t == "array":
        count = max(1, int(schema.get("minItems", 1)))
        return [sample_value(schema.get("items") or {}, name, for_request, depth + 1) for _ in range(count)]
    if t == "integer":
        return _sample_number(schema, integer=True)
    if t == "number":
        return _sample_number(schema, integer=False)
    if t == "boolean":
        return True
    # string
    fmt = schema.get("format", "")
    if fmt in FORMAT_SAMPLES:
        return FORMAT_SAMPLES[fmt]
    base = f"sample-{name}"
    lo, hi = schema.get("minLength"), schema.get("maxLength")
    if lo is not None and len(base) < lo:
        base = base + "x" * (lo - len(base))
    if hi is not None and len(base) > hi:
        base = ("x" * hi) if hi < 3 else base[:hi]
    return base


def _sample_number(schema: dict, integer: bool):
    lo, hi = schema.get("minimum"), schema.get("maximum")
    if lo is not None and hi is not None:
        val = 100 if lo <= 100 <= hi else (lo if lo > 0 else (lo + hi) / 2)
    elif lo is not None:
        val = lo + 1 if schema.get("exclusiveMinimum") is True else lo
        if isinstance(schema.get("exclusiveMinimum"), (int, float)) and not isinstance(schema.get("exclusiveMinimum"), bool):
            val = schema["exclusiveMinimum"] + 1
    elif hi is not None:
        val = min(1, hi)
    else:
        val = 1
    if lo is not None and val == lo and schema.get("exclusiveMinimum") is True:
        val = lo + (1 if integer else 0.01)
    return int(val) if integer else float(val)


# --------------------------------------------------------------------------- negatives & boundaries

@dataclass
class ValueCase:
    field: str
    value: Any
    valid: bool
    reason: str


def invalid_values(field: str, schema: dict) -> list[ValueCase]:
    """Values that violate the schema's type, enum or format."""
    schema = _first_variant(schema)
    t = schema_type(schema)
    cases: list[ValueCase] = []
    if schema.get("enum"):
        cases.append(ValueCase(field, "INVALID_ENUM_VALUE", False, "value not in enum"))
    if t in ("integer", "number"):
        cases.append(ValueCase(field, "not-a-number", False, f"string instead of {t}"))
    elif t == "boolean":
        cases.append(ValueCase(field, "not-a-boolean", False, "string instead of boolean"))
    elif t == "array":
        cases.append(ValueCase(field, "not-an-array", False, "string instead of array"))
    elif t == "object":
        cases.append(ValueCase(field, "not-an-object", False, "string instead of object"))
    elif t == "string":
        fmt = schema.get("format", "")
        if fmt in INVALID_FORMATS:
            cases.append(ValueCase(field, INVALID_FORMATS[fmt], False, f"invalid {fmt} format"))
    if t == "integer" and not schema.get("enum"):
        cases.append(ValueCase(field, 1.5, False, "decimal instead of integer"))
    return cases


def boundary_values(field: str, schema: dict) -> list[ValueCase]:
    """On-boundary (valid) and just-outside (invalid) values for length/range constraints."""
    schema = _first_variant(schema)
    t = schema_type(schema)
    cases: list[ValueCase] = []
    if t == "string" and not schema.get("enum") and not schema.get("format"):
        lo, hi = schema.get("minLength"), schema.get("maxLength")
        if lo is not None:
            if lo > 0:
                cases.append(ValueCase(field, "a" * (lo - 1), False, f"length {lo - 1} (minLength - 1)"))
            if lo > 0:
                cases.append(ValueCase(field, "a" * lo, True, f"length {lo} (minLength)"))
        if hi is not None:
            cases.append(ValueCase(field, "a" * hi, True, f"length {hi} (maxLength)"))
            cases.append(ValueCase(field, "a" * (hi + 1), False, f"length {hi + 1} (maxLength + 1)"))
    elif t in ("integer", "number"):
        step = 1 if t == "integer" else 0.01
        lo, hi = schema.get("minimum"), schema.get("maximum")
        ex_lo, ex_hi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
        # OAS 3.1 numeric exclusive bounds
        if isinstance(ex_lo, (int, float)) and not isinstance(ex_lo, bool):
            lo, ex_lo = ex_lo, True
        if isinstance(ex_hi, (int, float)) and not isinstance(ex_hi, bool):
            hi, ex_hi = ex_hi, True
        if lo is not None:
            if ex_lo:
                cases.append(ValueCase(field, _num(lo, t), False, f"{lo} (exclusive minimum)"))
                cases.append(ValueCase(field, _num(lo + step, t), True, f"{lo} + {step}"))
            else:
                cases.append(ValueCase(field, _num(lo, t), True, f"{lo} (minimum)"))
                cases.append(ValueCase(field, _num(lo - step, t), False, f"{lo} - {step} (below minimum)"))
        if hi is not None:
            if ex_hi:
                cases.append(ValueCase(field, _num(hi, t), False, f"{hi} (exclusive maximum)"))
                cases.append(ValueCase(field, _num(hi - step, t), True, f"{hi} - {step}"))
            else:
                cases.append(ValueCase(field, _num(hi, t), True, f"{hi} (maximum)"))
                cases.append(ValueCase(field, _num(hi + step, t), False, f"{hi} + {step} (above maximum)"))
    elif t == "array":
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        item = sample_value(schema.get("items") or {}, field)
        if lo:
            cases.append(ValueCase(field, [item] * (lo - 1), False, f"{lo - 1} items (minItems - 1)"))
            cases.append(ValueCase(field, [item] * lo, True, f"{lo} items (minItems)"))
        if hi is not None and hi <= 50:
            cases.append(ValueCase(field, [item] * hi, True, f"{hi} items (maxItems)"))
            cases.append(ValueCase(field, [item] * (hi + 1), False, f"{hi + 1} items (maxItems + 1)"))
    return cases


def _num(v, t):
    return int(v) if t == "integer" else round(float(v), 4)


def walk_fields(schema: dict | None, prefix: str = "", depth: int = 0, max_depth: int = 2):
    """Yield (dotted_path, schema, required) for request-writable fields, up to max_depth levels."""
    if not schema or depth > max_depth or schema.get("x-circular"):
        return
    schema = _first_variant(schema)
    required = set(schema.get("required") or [])
    for prop, sub in (schema.get("properties") or {}).items():
        if sub.get("readOnly"):
            continue
        path = f"{prefix}{prop}"
        yield path, sub, prop in required
        if schema_type(sub) == "object":
            yield from walk_fields(sub, path + ".", depth + 1, max_depth)
