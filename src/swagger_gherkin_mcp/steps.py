"""Step vocabulary.

Every step the generator emits comes from this table, so teams can align the
output with their existing step definitions by overriding any template in a
YAML file (see examples/step-templates.yaml). Point the server at it with the
SWAGGER_GHERKIN_STEPS environment variable, or pass `step_templates_path` to a tool.

Placeholders use Python str.format syntax. In Scenario Outlines the generator
passes Gherkin placeholders such as "<field>" as the values.
"""
from __future__ import annotations

import os

import yaml

DEFAULT_TEMPLATES: dict[str, str] = {
    # Background / setup
    "base_url": "the API base URL is configured",
    "authenticated": 'I am authenticated with a valid "{scheme}" credential',
    "not_authenticated": "I am not authenticated",
    "insufficient_permissions": 'I am authenticated with a "{scheme}" credential that lacks the required permissions',
    # Request building
    "path_param": 'the path parameter "{name}" is {value}',
    "query_param": 'the query parameter "{name}" is {value}',
    "query_param_length": 'the query parameter "{name}" is a string of length {length}',
    "query_param_missing": 'the query parameter "{name}" is not provided',
    "header": 'the request header "{name}" is {value}',
    "header_missing": 'the request header "{name}" is not provided',
    "body": "the request body is:",
    "no_body": "the request has no body",
    "content_type": 'the request content type is "{content_type}"',
    "body_field_set": 'the request body field "{field}" is set to {value}',
    "body_field_length": 'the request body field "{field}" is set to a string of length {length}',
    "body_field_removed": 'the request body field "{field}" is removed',
    # Action
    "send": 'I send a {method} request to "{path}"',
    # Assertions
    "status": "the response status code should be {status}",
    "schema_named": 'the response body should match the "{schema}" schema',
    "schema_generic": "the response body should match the documented {status} response schema",
    "body_has_field": 'the response body should contain the field "{field}"',
}


def load_templates(path: str | None = None) -> dict[str, str]:
    """Defaults, overridden by the YAML file at `path` or $SWAGGER_GHERKIN_STEPS."""
    templates = dict(DEFAULT_TEMPLATES)
    path = path or os.environ.get("SWAGGER_GHERKIN_STEPS")
    if path:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            overrides = yaml.safe_load(fh) or {}
        unknown = set(overrides) - set(DEFAULT_TEMPLATES)
        if unknown:
            raise ValueError(f"Unknown step template keys: {sorted(unknown)}. "
                             f"Valid keys: {sorted(DEFAULT_TEMPLATES)}")
        templates.update({k: str(v) for k, v in overrides.items()})
    return templates
