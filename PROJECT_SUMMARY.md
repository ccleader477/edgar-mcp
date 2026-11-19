# edgar-mcp: project summary

Last updated 2026-11-19.

## What it is
A simple MCP server, written in Python with FastMCP, that brings SEC EDGAR financial
filings into Claude and other MCP clients. It lives on my machine at `Desktop\edgar-mcp`
and runs locally over stdio. Claude Code starts it through the project's `.mcp.json`.

It needs no Anthropic API key. It only needs `SEC_USER_AGENT` (a name and email, which
SEC requires) in a local `.env` file that git ignores.

## Tools
| Tool | What it does |
|---|---|
| `search_filings(ticker, form_types, limit, filed_after)` | Lists a company's filings, newest first |
| `get_financial_facts(ticker, metrics, fiscal_years, period)` | Reported XBRL values (revenue, net income, assets…), annual or quarterly |
| `get_filing_section(ticker, item, accession_number, max_chars, offset)` | Text of 10-K Item 1A (Risk Factors) or Item 7 (MD&A) |
| `validate_financials(ticker, fiscal_year)` | Data checks: balance sheet balances, quarters sum to the year, gross profit, sign/range, big year-over-year swings, ID formats |

There is also one resource, `edgar://{cik}/{accession}`, which returns the full text of a filing's main document.

## Safeguards
- **Provenance on every number:** each value carries the accession number, form, filed
  date, XBRL concept, unit, period start and end, and a source URL.
- **Data quality:**
  - Duplicate facts are collapsed, with the latest filing winning, and restated values are flagged.
  - Amendments win over the original filings.
  - Year-to-date values become discrete quarters, and Q4 is derived as the full year minus Q1–Q3.
  - Non-calendar fiscal years are handled, such as Apple ending in September and Microsoft in June.
- **Problems are never hidden:** data-quality and validation problems come back as
  structured warnings in the result instead of being dropped.
- **Untrusted filing text:** text from filings is wrapped in `<filing_text>` tags.
- **Read-only:** no tool writes or sends anything, except the local cache and audit log.
- **Permissions** (`config/roles.yaml`):
  - The `analyst` role gets everything. The `viewer` role gets only numbers and validation.
  - Tickers are limited to an allowlist of about 31 large caps (plus CELH).
  - Allowed form types are 10-K, 10-Q, 8-K, 20-F, 40-F and DEF 14A, including amendments.
  - Each call has size limits.
  - Every call is written to `audit.log.jsonl`.
- **SEC etiquette:** every request sends the User-Agent, requests are limited to 8 per
  second, and responses are cached on disk.

## Code layout
```
src/edgar_mcp/  edgar.py (HTTP client), models.py, quality.py, validate.py,
                permissions.py, server.py
config/         roles.yaml
tests/          84 tests using saved fixtures (no live network)
scripts/        fetch_fixtures.py
```

## Status
- Plan phases 0–5 are done: setup, EDGAR client, data quality, validation, the server
  and permissions.
- All 84 tests pass, and ruff (lint) and mypy (types) are clean.
- It was tested on live SEC data:
  - Apple's latest 10-K is accession 0000320193-26-000026, with FY2026 revenue of $478.834B.
  - Validation passed 8 of 8 checks.
- Phase 6 (a separate modeling agent that built DCF models in Excel) was **removed**. I
  wanted a simple MCP server, and the agent was the only part that needed an Anthropic
  API key.

## What's left
1. Check it in Claude Code: open a session in `edgar-mcp`, run `/mcp`, and confirm `edgar`
   is connected with 4 tools.
2. Phase 7:
   - Write 10–15 hand-verified eval questions (`evals/questions.yaml`), run through the MCP tools directly.
   - Write a README with setup steps, the tool list, the permission model and the validation checks.
3. Put the code in git and on GitHub. Nothing is committed yet.
4. Optional Phase 8: run it as a remote connector over HTTP with OAuth.

## Commands
Run these from `edgar-mcp`. `uv` isn't on PATH, so it's run as `python -m uv`.
- Install: `python -m uv sync`
- Tests: `python -m uv run pytest -q`
- Lint and types: `python -m uv run ruff check .` and `python -m uv run mypy src`
- Inspector: `python -m uv run mcp dev src/edgar_mcp/server.py`
