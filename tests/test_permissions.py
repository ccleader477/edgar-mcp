from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from edgar_mcp.permissions import Guard, PermissionDenied, Policy
from tests.mcp_session import Opener


def audit_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.log.jsonl"  # same path the opener's Guard writes to


async def test_viewer_denied_filing_section(opener: Opener, audit_path: Path) -> None:
    async with opener("viewer") as viewer:
        result = await viewer.call_tool("get_filing_section", {"ticker": "AAPL", "item": "1A"})
        assert result.isError
        assert "role 'viewer' may not call get_filing_section" in text(result)
        (line,) = audit_lines(audit_path)
        assert line["status"] == "denied" and line["role"] == "viewer"
        assert line["tool"] == "get_filing_section"


async def test_viewer_allowed_facts(opener: Opener, audit_path: Path) -> None:
    async with opener("viewer") as viewer:
        result = await viewer.call_tool(
            "get_financial_facts",
            {"ticker": "AAPL", "metrics": ["revenue"], "fiscal_years": [2024]},
        )
        assert not result.isError
        (line,) = audit_lines(audit_path)
        assert line["status"] == "ok"
        assert len(line["result_sha256"]) == 64
        assert line["args"] == {
            "ticker": "AAPL",
            "metrics": ["revenue"],
            "fiscal_years": [2024],
            "period": "annual",
        }


async def test_non_allowlisted_ticker_denied(opener: Opener, audit_path: Path) -> None:
    async with opener("analyst") as analyst:
        result = await analyst.call_tool(
            "get_financial_facts", {"ticker": "GME", "metrics": ["revenue"]}
        )
        assert result.isError
        assert "ticker 'GME' is not on the allowlist" in text(result)
        assert audit_lines(audit_path)[-1]["status"] == "denied"


async def test_non_allowlisted_form_denied(opener: Opener) -> None:
    async with opener("analyst") as analyst:
        result = await analyst.call_tool(
            "search_filings", {"ticker": "AAPL", "form_types": ["S-1"]}
        )
        assert result.isError
        assert "form types ['S-1']" in text(result)


async def test_result_cap(opener: Opener) -> None:
    policy = Policy.load()
    policy.limits.max_filings = 2
    async with opener("analyst", policy) as s:
        result = await s.call_tool("search_filings", {"ticker": "AAPL", "limit": 10})
        data = json.loads(text(result))
        assert len(data["filings"]) == 2
        assert "capped" in data["warnings"][0]


async def test_errors_are_audited(opener: Opener, audit_path: Path) -> None:
    async with opener("analyst") as analyst:
        await analyst.call_tool("search_filings", {"ticker": "AAPL", "filed_after": "yesterday"})
        line = audit_lines(audit_path)[-1]
        assert line["status"] == "error" and "YYYY-MM-DD" in line["error"]


async def test_every_call_audited(opener: Opener, audit_path: Path) -> None:
    async with opener("analyst") as analyst:
        await analyst.call_tool("search_filings", {"ticker": "AAPL", "limit": 1})
        await analyst.call_tool("validate_financials", {"ticker": "AAPL", "fiscal_year": 2024})
        await analyst.call_tool("get_filing_section", {"ticker": "GME", "item": "7"})
        assert [(x["tool"], x["status"]) for x in audit_lines(audit_path)] == [
            ("search_filings", "ok"),
            ("validate_financials", "ok"),
            ("get_filing_section", "denied"),
        ]


def test_role_from_env(monkeypatch: pytest.MonkeyPatch, audit_path: Path) -> None:
    monkeypatch.setenv("EDGAR_MCP_ROLE", "viewer")
    assert Guard(audit_path=audit_path).role == "viewer"


def test_unknown_role_rejected(audit_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="Unknown role 'admin'"):
        Guard(role="admin", audit_path=audit_path)
