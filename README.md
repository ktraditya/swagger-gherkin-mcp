# swagger-gherkin-mcp

An MCP server that turns a Swagger 2.0 / OpenAPI 3.x spec into Gherkin (Cucumber) contract tests.
It works with any MCP client: IBM Bob, Claude, Cursor, VS Code Copilot, and others.

Scenario generation is **deterministic**: the same spec always produces the same feature files,
so they diff cleanly in Git. The agent's LLM is only needed for what a spec can't express
(business rules, multi-step flows). The server gives it `get_operation_details` and
`get_step_vocabulary` for that.

## Install

```bash
pip install -e .            # Python 3.10+
pytest                      # optional: pip install -e ".[test]" first
```

## Connect to IBM Bob

Copy `bob/mcp.json` to `.bob/mcp.json` in your project (or merge it into your global MCP settings).
Use the full path to your virtualenv's `python` if `python` isn't on PATH. Then ask Bob things like:

- "List the operations in specs/payments.yaml"
- "Generate feature files for specs/payments.yaml into src/test/resources/features"
- "Show the coverage report, then write business scenarios for the 409 cases using the same step vocabulary"

For a shared, team-wide server: `swagger-gherkin-mcp --http` serves streamable HTTP on `127.0.0.1:8000/mcp`.
Put it behind your usual auth/reverse proxy before exposing it.

## Tools

| Tool | What it does |
|---|---|
| `list_operations` | operationIds, methods, paths, auth, declared responses |
| `get_operation_details` | resolved params, body schema, responses, and a sample valid request |
| `generate_scenarios` | Gherkin for one operation |
| `generate_feature_files` | one `.feature` per tag; returns text or writes to `output_dir` |
| `coverage_report` | which declared response codes are covered and which still need hand-written scenarios |
| `get_step_vocabulary` | the step phrases in use (after overrides) |

`spec` accepts a file path or an http(s) URL. `categories` limits output to any of
`positive, validation, boundary, security, not_found`.

## What gets generated per operation

- **positive**: happy path (`@smoke`), plus a "required fields only" variant. Asserts status, response schema and required response fields.
- **validation**: no body, each missing required field (nested fields included), wrong types, invalid enum, invalid formats (date, uuid, email, …), invalid path/query params, missing required query/header params, and 415 if declared.
- **boundary**: minLength/maxLength, minimum/maximum (inclusive and exclusive) and minItems/maxItems, both on the edge (expect success) and just outside it (expect 400/422).
- **security**: 401 when unauthenticated; 403 if declared.
- **not_found**: 404 with a non-existent id, if declared.

Expected codes come from the spec. Where the spec doesn't declare one (e.g. no 400), the server
assumes the usual code and writes a `# NOTE:` comment above the scenario. Declared responses it can't
generate (e.g. 409 Conflict, which needs business state) are listed as `# TODO:` comments and in
`coverage_report`.

Each operation is a Gherkin `Rule` with a `Background` holding a fully valid request, so each scenario
only states what it changes. This needs Cucumber 6+; pass `use_rules: false` to inline the full request
in every scenario instead.

## Matching your existing step definitions

All step wording comes from a template table. Override any of it in YAML
(see `examples/step-templates.yaml`) and point the server at it via `SWAGGER_GHERKIN_STEPS`
or the `step_templates_path` tool argument. Unknown keys are rejected, so typos fail fast.

### Contract for step-definition implementers

The steps assume a **request builder** that the Given steps mutate. Later steps override earlier ones.

| Step | Meaning |
|---|---|
| `the path/query parameter "x" is <json>` | set the param; the value is a JSON literal (`"abc"`, `10`, `true`) |
| `the request header "x" is <json>` / `is not provided` | set or remove a header |
| `the request body is:` + docstring | replace the whole body (JSON; for form endpoints, serialise as form fields) |
| `the request body field "a.b" is set to <json>` | set a field by dotted path |
| `the request body field "a.b" is set to a string of length N` | set a field to an N-character string |
| `the request body field "a.b" is removed` | delete a field by dotted path |
| `the request has no body` | send without a body |
| `the response body should match the "Pet" schema` | validate against the named schema in the spec |
| `the response body should match the documented 200 response schema` | validate against the operation's response schema |

In Java, a thin RestAssured + Jackson `ObjectNode` glue class implements all of these.
Atlassian's `swagger-request-validator` handles the two schema steps directly from the spec.

## Limitations

- Only local `$ref`s are resolved. Bundle multi-file specs first (e.g. `redocly bundle`).
- `pattern` isn't used to generate negatives, because reliably inventing a non-matching string isn't possible in general.
- `oneOf`/`anyOf` use the first variant; `allOf` is merged.
- Circular schemas are cut after one level.
- Generated scenarios test the **contract**, not business rules. Review them before committing.
