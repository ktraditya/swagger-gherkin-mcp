"""Generation tests: every generated feature must parse with the official Gherkin parser."""
import os
import sys

import pytest
from gherkin.parser import Parser
from gherkin.token_scanner import TokenScanner

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from swagger_gherkin_mcp.generator import generate_operation, render_feature  # noqa: E402
from swagger_gherkin_mcp.server import (coverage_report, generate_feature_files,  # noqa: E402
                                        get_operation_details, list_operations)
from swagger_gherkin_mcp.spec_loader import load_spec  # noqa: E402
from swagger_gherkin_mcp.steps import load_templates  # noqa: E402

EX = os.path.join(os.path.dirname(__file__), "..", "examples")
OAS3 = os.path.join(EX, "payments-api.yaml")
SW2 = os.path.join(EX, "petstore-swagger2.json")


def parse(text):
    return Parser().parse(TokenScanner(text))


def scenarios_of(doc):
    out = []
    for child in doc["feature"]["children"]:
        if "rule" in child:
            out += [c["scenario"] for c in child["rule"]["children"] if "scenario" in c]
        elif "scenario" in child:
            out.append(child["scenario"])
    return out


@pytest.mark.parametrize("spec", [OAS3, SW2])
@pytest.mark.parametrize("use_rules", [True, False])
def test_all_features_parse(spec, use_rules):
    api = load_spec(spec)
    t = load_templates(None)
    for tag in api.tags():
        results = [generate_operation(api, op, t) for op in api.operations if op.tags[0] == tag]
        doc = parse(render_feature(api, tag, results, t, use_rules))
        names = [s["name"] for s in scenarios_of(doc)]
        assert names, tag
        assert len(names) == len(set(names)), "scenario names must be unique"


def test_oas3_normalisation():
    api = load_spec(OAS3)
    op = api.get("createPayment")
    assert op.success_code == "201"
    assert op.security == ["bearerAuth"]
    assert {p.name for p in op.params} == {"accountId", "X-Request-ID"}  # path-level param merged
    assert op.responses["201"]["schema"]["x-schema-name"] == "Payment"
    assert "id" in op.responses["201"]["schema"]["properties"]  # allOf merged
    assert api.get("health").security == []  # security: [] overrides global
    assert api.get("getAccount").responses["200"]["schema"]["properties"]["owner"].get("x-circular")


def test_swagger2_normalisation():
    api = load_spec(SW2)
    add = api.get("addPet")
    assert add.body_schema["required"] == ["name", "photoUrls"]
    form = api.get("updatePetWithForm")
    assert form.body_content_type == "application/x-www-form-urlencoded"
    assert form.body_schema["properties"]["name"]["maxLength"] == 50
    assert api.get("getPetById").params[0].schema["type"] == "integer"


def test_expected_cases_present():
    text = generate_feature_files(OAS3, tag="Payments")
    assert "| amount.value | 0.0       | 400" in text  # below minimum
    assert "| creditorName   | 71     | 400" in text  # maxLength + 1
    assert "@security" in text and "should be 403" in text
    assert "TODO: response 409 not generated" in text
    assert "should be 415" in text


def test_assumed_codes_are_flagged():
    text = generate_feature_files(SW2, tag="pet")
    assert "NOTE: spec declares no 400/422 response; 400 assumed" in text
    assert "NOTE: spec declares no 401 response" in text


def test_step_overrides(tmp_path):
    text = generate_feature_files(OAS3, tag="Payments",
                                  step_templates_path=os.path.join(EX, "step-templates.yaml"))
    parse(text)
    assert 'Given the "payments" service is available' in text
    assert 'When I call POST "/accounts/{accountId}/payments"' in text
    bad = tmp_path / "bad.yaml"
    bad.write_text("nonsense_key: x\n")
    with pytest.raises(ValueError):
        load_templates(str(bad))


def test_write_files_and_reports(tmp_path):
    msg = generate_feature_files(OAS3, output_dir=str(tmp_path))
    files = sorted(os.listdir(tmp_path))
    assert files == ["accounts.feature", "monitoring.feature", "payments.feature"], msg
    for f in files:
        parse((tmp_path / f).read_text())
    assert "createPayment" in list_operations(OAS3)
    assert '"sample"' in get_operation_details(OAS3, "createPayment")
    rep = coverage_report(OAS3)
    assert "createPayment" in rep and "409" in rep
