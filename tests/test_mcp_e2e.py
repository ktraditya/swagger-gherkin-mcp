"""End-to-end: start the server over stdio exactly like Bob/Claude would, and call its tools."""
import asyncio
import os
import sys

import pytest

EX = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "examples"))


def _text(result):
    return "".join(getattr(c, "text", "") for c in result.content)


def test_stdio_roundtrip():
    try:
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters
    except ImportError:
        pytest.skip("needs MCP SDK v2 client")

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "swagger_gherkin_mcp.server"],
                                       env={**os.environ, "PYTHONPATH": os.path.join(EX, "..", "src")})
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert tools == {"list_operations", "get_operation_details", "generate_scenarios",
                             "generate_feature_files", "coverage_report", "get_step_vocabulary"}
            spec = os.path.join(EX, "payments-api.yaml")
            out = _text(await client.call_tool("generate_scenarios",
                                               {"spec": spec, "operation_id": "getAccount"}))
            assert "Feature: Accounts" in out and "should be 404" in out
            return out

    out = asyncio.run(run())
    print(out)
