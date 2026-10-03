"""Load Swagger 2.0 / OpenAPI 3.x specs and normalise them into a common model.

Only local references ("#/...") are resolved. Circular references are cut after
one level and marked with "x-circular" so generation never loops forever.
"""
from __future__ import annotations

import copy
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import yaml

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head")


@dataclass
class Param:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict
    description: str = ""


@dataclass
class Operation:
    operation_id: str
    method: str
    path: str
    summary: str
    description: str
    tags: list[str]
    params: list[Param]
    body_schema: dict | None
    body_required: bool
    body_content_type: str | None
    responses: dict[str, dict]  # code -> {"description", "schema", "content_type"}
    security: list[str]  # names of security schemes required (empty = public)
    deprecated: bool = False

    @property
    def success_code(self) -> str:
        codes = sorted(c for c in self.responses if c.startswith("2"))
        return codes[0] if codes else "200"


@dataclass
class ApiSpec:
    title: str
    version: str
    spec_version: str  # "2.0" or "3.x"
    operations: list[Operation] = field(default_factory=list)
    security_schemes: dict[str, dict] = field(default_factory=dict)

    def get(self, operation_id: str) -> Operation:
        for op in self.operations:
            if op.operation_id == operation_id:
                return op
        for op in self.operations:  # lenient, case-insensitive
            if op.operation_id.lower() == operation_id.lower():
                return op
        raise KeyError(
            f"Operation '{operation_id}' not found. "
            f"Available: {', '.join(o.operation_id for o in self.operations)}"
        )

    def tags(self) -> list[str]:
        seen: list[str] = []
        for op in self.operations:
            for t in op.tags:
                if t not in seen:
                    seen.append(t)
        return seen


# --------------------------------------------------------------------------- loading

_cache: dict[str, tuple[float, ApiSpec]] = {}


def _is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source))


def _read_source(source: str) -> str:
    if _is_url(source):
        req = urllib.request.Request(source, headers={"Accept": "application/json, application/yaml, */*"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - user-supplied spec URL
            return resp.read().decode("utf-8")
    path = os.path.expanduser(source)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Spec file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_text(text: str) -> dict:
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            raise ValueError("Spec is neither valid JSON nor a YAML mapping")
        return data


def load_spec(source: str) -> ApiSpec:
    """Load (and cache) a spec from a file path or http(s) URL."""
    stamp = 0.0 if _is_url(source) else os.path.getmtime(os.path.expanduser(source))
    cached = _cache.get(source)
    if cached and cached[0] == stamp and not _is_url(source):
        return cached[1]
    spec = normalise(parse_text(_read_source(source)))
    _cache[source] = (stamp, spec)
    return spec


# --------------------------------------------------------------------------- $ref resolution

class _Resolver:
    def __init__(self, root: dict):
        self.root = root

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError(f"External $ref not supported (bundle the spec first): {ref}")
        node: Any = self.root
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def resolve(self, node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [self.resolve(n, stack) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.rsplit("/", 1)[-1]
            if ref in stack:
                return {"type": "object", "x-circular": ref, "x-schema-name": name}
            resolved = self.resolve(copy.deepcopy(self._lookup(ref)), stack + (ref,))
            if isinstance(resolved, dict) and ("/schemas/" in ref or "/definitions/" in ref):
                resolved.setdefault("x-schema-name", name)
            return resolved
        return {k: self.resolve(v, stack) for k, v in node.items()}


def merge_all_of(schema: Any) -> Any:
    """Flatten allOf into a single object schema (recursively)."""
    if not isinstance(schema, dict):
        return schema
    s = dict(schema)
    if "allOf" in s:
        parts = [merge_all_of(p) for p in s.pop("allOf")]
        merged: dict = {"type": "object", "properties": {}, "required": []}
        for p in parts:
            merged["properties"].update(p.get("properties", {}))
            merged["required"] += [r for r in p.get("required", []) if r not in merged["required"]]
            for k, v in p.items():
                if k not in ("properties", "required", "type", "x-schema-name"):
                    merged.setdefault(k, v)
        merged["properties"].update(s.pop("properties", {}))
        merged["required"] += [r for r in s.pop("required", []) if r not in merged["required"]]
        merged.update({k: v for k, v in s.items() if k != "type"})
        s = merged
    if "properties" in s:
        s["properties"] = {k: merge_all_of(v) for k, v in s["properties"].items()}
    if "items" in s:
        s["items"] = merge_all_of(s["items"])
    for key in ("oneOf", "anyOf"):
        if key in s:
            s[key] = [merge_all_of(x) for x in s[key]]
    return s


# --------------------------------------------------------------------------- normalisation

def _slug_operation_id(method: str, path: str) -> str:
    parts = [p for p in re.split(r"[/{}\-_.]+", path) if p]
    return method.lower() + "".join(p[:1].upper() + p[1:] for p in parts)


def _pick_content(content: dict | None) -> tuple[str | None, dict | None]:
    if not content:
        return None, None
    for ct in ("application/json", "application/problem+json"):
        if ct in content:
            return ct, (content[ct] or {}).get("schema")
    for ct, media in content.items():
        if "json" in ct:
            return ct, (media or {}).get("schema")
    ct, media = next(iter(content.items()))
    return ct, (media or {}).get("schema")


_V2_PARAM_KEYS = ("type", "format", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                  "minLength", "maxLength", "pattern", "items", "default", "minItems", "maxItems")


def _swagger2_param_schema(p: dict) -> dict:
    schema = {k: p[k] for k in _V2_PARAM_KEYS if k in p}
    if "x-example" in p:
        schema["example"] = p["x-example"]
    return schema


def normalise(raw: dict) -> ApiSpec:
    is_v2 = str(raw.get("swagger", "")).startswith("2")
    if not is_v2 and not str(raw.get("openapi", "")).startswith("3"):
        raise ValueError("Unsupported spec: expected 'swagger: \"2.0\"' or 'openapi: 3.x'")

    root = _Resolver(raw).resolve(raw)
    info = root.get("info", {})
    spec = ApiSpec(
        title=info.get("title", "API"),
        version=str(info.get("version", "")),
        spec_version="2.0" if is_v2 else str(raw.get("openapi")),
        security_schemes=(root.get("securityDefinitions") if is_v2
                          else root.get("components", {}).get("securitySchemes")) or {},
    )
    global_security = root.get("security", [])
    used_ids: set[str] = set()

    for path, item in (root.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        path_params = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue

            merged: dict[tuple, dict] = {}  # path-level then op-level (op wins)
            for p in path_params + op.get("parameters", []):
                merged[(p.get("name"), p.get("in"))] = p

            params: list[Param] = []
            body_schema, body_required, body_ct = None, False, None
            form_props: dict = {}
            form_required: list[str] = []
            for p in merged.values():
                loc = p.get("in")
                if loc == "body":
                    body_schema = p.get("schema")
                    body_required = bool(p.get("required"))
                    body_ct = (op.get("consumes") or root.get("consumes") or ["application/json"])[0]
                elif loc == "formData":
                    form_props[p["name"]] = _swagger2_param_schema(p)
                    if p.get("required"):
                        form_required.append(p["name"])
                else:
                    schema = _swagger2_param_schema(p) if is_v2 else dict(p.get("schema") or {})
                    schema = schema or {"type": "string"}
                    if "example" in p:
                        schema.setdefault("example", p["example"])
                    params.append(Param(p["name"], loc, bool(p.get("required")) or loc == "path",
                                        schema, p.get("description", "")))
            if form_props:
                body_schema = {"type": "object", "properties": form_props, "required": form_required}
                body_required = bool(form_required)
                ct_list = op.get("consumes") or root.get("consumes") or []
                body_ct = next((c for c in ct_list if "form" in c), "application/x-www-form-urlencoded")

            if not is_v2 and "requestBody" in op:
                rb = op["requestBody"] or {}
                body_ct, body_schema = _pick_content(rb.get("content"))
                body_required = bool(rb.get("required"))

            responses: dict[str, dict] = {}
            for code, resp in (op.get("responses") or {}).items():
                resp = resp or {}
                if is_v2:
                    ct = (op.get("produces") or root.get("produces") or ["application/json"])[0]
                    schema = resp.get("schema")
                else:
                    ct, schema = _pick_content(resp.get("content"))
                responses[str(code)] = {
                    "description": resp.get("description", ""),
                    "schema": merge_all_of(schema) if schema else None,
                    "content_type": ct if schema else None,
                }

            sec = op["security"] if "security" in op else global_security
            sec_names = sorted({name for req in (sec or []) for name in (req or {}).keys()})

            op_id = op.get("operationId") or _slug_operation_id(method, path)
            base, n = op_id, 2
            while op_id in used_ids:
                op_id, n = f"{base}_{n}", n + 1
            used_ids.add(op_id)

            spec.operations.append(Operation(
                operation_id=op_id,
                method=method.upper(),
                path=path,
                summary=(op.get("summary") or "").strip(),
                description=(op.get("description") or "").strip(),
                tags=op.get("tags") or ["default"],
                params=params,
                body_schema=merge_all_of(body_schema) if body_schema else None,
                body_required=body_required,
                body_content_type=body_ct,
                responses=responses,
                security=sec_names,
                deprecated=bool(op.get("deprecated")),
            ))
    return spec
