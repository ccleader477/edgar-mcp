"""Each check: a passing case on real AAPL data and a deliberately broken copy of it."""

from __future__ import annotations

import copy
from typing import Any

import respx

from edgar_mcp.edgar import EdgarClient
from edgar_mcp.quality import normalize_company_facts
from edgar_mcp.validate import (
    check_balance_sheet,
    check_formats,
    check_gross_profit,
    check_quarters_sum,
    check_sign_and_range,
    check_yoy,
    run_all,
    run_checks,
    summarize,
)
from tests.conftest import AS_OF, load_fixture

FY = 2024
FYE = "2024-09-28"
FY_START = "2023-10-01"
CIK = "0000320193"


def aapl() -> dict[str, Any]:
    return copy.deepcopy(load_fixture("companyfacts_AAPL.json"))


def broken(concept: str, end: str, start: str | None, fn: Any) -> Any:
    """AAPL facts with every filing's copy of one (concept, period) value altered."""
    raw = aapl()
    hit = 0
    for r in raw["facts"]["us-gaap"][concept]["units"]["USD"]:
        if r["end"] == end and r.get("start") == start:
            r["val"] = fn(r["val"])
            hit += 1
    assert hit, f"fixture has no {concept} {start}..{end}"
    return normalize_company_facts(raw, as_of=AS_OF)


GOOD = normalize_company_facts(load_fixture("companyfacts_AAPL.json"), as_of=AS_OF)


# ---- balance sheet ----------------------------------------------------------------------------


def test_balance_sheet_pass() -> None:
    r = check_balance_sheet(GOOD, FY)
    assert r.status == "pass", r.detail
    assert {f.concept for f in r.facts_used} == {"Assets", "Liabilities", "StockholdersEquity"}


def test_balance_sheet_broken_fails() -> None:
    norm = broken("Assets", FYE, None, lambda v: v * 1.10)
    r = check_balance_sheet(norm, FY)
    assert r.status == "fail"
    assert "diff" in r.detail


def test_balance_sheet_untagged_component_warns() -> None:
    norm = broken("Liabilities", FYE, None, lambda v: v * 0.9)
    r = check_balance_sheet(norm, FY)
    assert r.status == "warn"
    assert "doesn't expose" in r.detail


# ---- quarters sum to annual -----------------------------------------------------------------


def test_quarters_sum_pass() -> None:
    for metric in ("revenue", "net_income", "operating_cash_flow"):
        r = check_quarters_sum(GOOD, FY, metric)
        assert r.status == "pass", (metric, r.detail)


def test_quarters_sum_broken_ytd_fails() -> None:
    # The Q2 10-Q's H1 YTD revenue no longer equals its own Q1 + Q2.
    norm = broken(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "2024-03-30",
        FY_START,
        lambda v: v + 5_000_000_000,
    )
    r = check_quarters_sum(norm, FY, "revenue")
    assert r.status == "fail"
    assert "H1 YTD" in r.detail


def test_quarters_sum_broken_fy_flags_derived_q4() -> None:
    norm = broken(
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        FYE,
        FY_START,
        lambda v: v - 300_000_000_000,
    )
    r = check_quarters_sum(norm, FY, "revenue")
    assert r.status == "fail"
    assert "negative" in r.detail


# ---- gross profit ------------------------------------------------------------------------------


def test_gross_profit_pass() -> None:
    assert check_gross_profit(GOOD, FY).status == "pass"


def test_gross_profit_broken_fails() -> None:
    norm = broken("GrossProfit", FYE, FY_START, lambda v: v + 10_000_000_000)
    r = check_gross_profit(norm, FY)
    assert r.status == "fail"


def test_gross_profit_missing_inputs_warns() -> None:
    raw = aapl()
    del raw["facts"]["us-gaap"]["GrossProfit"]
    r = check_gross_profit(normalize_company_facts(raw, as_of=AS_OF), FY)
    assert r.status == "warn"
    assert "Skipped" in r.detail


# ---- sign / range ------------------------------------------------------------------------------


def test_sign_and_range_pass() -> None:
    assert check_sign_and_range(GOOD, FY).status == "pass"


def test_negative_revenue_fails() -> None:
    norm = broken(
        "RevenueFromContractWithCustomerExcludingAssessedTax", FYE, FY_START, lambda v: -v
    )
    r = check_sign_and_range(norm, FY)
    assert r.status == "fail"
    assert "negative" in r.detail


def test_implausible_tax_rate_warns() -> None:
    norm = broken("IncomeTaxExpenseBenefit", FYE, FY_START, lambda v: v * 4)
    r = check_sign_and_range(norm, FY)
    assert r.status == "warn"
    assert "effective tax rate" in r.detail


# ---- YoY ----------------------------------------------------------------------------------------


def test_yoy_pass() -> None:
    assert check_yoy(GOOD, FY).status == "pass"


def test_yoy_jump_warns() -> None:
    norm = broken(
        "RevenueFromContractWithCustomerExcludingAssessedTax", FYE, FY_START, lambda v: v * 2
    )
    r = check_yoy(norm, FY)
    assert r.status == "warn"
    assert "revenue changed +" in r.detail


# ---- formats -----------------------------------------------------------------------------------


def test_formats_pass() -> None:
    facts = check_balance_sheet(GOOD, FY).facts_used
    assert check_formats(CIK, facts).status == "pass"


def test_formats_broken_fail() -> None:
    good = check_balance_sheet(GOOD, FY).facts_used[0]
    bad_accn = good.model_copy(
        update={"provenance": good.provenance.model_copy(update={"accession_number": "123"})}
    )
    bad_form = good.model_copy(
        update={"provenance": good.provenance.model_copy(update={"form": "S-1"})}
    )
    r = check_formats("320193", [bad_accn, bad_form])
    assert r.status == "fail"
    assert "not 10 digits" in r.detail
    assert "bad accession" in r.detail
    assert "unexpected form type 'S-1'" in r.detail


# ---- runners -----------------------------------------------------------------------------------


def test_run_checks_all_pass_for_aapl() -> None:
    results = run_checks(GOOD, FY, CIK)
    assert summarize(results) == {"pass": 8, "warn": 0, "fail": 0}


def test_real_untagged_mezzanine_equity_celh_warns() -> None:
    norm = normalize_company_facts(load_fixture("companyfacts_CELH.json"), as_of=AS_OF)
    assert check_balance_sheet(norm, 2025).status == "warn"


async def test_run_all(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    results = await run_all("MSFT", 2025, client=client)
    assert summarize(results)["fail"] == 0
    assert {r.name for r in results} >= {"balance_sheet_identity", "format_checks"}
