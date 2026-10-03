"""MCP server: Swagger / OpenAPI -> Gherkin.

Run with:  swagger-gherkin-mcp            (stdio, for Bob / Claude / Cursor / VS Code)
           swagger-gherkin-mcp --http     (streamable HTTP on 127.0.0.1:8000/mcp)
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Annotated

from pydantic import Field

try:  # MCP Python SDK v2
    from mcp.server import MCPServer as _Server
except ImportError:  # MCP Python SDK v1
    from mcp.server.fastmcp import FastMCP as _Server

from .generator import CATEGORIES, feature_file_name, generate_operation, render_feature
from .samples import sample_value
from .spec_loader import load_spec
from .steps import load_templates

mcp = _Server(
    "swagger-gherkin",
    instructions=(
        "Generates Gherkin (Cucumber) contract-test scenarios from Swagger 2.0 / OpenAPI 3.x specs. "
        "Typical flow: list_operations -> generate_scenarios for one operation, or generate_feature_files "
        "for a whole spec. Scenarios are deterministic and cover happy paths, validation, boundaries, "
        "auth and 404s. Business rules and multi-step flows are NOT in a spec: after generating, read "
        "get_operation_details and add business scenarios yourself using the same step vocabulary "
        "(get_step_vocabulary)."
    ),
)

SpecSource = Annotated[str, Field(description="Path to a local .yaml/.json spec, or an http(s) URL")]
StepsPath = Annotated[str | None, Field(
    description="Optional YAML file overriding step wording to match your step definitions "
                "(defaults to $SWAGGER_GHERKIN_STEPS)")]
Categories = Annotated[list[str] | None, Field(
    description=f"Subset of {list(CATEGORIES)}; default is all")]
UseRules = Annotated[bool, Field(
    description="Group each operation in a Gherkin 'Rule' with a Background holding the valid base request "
                "(needs Cucumber 6+). Set false to inline the full request in every scenario.")]


@mcp.tool()
def list_operations(spec: SpecSource, tag: Annotated[str | None, Field(description="Filter by tag")] = None) -> str:
    """List every operation in the spec with its operationId, method, path, tags and declared responses."""
    api = load_spec(spec)
    rows = [f"{api.title} {api.version} (spec {api.spec_version}) - {len(api.operations)} operations",
            f"Tags: {', '.join(api.tags())}", ""]
    for op in api.operations:
        if tag and tag not in op.tags:
            continue
        flags = (" [auth]" if op.security else "") + (" [deprecated]" if op.deprecated else "")
        rows.append(f"- {op.operation_id}: {op.method} {op.path}{flags}  tags={op.tags}  "
                    f"responses={sorted(op.responses)}  {op.summary}")
    return "\n".join(rows)


@mcp.tool()
def get_operation_details(spec: SpecSource, operation_id: str) -> str:
    """Resolved details for one operation: parameters, request body schema, responses, security and a
    sample valid request. Use this to write business-level scenarios the spec structure can't express."""
    api = load_spec(spec)
    op = api.get(operation_id)
    details = {
        "operationId": op.operation_id,
        "method": op.method,
        "path": op.path,
        "summary": op.summary,
        "description": op.description,
        "tags": op.tags,
        "security": op.security,
        "parameters": [{"name": p.name, "in": p.location, "required": p.required, "schema": p.schema,
                        "description": p.description} for p in op.params],
        "requestBody": {"contentType": op.body_content_type, "required": op.body_required,
                        "schema": op.body_schema,
                        "sample": sample_value(op.body_schema, "body")} if op.body_schema else None,
        "responses": op.responses,
    }
    return json.dumps(details, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
def generate_scenarios(spec: SpecSource, operation_id: str, categories: Categories = None,
                       step_templates_path: StepsPath = None, use_rules: UseRules = True) -> str:
    """Generate a Gherkin feature for a single operation."""
    api = load_spec(spec)
    templates = load_templates(step_templates_path)
    res = generate_operation(api, api.get(operation_id), templates, categories)
    return render_feature(api, res.operation.tags[0], [res], templates, use_rules)


@mcp.tool()
def generate_feature_files(
    spec: SpecSource,
    output_dir: Annotated[str | None, Field(
        description="If given, write one .feature file per tag into this directory; otherwise return the text")] = None,
    tag: Annotated[str | None, Field(description="Only generate the feature for this tag")] = None,
    categories: Categories = None,
    step_templates_path: StepsPath = None,
    use_rules: UseRules = True,
) -> str:
    """Generate feature files for the whole spec (one Feature per tag). Each operation is placed under its
    first tag so nothing is duplicated."""
    api = load_spec(spec)
    templates = load_templates(step_templates_path)
    groups: dict[str, list] = {}
    for op in api.operations:
        key = tag if (tag and tag in op.tags) else op.tags[0]
        if tag and tag not in op.tags:
            continue
        groups.setdefault(key, []).append(generate_operation(api, op, templates, categories))
    if not groups:
        return f"No operations found{f' for tag {tag!r}' if tag else ''}."

    texts = {name: render_feature(api, name, results, templates, use_rules) for name, results in groups.items()}
    summary = [f"{name}: {len(results)} operations, {sum(len(r.scenarios) for r in results)} scenarios"
               for name, results in groups.items()]
    if not output_dir:
        return "\n\n".join(texts.values())

    out = os.path.abspath(os.path.expanduser(output_dir))
    os.makedirs(out, exist_ok=True)
    written = []
    for name, text in texts.items():
        path = os.path.join(out, feature_file_name(name))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(path)
    return "Wrote:\n" + "\n".join(f"- {p}" for p in written) + "\n\n" + "\n".join(summary)


@mcp.tool()
def coverage_report(spec: SpecSource) -> str:
    """Show, per operation, which declared response codes the generated scenarios exercise and which
    still need hand-written scenarios (usually business-state cases like 409 Conflict)."""
    api = load_spec(spec)
    templates = load_templates(None)
    lines = ["operationId | scenarios | covered | NOT covered", "--- | --- | --- | ---"]
    total, covered_n = 0, 0
    for op in api.operations:
        res = generate_operation(api, op, templates)
        declared = [c for c in op.responses if c != "default"]
        cov = [c for c in res.covered if c in declared]
        total += len(declared)
        covered_n += len(cov)
        lines.append(f"{op.operation_id} | {len(res.scenarios)} | {', '.join(cov) or '-'} | "
                     f"{', '.join(sorted(res.uncovered)) or '-'}")
    pct = (100 * covered_n / total) if total else 100
    lines.append(f"\nDeclared response codes covered: {covered_n}/{total} ({pct:.0f}%)")
    return "\n".join(lines)


@mcp.tool()
def get_step_vocabulary(step_templates_path: StepsPath = None) -> str:
    """The step phrases the generator uses (after overrides). Use the same phrases when writing extra
    scenarios so they bind to existing step definitions."""
    return json.dumps(load_templates(step_templates_path), indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swagger/OpenAPI -> Gherkin MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    args = parser.parse_args()
    mcp.run(transport="streamable-http" if args.http else "stdio")


if __name__ == "__main__":
    main()
