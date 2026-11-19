from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from edgar_mcp.models import PeriodType
from edgar_mcp.quality import (
    FiscalCalendar,
    NormalizedFacts,
    check_units_match,
    normalize_company_facts,
    select_metric,
)
from tests.conftest import AS_OF, load_fixture


def norm(ticker: str) -> NormalizedFacts:
    return normalize_company_facts(load_fixture(f"companyfacts_{ticker}.json"), as_of=AS_OF)


def row(
    val: float,
    end: str,
    *,
    start: str | None = None,
    accn: str = "0000000001-24-000001",
    form: str = "10-K",
    filed: str = "2024-02-01",
    fy: int = 2023,
    fp: str = "FY",
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "end": end,
        "val": val,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }
    if start:
        r["start"] = start
    return r


def company(concepts: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    return {
        "cik": 1,
        "entityName": "Test Co",
        "facts": {"us-gaap": {c: {"label": c, "units": u} for c, u in concepts.items()}},
    }


# Calendar-year company with one 10-K for FY2023, used by the synthetic cases.
FY23 = row(1000, "2023-12-31", start="2023-01-01", accn="0000000001-24-000001")


# ---- dedup / restatement / amendments -----------------------------------------------------


def test_dedup_keeps_latest_filed_and_flags_restatement() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [
                    FY23,
                    # Next year's 10-K re-reports FY2023 with a restated value.
                    row(
                        1100,
                        "2023-12-31",
                        start="2023-01-01",
                        accn="0000000001-25-000001",
                        filed="2025-02-01",
                        fy=2024,
                    ),
                ]
            }
        }
    )
    facts = normalize_company_facts(raw, as_of=AS_OF).facts
    fy = [f for f in facts if f.fiscal_period == "FY"]
    assert len(fy) == 1
    assert fy[0].value == 1100
    assert fy[0].restated is True
    assert fy[0].prior_value == 1000
    assert fy[0].provenance.accession_number == "0000000001-25-000001"


def test_identical_repeats_are_not_restatements() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [
                    FY23,
                    row(
                        1000,
                        "2023-12-31",
                        start="2023-01-01",
                        accn="0000000001-25-000001",
                        filed="2025-02-01",
                        fy=2024,
                    ),
                ]
            }
        }
    )
    (fact,) = normalize_company_facts(raw, as_of=AS_OF).facts
    assert fact.restated is False
    assert fact.prior_value is None


def test_amendment_preferred_over_original_filed_same_day() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [
                    row(
                        1000,
                        "2023-12-31",
                        start="2023-01-01",
                        accn="0000000001-24-000009",
                        form="10-K/A",
                    ),
                    row(990, "2023-12-31", start="2023-01-01", accn="0000000001-24-000001"),
                ]
            }
        }
    )
    (fact,) = normalize_company_facts(raw, as_of=AS_OF).facts
    assert fact.value == 1000
    assert fact.amended is True
    assert fact.provenance.form == "10-K/A"


def test_real_restatement_celh_10k_amendment() -> None:
    """Celsius filed a 10-K/A in April 2023 that restated FY2020 net income incl. NCI."""
    facts, warnings = select_metric(norm("CELH"), "ProfitLoss", fiscal_years=[2020])
    (fact,) = facts
    assert fact.value == 8_524_000
    assert fact.prior_value == 9_048_234
    assert fact.restated and fact.amended
    assert fact.provenance.form == "10-K/A"
    assert fact.provenance.filed_date == date(2023, 4, 19)
    assert any(w.code == "restated" for w in warnings)


def test_non_periodic_forms_ignored() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [
                    FY23,
                    row(5, "2023-12-31", start="2023-01-01", form="DEF 14A", filed="2026-01-01"),
                ]
            }
        }
    )
    (fact,) = normalize_company_facts(raw, as_of=AS_OF).facts
    assert fact.value == 1000


# ---- duration vs instant ---------------------------------------------------------------------


def test_duration_and_instant_kept_separate() -> None:
    n = norm("AAPL")
    revenue, _ = select_metric(n, "revenue", fiscal_years=[2024])
    assets, _ = select_metric(n, "total_assets", fiscal_years=[2024])
    assert revenue[0].period_type is PeriodType.DURATION
    assert revenue[0].period_start is not None
    assert assets[0].period_type is PeriodType.INSTANT
    assert assets[0].period_start is None
    assert assets[0].period_end == date(2024, 9, 28)


# ---- fiscal calendar (non-December years) -------------------------------------------------


def test_september_fiscal_year_aapl() -> None:
    n = norm("AAPL")
    assert n.calendar.year_ends[2024] == date(2024, 9, 28)
    quarters, _ = select_metric(n, "revenue", fiscal_years=[2025], period="quarterly")
    q1 = quarters[0]
    assert q1.fiscal_period == "Q1"
    # FY2025 Q1 is Oct-Dec 2024 — calendar Q4, fiscal Q1.
    assert q1.period_start == date(2024, 9, 29)
    assert q1.period_end == date(2024, 12, 28)


def test_june_fiscal_year_msft() -> None:
    n = norm("MSFT")
    assert n.calendar.year_ends[2025] == date(2025, 6, 30)
    quarters, _ = select_metric(n, "revenue", fiscal_years=[2025], period="quarterly")
    assert [q.fiscal_period for q in quarters] == ["Q1", "Q2", "Q3", "Q4"]
    assert quarters[0].period_end == date(2024, 9, 30)


def test_calendar_extrapolates_past_last_10k() -> None:
    cal = FiscalCalendar({2024: date(2024, 9, 28)})
    fy, start, _ = cal.locate(date(2024, 12, 28)) or (0, None, None)
    assert fy == 2025
    assert start == date(2024, 9, 29)


# ---- YTD -> discrete quarters, Q4 derivation ----------------------------------------------


def test_ytd_values_converted_to_discrete_quarters() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [
                    FY23,
                    row(200, "2023-03-31", start="2023-01-01", form="10-Q", fp="Q1"),
                    row(450, "2023-06-30", start="2023-01-01", form="10-Q", fp="Q2"),  # H1 YTD
                    row(720, "2023-09-30", start="2023-01-01", form="10-Q", fp="Q3"),  # 9M YTD
                ]
            }
        }
    )
    n = normalize_company_facts(raw, as_of=AS_OF)
    q, _ = select_metric(n, "Revenues", fiscal_years=[2023], period="quarterly")
    assert [(f.fiscal_period, f.value) for f in q] == [
        ("Q1", 200),
        ("Q2", 250),
        ("Q3", 270),
        ("Q4", 280),
    ]
    q4 = q[-1]
    assert q4.derived and q4.derivation == "FY − (Q1+Q2+Q3)"
    assert q4.period_start == date(2023, 10, 1)
    assert len(q4.derived_from) == 4


def test_aapl_quarters_sum_to_fy_exactly() -> None:
    """Phase 2 acceptance: four derived quarters of revenue sum to reported FY exactly."""
    n = norm("AAPL")
    for fy in (2022, 2023, 2024, 2025):
        facts, _ = select_metric(n, "revenue", fiscal_years=[fy], period="all")
        by = {f.fiscal_period: f.value for f in facts}
        assert by["Q1"] + by["Q2"] + by["Q3"] + by["Q4"] == by["FY"], fy
    q, _ = select_metric(n, "revenue", fiscal_years=[2024], period="quarterly")
    assert q[-1].value == 94_930_000_000  # Apple's reported Q4 FY2024 revenue


def test_eps_not_derived_by_subtraction() -> None:
    n = norm("AAPL")
    q, _ = select_metric(n, "eps_diluted", fiscal_years=[2024], period="quarterly")
    assert all(not f.derived for f in q)
    assert "Q4" not in {f.fiscal_period for f in q}


def test_q4_not_derivable_is_reported() -> None:
    raw = company(
        {"Revenues": {"USD": [FY23, row(200, "2023-03-31", start="2023-01-01", form="10-Q")]}}
    )
    n = normalize_company_facts(raw, as_of=AS_OF)
    assert any(w.code == "q4_not_derivable" for w in n.warnings)


# ---- units ------------------------------------------------------------------------------------


def test_unit_normalization_usd_per_share() -> None:
    q, _ = select_metric(norm("AAPL"), "eps_diluted", fiscal_years=[2024])
    assert q[0].unit == "USD/share"


def test_mixed_units_rejected_with_warning() -> None:
    raw = company(
        {
            "Revenues": {
                "USD": [FY23],
                "EUR": [row(900, "2023-12-31", start="2023-01-01")],
            }
        }
    )
    n = normalize_company_facts(raw, as_of=AS_OF)
    assert {f.unit for f in n.facts} == {"USD"}
    (w,) = [w for w in n.warnings if w.code == "mixed_units"]
    assert "EUR" in w.message


def test_arithmetic_over_mixed_units_refused() -> None:
    n = norm("AAPL")
    rev, _ = select_metric(n, "revenue", fiscal_years=[2024])
    eps, _ = select_metric(n, "eps_diluted", fiscal_years=[2024])
    assert check_units_match(rev) == "USD"
    with pytest.raises(ValueError, match="mixed units"):
        check_units_match(rev + eps)


# ---- provenance + warnings ------------------------------------------------------------------


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "CELH"])
def test_every_fact_has_complete_provenance(ticker: str) -> None:
    n = norm(ticker)
    assert n.facts
    for f in n.facts:
        p = f.provenance
        assert p.accession_number and p.form and p.filed_date and p.concept and p.unit
        assert p.period_end and p.source_url.startswith("https://www.sec.gov/Archives/")
        assert (p.period_start is None) == (f.period_type is PeriodType.INSTANT)
        assert f.as_of == AS_OF


def test_concept_change_across_years_warns() -> None:
    raw = company(
        {
            "SalesRevenueNet": {
                "USD": [
                    row(
                        900,
                        "2022-12-31",
                        start="2022-01-01",
                        fy=2022,
                        accn="0000000001-23-000001",
                        filed="2023-02-01",
                    )
                ]
            },
            "Revenues": {"USD": [FY23]},
        }
    )
    facts, warnings = select_metric(normalize_company_facts(raw, as_of=AS_OF), "revenue")
    assert [f.concept for f in facts] == ["SalesRevenueNet", "Revenues"]
    assert any(w.code == "concept_changed" for w in warnings)


def test_missing_year_warns() -> None:
    facts, warnings = select_metric(norm("AAPL"), "revenue", fiscal_years=[1990])
    assert facts == []
    assert warnings[0].code == "no_data"


def test_no_10k_data_warns() -> None:
    n = normalize_company_facts(company({}), as_of=AS_OF)
    assert n.warnings[0].code == "no_fiscal_calendar"
