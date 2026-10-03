"""Build Gherkin scenarios from a normalised operation and render feature files.

Layout (default, Gherkin 6+ / Cucumber-JVM 6+):

  Feature: <tag>
    Background:            -> base URL
    Rule: <METHOD path>    -> one Rule per operation
      Background:          -> a fully valid base request (path/query/header params + body)
      Scenario ...         -> auth + only the deltas that make the case

With use_rules=False the base request is inlined into every scenario instead,
for older Cucumber versions that don't support Rule.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .samples import boundary_values, invalid_values, sample_value, schema_type, walk_fields
from .spec_loader import ApiSpec, Operation

CATEGORIES = ("positive", "validation", "boundary", "security", "not_found")
_SKIP_HEADERS = {"authorization", "content-type", "accept"}


# --------------------------------------------------------------------------- model

@dataclass
class Step:
    kind: str  # given | when | then
    text: str
    docstring: str | None = None
    doc_type: str = ""


@dataclass
class Scenario:
    name: str
    tags: list[str]
    auth: list[Step] = field(default_factory=list)   # rendered before the base request
    steps: list[Step] = field(default_factory=list)  # deltas, action and assertions
    notes: list[str] = field(default_factory=list)
    examples_header: list[str] | None = None
    examples_rows: list[list[str]] = field(default_factory=list)
    covers: set[str] = field(default_factory=set)

    def given(self, text, docstring=None, doc_type=""):
        self.steps.append(Step("given", text, docstring, doc_type))

    def when(self, text):
        self.steps.append(Step("when", text))

    def then(self, text):
        self.steps.append(Step("then", text))


@dataclass
class OperationResult:
    operation: Operation
    base: list[Step]
    scenarios: list[Scenario]
    uncovered: dict[str, str]

    @property
    def covered(self) -> list[str]:
        return sorted({c for s in self.scenarios for c in s.covers})


# --------------------------------------------------------------------------- helpers

def _lit(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _cell(value: Any) -> str:
    s = value if isinstance(value, str) else str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _tag(text: str) -> str:
    return "@" + re.sub(r"[^A-Za-z0-9_\-.]+", "-", text).strip("-")


class _Gen:
    def __init__(self, op: Operation, t: dict[str, str]):
        self.op, self.t = op, t
        self.base_tags = [_tag(op.operation_id)] + (["@deprecated"] if op.deprecated else [])
        self.sample_body = sample_value(op.body_schema, "body") if op.body_schema else None

    # ---- expected codes ------------------------------------------------------
    def _code(self, candidates: tuple[str, ...], fallback: str, what: str):
        for c in candidates:
            if c in self.op.responses:
                return c, None
        return fallback, f"NOTE: spec declares no {'/'.join(candidates)} response; {fallback} assumed for {what}"

    def validation_code(self):
        return self._code(("400", "422"), "400", "validation errors")

    # ---- base request --------------------------------------------------------
    def base_steps(self) -> list[Step]:
        op, t, steps = self.op, self.t, []
        for p in op.params:
            v = _lit(sample_value(p.schema, p.name))
            if p.location == "path":
                steps.append(Step("given", t["path_param"].format(name=p.name, value=v)))
            elif p.location == "query" and p.required:
                steps.append(Step("given", t["query_param"].format(name=p.name, value=v)))
            elif p.location == "header" and p.required and p.name.lower() not in _SKIP_HEADERS:
                steps.append(Step("given", t["header"].format(name=p.name, value=v)))
        if op.body_schema:
            if op.body_content_type and "json" not in op.body_content_type:
                steps.append(Step("given", t["content_type"].format(content_type=op.body_content_type)))
            steps.append(Step("given", t["body"], json.dumps(self.sample_body, indent=2, ensure_ascii=False), "json"))
        return steps

    # ---- scenario factory ----------------------------------------------------
    def new(self, title: str, category_tag: str, notes=(), auth: str = "valid") -> Scenario:
        sc = Scenario(title, self.base_tags + [category_tag], notes=[n for n in notes if n])
        if self.op.security:
            scheme = self.op.security[0]
            text = {"valid": self.t["authenticated"].format(scheme=scheme),
                    "none": self.t["not_authenticated"],
                    "forbidden": self.t["insufficient_permissions"].format(scheme=scheme)}[auth]
            sc.auth.append(Step("given", text))
        return sc

    def send(self, sc: Scenario) -> None:
        sc.when(self.t["send"].format(method=self.op.method, path=self.op.path))

    def expect(self, sc: Scenario, status: str, covers=()) -> None:
        sc.then(self.t["status"].format(status=status))
        sc.covers.update(covers or {status})

    # ---- categories ----------------------------------------------------------
    def positive(self) -> list[Scenario]:
        op, out = self.op, []
        sc = self.new(op.summary.rstrip(".") or "successful request", "@positive")
        sc.tags.append("@smoke")
        self.send(sc)
        self._assert_success(sc)
        out.append(sc)

        body = self.sample_body
        if isinstance(body, dict) and op.body_schema.get("required"):
            minimal = {k: v for k, v in body.items() if k in op.body_schema["required"]}
            if minimal != body:
                sc = self.new("accept a request with only the required fields", "@positive")
                sc.given(self.t["body"], json.dumps(minimal, indent=2, ensure_ascii=False), "json")
                self.send(sc)
                self._assert_success(sc)
                out.append(sc)
        return out

    def _assert_success(self, sc: Scenario) -> None:
        code = self.op.success_code
        self.expect(sc, code)
        schema = (self.op.responses.get(code) or {}).get("schema")
        if schema:
            name = schema.get("x-schema-name")
            sc.then(self.t["schema_named"].format(schema=name) if name
                    else self.t["schema_generic"].format(status=code))
            if schema_type(schema) == "object":
                for f in (schema.get("required") or [])[:5]:
                    sc.then(self.t["body_has_field"].format(field=f))

    def validation(self) -> list[Scenario]:
        op, t = self.op, self.t
        code, note = self.validation_code()
        out: list[Scenario] = []

        if op.body_schema and op.body_required:
            sc = self.new("reject a request without a body", "@negative", [note])
            sc.given(t["no_body"])
            self.send(sc)
            self.expect(sc, code)
            out.append(sc)

        if op.body_schema:
            fields = list(walk_fields(op.body_schema))
            required = [f for f, _, req in fields if req]
            if required:
                sc = self.new("reject a missing required field <field>", "@negative", [note])
                sc.given(t["body_field_removed"].format(field="<field>"))
                self.send(sc)
                self.expect(sc, code)
                sc.examples_header, sc.examples_rows = ["field"], [[f] for f in required]
                out.append(sc)

            rows = [[c.field, _lit(c.value), c.reason] for f, s, _ in fields for c in invalid_values(f, s)]
            if rows:
                sc = self.new("reject an invalid value for <field> (<reason>)", "@negative", [note])
                sc.given(t["body_field_set"].format(field="<field>", value="<value>"))
                self.send(sc)
                self.expect(sc, code)
                sc.examples_header, sc.examples_rows = ["field", "value", "reason"], rows
                out.append(sc)

        for p in op.params:
            if p.location not in ("path", "query"):
                continue
            cases = invalid_values(p.name, p.schema)
            if p.location == "path":  # path params can't take part in boundary outlines, so add invalid edges here
                cases += [c for c in boundary_values(p.name, p.schema) if not c.valid]
            if not cases:
                continue
            sc = self.new(f"reject an invalid {p.location} parameter {p.name} (<reason>)", "@negative", [note])
            key = "path_param" if p.location == "path" else "query_param"
            sc.given(t[key].format(name=p.name, value="<value>"))
            self.send(sc)
            self.expect(sc, code)
            sc.examples_header = ["value", "reason"]
            sc.examples_rows = [[_lit(c.value), c.reason] for c in cases]
            out.append(sc)

        for p in op.params:
            if p.required and p.location in ("query", "header") and p.name.lower() not in _SKIP_HEADERS:
                sc = self.new(f"reject a missing required {p.location} {p.name}", "@negative", [note])
                sc.given(t["query_param_missing" if p.location == "query" else "header_missing"].format(name=p.name))
                self.send(sc)
                self.expect(sc, code)
                out.append(sc)

        if op.body_schema and "415" in op.responses:
            sc = self.new("reject an unsupported content type", "@negative")
            sc.given(t["content_type"].format(content_type="text/plain"))
            self.send(sc)
            self.expect(sc, "415")
            out.append(sc)
        return out

    def boundary(self) -> list[Scenario]:
        op, t = self.op, self.t
        ok = op.success_code
        bad, note = self.validation_code()
        out: list[Scenario] = []

        def outline(title, step_text, header, rows):
            sc = self.new(title, "@boundary", [note])
            sc.given(step_text)
            self.send(sc)
            self.expect(sc, "<status>", covers={ok, bad})
            sc.examples_header, sc.examples_rows = header, rows
            out.append(sc)

        if op.body_schema:
            length_rows, value_rows = [], []
            for f, s, _ in walk_fields(op.body_schema):
                for c in boundary_values(f, s):
                    status = ok if c.valid else bad
                    if isinstance(c.value, str):
                        length_rows.append([f, str(len(c.value)), status, c.reason])
                    else:
                        value_rows.append([f, _lit(c.value), status, c.reason])
            if length_rows:
                outline("string length boundary for <field> (<case>)",
                        t["body_field_length"].format(field="<field>", length="<length>"),
                        ["field", "length", "status", "case"], length_rows)
            if value_rows:
                outline("value boundary for <field> (<case>)",
                        t["body_field_set"].format(field="<field>", value="<value>"),
                        ["field", "value", "status", "case"], value_rows)

        for p in (p for p in op.params if p.location == "query"):
            cases = boundary_values(p.name, p.schema)
            if not cases:
                continue
            if isinstance(cases[0].value, str):
                outline(f"query parameter {p.name} length boundary (<case>)",
                        t["query_param_length"].format(name=p.name, length="<length>"),
                        ["length", "status", "case"],
                        [[str(len(c.value)), ok if c.valid else bad, c.reason] for c in cases])
            else:
                outline(f"query parameter {p.name} value boundary (<case>)",
                        t["query_param"].format(name=p.name, value="<value>"),
                        ["value", "status", "case"],
                        [[_lit(c.value), ok if c.valid else bad, c.reason] for c in cases])
        return out

    def security(self) -> list[Scenario]:
        if not self.op.security:
            return []
        code, note = self._code(("401",), "401", "unauthenticated requests")
        sc = self.new("reject an unauthenticated request", "@security", [note], auth="none")
        self.send(sc)
        self.expect(sc, code)
        out = [sc]
        if "403" in self.op.responses:
            sc = self.new("reject a caller without the required permissions", "@security", auth="forbidden")
            self.send(sc)
            self.expect(sc, "403")
            out.append(sc)
        return out

    def not_found(self) -> list[Scenario]:
        op = self.op
        path_params = [p for p in op.params if p.location == "path"]
        if "404" not in op.responses or not path_params:
            return []
        sc = self.new("return 404 for a non-existent resource", "@negative")
        for p in path_params:
            st, fmt = schema_type(p.schema), p.schema.get("format")
            if fmt == "uuid":
                v = "00000000-0000-0000-0000-000000000000"
            elif st in ("integer", "number"):
                v = int(p.schema["maximum"]) if "maximum" in p.schema else 999999999
            else:
                v = "does-not-exist"
            sc.given(self.t["path_param"].format(name=p.name, value=_lit(v)))
        self.send(sc)
        self.expect(sc, "404")
        return [sc]


# --------------------------------------------------------------------------- public API

def generate_operation(spec: ApiSpec, op: Operation, templates: dict[str, str],
                       categories: list[str] | None = None) -> OperationResult:
    cats = list(categories or CATEGORIES)
    bad = set(cats) - set(CATEGORIES)
    if bad:
        raise ValueError(f"Unknown categories {sorted(bad)}; choose from {list(CATEGORIES)}")
    g = _Gen(op, templates)
    scenarios: list[Scenario] = []
    for c in CATEGORIES:
        if c in cats:
            scenarios.extend(getattr(g, c)())
    covered = {code for s in scenarios for code in s.covers}
    uncovered = {c: r.get("description", "") for c, r in op.responses.items()
                 if c not in covered and c != "default"}
    return OperationResult(op, g.base_steps(), scenarios, uncovered)


def render_feature(spec: ApiSpec, feature_name: str, results: list[OperationResult],
                   templates: dict[str, str], use_rules: bool = True) -> str:
    lines = [f"# Generated by swagger-gherkin-mcp from {spec.title} {spec.version}".rstrip(),
             "# Review before committing: generated scenarios cover the API contract, not business rules.",
             f"{_tag('api')} {_tag(feature_name)}",
             f"Feature: {feature_name}",
             f"  Contract tests for {spec.title}" + (f" v{spec.version}" if spec.version else "") + ".",
             "",
             "  Background:",
             f"    Given {templates['base_url']}",
             ""]
    for res in results:
        op = res.operation
        ind = "    " if use_rules else "  "
        if use_rules:
            lines.append(f"  Rule: {op.method} {op.path}" + (f" - {op.summary}" if op.summary else ""))
            lines.append("")
        else:
            lines.append(f"  # ---- {op.method} {op.path} ({op.operation_id})")
        for code, desc in sorted(res.uncovered.items()):
            lines.append(f"{ind}# TODO: response {code} not generated ({desc or 'no description'}) "
                         "- needs business-state setup")
        if use_rules and res.base:
            lines.append(f"{ind}Background:")
            lines.extend(_render_steps(res.base, ind + "  "))
            lines.append("")
        elif res.uncovered:
            lines.append("")
        for sc in res.scenarios:
            prefix = f"{op.operation_id}: "  # unique names in JUnit/HTML reports
            steps = sc.auth + ([] if use_rules else res.base) + sc.steps
            lines.extend(_render_scenario(sc, prefix, steps, ind))
    return "\n".join(lines).rstrip() + "\n"


def _render_steps(steps: list[Step], ind: str) -> list[str]:
    out, last = [], None
    for st in steps:
        kw = {"given": "Given", "when": "When", "then": "Then"}[st.kind]
        out.append(f"{ind}{'And' if st.kind == last else kw} {st.text}")
        last = st.kind
        if st.docstring is not None:
            out.append(f'{ind}  """{st.doc_type}')
            out.extend(f"{ind}  {line}" for line in st.docstring.splitlines())
            out.append(f'{ind}  """')
    return out


def _render_scenario(sc: Scenario, prefix: str, steps: list[Step], ind: str) -> list[str]:
    out = [f"{ind}# {n}" for n in sc.notes]
    out.append(ind + " ".join(sc.tags))
    out.append(f"{ind}{'Scenario Outline' if sc.examples_header else 'Scenario'}: {prefix}{sc.name}")
    out.extend(_render_steps(steps, ind + "  "))
    if sc.examples_header:
        out += ["", f"{ind}  Examples:"]
        table = [sc.examples_header] + [[_cell(c) for c in row] for row in sc.examples_rows]
        widths = [max(len(r[i]) for r in table) for i in range(len(sc.examples_header))]
        out += [f"{ind}    | " + " | ".join(c.ljust(w) for c, w in zip(row, widths)) + " |" for row in table]
    out.append("")
    return out


def feature_file_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + ".feature"
