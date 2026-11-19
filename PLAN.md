# Build Plan

Work through one phase per Claude Code session (or use `/phase N`). For each
phase: enter plan mode first (Shift+Tab), agree on the approach, then
implement, tick boxes, and stop at the acceptance criteria.

---

## Phase 0: Scaffold
- [x] `uv init`, add dependencies listed in CLAUDE.md, create the layout
- [x] `.gitignore` (`.env`, `.cache/`, `audit.log.jsonl`, `__pycache__`)
- [x] `.env.example` with `SEC_USER_AGENT=` and `ANTHROPIC_API_KEY=`
- [x] ruff + mypy config in `pyproject.toml`; one trivial passing test

**Acceptance:** `uv run pytest -q`, `ruff check .` and `mypy src` all pass.

---

## Phase 1: EDGAR data layer (`edgar.py`, `models.py`)
- [x] Async `EdgarClient` with httpx, User-Agent from env, rate limiter, on-disk cache with TTL
- [x] `ticker_to_cik(ticker)` using SEC's company tickers file; zero-pad to 10
- [x] `get_submissions(cik)` → filing list (form, accession, filed date, primary doc URL)
- [x] `get_company_facts(cik)` → raw XBRL companyfacts
- [x] `get_filing_document(accession, cik)` → HTML/text
- [x] `extract_section(doc, item)` for 10-K Items 1A and 7
- [x] Pydantic models: `Provenance`, `Fact`, `Filing`
- [x] Fixture script `scripts/fetch_fixtures.py` for AAPL, MSFT, and one company with a 10-K/A
- [x] Tests with fixtures + respx: retries, 429 handling, cache hit, missing UA error

**Acceptance:** all tests green with no network; a manual run of
`ticker_to_cik("AAPL")` returns `0000320193`.

---

## Phase 2: Data-quality controls (`quality.py`)
- [x] Dedup facts per (concept, unit, period); keep latest filed; set `restated`
- [x] Split duration vs instant facts
- [x] Convert YTD 10-Q values to discrete quarters; derive Q4 = FY − (Q1+Q2+Q3)
- [x] Handle non-December fiscal years using `fy`/`fp` + dates
- [x] Prefer amended filings over originals
- [x] Unit normalization (USD, shares, USD/share); reject mixed units
- [x] Attach full `Provenance` and an `as_of` timestamp to every output fact
- [x] Tests for each rule, including a restatement case and a Sept fiscal year

**Acceptance:** for AAPL fixtures, the four derived quarters of revenue sum to the
reported FY revenue exactly, and every returned fact has complete provenance.

---

## Phase 3: Filing-specific validation (`validate.py`)
Each check returns `CheckResult(name, status: pass|warn|fail, detail, facts_used)`.
- [x] Balance sheet identity: Assets ≈ Liabilities + Equity (tolerance 0.5%)
- [x] Quarters sum to annual (revenue, net income, operating cash flow)
- [x] Gross profit = Revenue − COGS when all three exist
- [x] Sign/range: revenue ≥ 0; effective tax rate and margins within plausible bounds
- [x] YoY change > 50% → warn
- [x] Format checks: accession number, CIK, form type
- [x] `run_all(ticker, fiscal_year)` → list of results
- [x] Tests: one passing and one deliberately broken fixture per check

**Acceptance:** every check has a pass test and a fail/warn test; the broken
fixture is caught.

---

## Phase 4: MCP server (`server.py`)
- [x] Tools: `search_filings`, `get_financial_facts`, `get_filing_section`, `validate_financials`
- [x] Clear docstrings and typed params (these are what the model reads)
- [x] Resource template `edgar://{cik}/{accession}` for filing documents
- [x] Filing text wrapped as untrusted (`<filing_text>` delimiters)
- [x] Tool errors return clear, recoverable messages (bad ticker, no data for period)
- [x] Add project `.mcp.json` so Claude Code can load the server

**Acceptance:** each tool works in `mcp dev` (MCP Inspector); in a fresh Claude
Code session, `/mcp` lists `edgar` and "What was Apple's FY revenue in its last
10-K?" is answered via the tool with provenance.

---

## Phase 5: Permissions & audit (`permissions.py`)
- [x] Roles from `config/roles.yaml`: `analyst` (all tools), `viewer` (facts + validation only)
- [x] Active role from `EDGAR_MCP_ROLE` env var (stdio); denied calls return a clear error
- [x] Allowlists: tickers and form types; per-call result caps
- [x] Audit log: JSONL line per call (timestamp, role, tool, args, result hash, status)
- [x] Tests: viewer is denied `get_filing_section`; non-allowlisted ticker is denied; audit line written

**Acceptance:** permission tests pass; audit log shows every call from a manual session.

---

## Phase 6: Agent workflow (`agent/`) (REMOVED: scope cut to the MCP server only)
- [x] `agent.py`: connects to the server over stdio via the MCP client, converts tools to Anthropic format, runs the tool-use loop with a step cap
- [x] System prompt: fetch before calculating, state assumptions, cite filings, never invent numbers
- [x] `verify.py`: extract every number in the final answer and check it traces to a tool result's provenance (or is a labeled assumption/derived calc); flag the rest
- [x] `export_excel`: write a model workbook (inputs with citations, calc sheet, summary)
- [x] Log each tool call and result to the console

**Acceptance:** `agent.py "Build a 5-year DCF for MSFT"` produces an answer,
`verify.py` reports zero untraced numbers, and an .xlsx is written.

---

## Phase 7: Evals & docs
- [ ] `evals/questions.yaml`: 10–15 hand-verified questions (normal case, odd fiscal year, restatement, Q4 derivation, a permission denial)
- [ ] `evals/run_evals.py`: runs each through the MCP tools (agent removed), scores exact/within-tolerance, prints a table
- [x] README: architecture diagram (Mermaid), tool list, permission model, table of validation checks, setup steps, demo GIF placeholder

**Acceptance:** evals ≥ 90% pass; README lets a stranger run it in under 10 minutes.

---

## Phase 8 (optional): Remote connector
- [ ] Streamable HTTP transport
- [ ] OAuth per the MCP authorization spec; map token scopes to roles
- [ ] Deploy (e.g. Fly.io / Render); add as a custom connector in Claude

**Acceptance:** a Claude client connects remotely, and an unauthorized token is rejected.
