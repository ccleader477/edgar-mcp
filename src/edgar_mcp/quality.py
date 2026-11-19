"""Data-quality controls for XBRL companyfacts.

Pipeline (see CLAUDE.md "Domain notes"):
1. Keep only periodic-report facts (10-K/10-Q and amendments).
2. Normalize units; reject mixed units per concept (dominant unit kept, the rest reported).
3. Dedupe per (concept, unit, period): keep the most recently *filed* value, preferring an
   amendment on ties; flag ``restated`` when the value changed across filings.
4. Assign each fact its own fiscal year/period from dates, anchored on 10-K fiscal year ends
   (companyfacts ``fy``/``fp`` describe the *filing*, not the fact).
5. Derive discrete quarters from YTD values; Q4 = FY - (Q1+Q2+Q3).
6. Attach full Provenance and an ``as_of`` timestamp to every output fact.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from edgar_mcp.edgar import archive_url
from edgar_mcp.models import Fact, PeriodType, Provenance, QualityWarning

PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "10-KT", "10-KT/A"}

UNIT_ALIASES = {
    "USD": "USD",
    "shares": "shares",
    "USD/shares": "USD/share",
    "USD/share": "USD/share",
    "pure": "pure",
}
ADDITIVE_UNITS = {"USD"}  # only monetary flows can be differenced into quarters

# Friendly metric name -> candidate concepts, most preferred first. "dei:" prefix selects
# the dei taxonomy; anything else is us-gaap.
METRICS: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
    ],
    "cost_of_revenue": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ],
    "stock_based_compensation": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "total_equity_incl_nci": [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ],
    "temporary_equity": ["TemporaryEquityCarryingAmountAttributableToParent"],
    "liabilities_and_equity": ["LiabilitiesAndStockholdersEquity"],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "shares_outstanding": ["dei:EntityCommonStockSharesOutstanding"],
}

ALL_METRIC_CONCEPTS = sorted({c for cs in METRICS.values() for c in cs})

PeriodFilter = Literal["annual", "quarterly", "all"]


def split_concept(name: str) -> tuple[str, str]:
    taxonomy, _, concept = name.rpartition(":")
    return (taxonomy or "us-gaap"), concept


def resolve_metric(name: str) -> list[str]:
    """Friendly metric -> candidate concepts; a raw concept name resolves to itself."""
    key = name.strip()
    return METRICS.get(key.lower(), [key])


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _days(start: date, end: date) -> int:
    return (end - start).days + 1


# ---- fiscal calendar ---------------------------------------------------------------------


@dataclass
class FiscalCalendar:
    """Fiscal-year ends learned from 10-K filings: {fiscal_year: fye_date}."""

    year_ends: dict[int, date] = field(default_factory=dict)

    @classmethod
    def from_companyfacts(cls, raw: dict[str, Any]) -> FiscalCalendar:
        # For each 10-K accession, the latest annual period end is that filing's own FYE.
        per_accn: dict[str, tuple[int, date]] = {}
        for body in raw.get("facts", {}).get("us-gaap", {}).values():
            for rows in body.get("units", {}).values():
                for r in rows:
                    if (
                        r.get("form") not in ("10-K", "10-K/A", "10-KT", "10-KT/A")
                        or r.get("fp") != "FY"
                        or not r.get("start")
                        or not isinstance(r.get("fy"), int)
                    ):
                        continue
                    if not 340 <= _days(_d(r["start"]), _d(r["end"])) <= 380:
                        continue
                    end = _d(r["end"])
                    prev = per_accn.get(r["accn"])
                    if prev is None or end > prev[1]:
                        per_accn[r["accn"]] = (r["fy"], end)
        votes: dict[int, Counter[date]] = defaultdict(Counter)
        for fy, end in per_accn.values():
            votes[fy][end] += 1
        return cls({fy: c.most_common(1)[0][0] for fy, c in votes.items()})

    def locate(self, end: date) -> tuple[int, date, date] | None:
        """Return (fiscal_year, fy_start, fy_end) for a period ending on ``end``."""
        if not self.year_ends:
            return None
        ordered = sorted(self.year_ends.items(), key=lambda kv: kv[1])
        for i, (fy, fye) in enumerate(ordered):
            if end <= fye + timedelta(days=3):
                if i > 0:
                    start = ordered[i - 1][1] + timedelta(days=1)
                else:
                    start = fye - timedelta(days=364)
                if end < start - timedelta(days=3):
                    return None  # before the first known fiscal year
                return fy, start, fye
        # After the last known FYE: extrapolate whole 52/53-week years forward.
        last_fy, last_fye = ordered[-1]
        k = 1
        while end > last_fye + timedelta(days=371 * k):
            k += 1
        fy_start = last_fye + timedelta(days=1) + timedelta(days=364 * (k - 1))
        return last_fy + k, fy_start, last_fye + timedelta(days=364 * k)


def _near(a: date, b: date) -> bool:
    return abs((a - b).days) <= 7


def classify_period(
    period_type: PeriodType, start: date | None, end: date, cal: FiscalCalendar
) -> tuple[int | None, str | None]:
    loc = cal.locate(end)
    if loc is None:
        return None, None
    fy, fy_start, fy_end = loc
    if period_type is PeriodType.INSTANT:
        if _near(end, fy_end):
            return fy, "FY"
        q = round((end - fy_start).days / 91.3)
        return fy, (f"Q{q}" if 1 <= q <= 3 else None)
    assert start is not None
    days = _days(start, end)
    if 70 <= days <= 110:
        q = round((start - fy_start).days / 91.3) + 1
        return fy, (f"Q{q}" if 1 <= q <= 4 else None)
    if _near(start, fy_start):
        if 340 <= days <= 380:
            return fy, "FY"
        if 160 <= days <= 200:
            return fy, "H1"
        if 250 <= days <= 290:
            return fy, "9M"
    return fy, None


# ---- normalization -----------------------------------------------------------------------


@dataclass
class NormalizedFacts:
    facts: list[Fact]
    warnings: list[QualityWarning]
    calendar: FiscalCalendar


def _provenance(cik: str, concept: str, unit: str, r: dict[str, Any]) -> Provenance:
    return Provenance(
        accession_number=r["accn"],
        form=r["form"],
        filed_date=_d(r["filed"]),
        concept=concept,
        unit=unit,
        period_start=_d(r["start"]) if r.get("start") else None,
        period_end=_d(r["end"]),
        source_url=archive_url(cik, r["accn"], ""),
    )


def _winner_key(r: dict[str, Any]) -> tuple[str, bool, str]:
    # Latest filed wins; on the same filing date an amendment beats the original.
    return (r["filed"], str(r["form"]).endswith("/A"), r["accn"])


def normalize_company_facts(
    raw: dict[str, Any],
    *,
    as_of: datetime,
    concepts: list[str] | None = None,
) -> NormalizedFacts:
    """Dedup, classify, unit-check and derive quarters for the requested concepts."""
    cik = str(raw.get("cik", "0")).zfill(10)
    cal = FiscalCalendar.from_companyfacts(raw)
    warnings: list[QualityWarning] = []
    if not cal.year_ends:
        warnings.append(
            QualityWarning(
                code="no_fiscal_calendar",
                message="No 10-K annual facts found; fiscal years/quarters cannot be assigned.",
            )
        )
    wanted = concepts if concepts is not None else ALL_METRIC_CONCEPTS
    facts: list[Fact] = []
    for name in wanted:
        taxonomy, concept = split_concept(name)
        body = raw.get("facts", {}).get(taxonomy, {}).get(concept)
        if body is None:
            continue
        units: dict[str, list[dict[str, Any]]] = {}
        for unit_raw, rows in body.get("units", {}).items():
            rows = [r for r in rows if r.get("form") in PERIODIC_FORMS]
            if rows:
                units.setdefault(UNIT_ALIASES.get(unit_raw, unit_raw), []).extend(rows)
        if not units:
            continue
        if len(units) > 1:
            keep = "USD" if "USD" in units else max(units, key=lambda u: len(units[u]))
            dropped = {u: len(rs) for u, rs in units.items() if u != keep}
            warnings.append(
                QualityWarning(
                    code="mixed_units",
                    concept=name,
                    message=f"{name} is reported in several units; kept {keep}, "
                    f"rejected {dropped} (facts in other units are not comparable).",
                )
            )
            units = {keep: units[keep]}
        for unit, rows in units.items():
            if unit not in UNIT_ALIASES.values():
                warnings.append(
                    QualityWarning(
                        code="unusual_unit",
                        concept=name,
                        message=f"{name} uses unit {unit!r}, not USD/shares/USD-per-share.",
                    )
                )
            facts.extend(_dedupe(cik, taxonomy, concept, body.get("label"), unit, rows, cal, as_of))
    facts.extend(derive_quarters(facts, warnings, as_of))
    return NormalizedFacts(facts=facts, warnings=warnings, calendar=cal)


def _dedupe(
    cik: str,
    taxonomy: str,
    concept: str,
    label: str | None,
    unit: str,
    rows: list[dict[str, Any]],
    cal: FiscalCalendar,
    as_of: datetime,
) -> list[Fact]:
    groups: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r.get("start"), r["end"])].append(r)
    out: list[Fact] = []
    for (start_s, end_s), rs in groups.items():
        winner = max(rs, key=_winner_key)
        differing = sorted(
            (r for r in rs if r["val"] != winner["val"]), key=lambda r: (r["filed"], r["accn"])
        )
        start = _d(start_s) if start_s else None
        end = _d(end_s)
        ptype = PeriodType.DURATION if start else PeriodType.INSTANT
        fy, fp = classify_period(ptype, start, end, cal)
        out.append(
            Fact(
                concept=concept,
                taxonomy=taxonomy,
                label=label,
                value=float(winner["val"]),
                unit=unit,
                period_type=ptype,
                period_start=start,
                period_end=end,
                fiscal_year=fy,
                fiscal_period=fp,
                provenance=_provenance(cik, concept, unit, winner),
                restated=bool(differing),
                prior_value=float(differing[0]["val"]) if differing else None,
                amended=str(winner["form"]).endswith("/A"),
                as_of=as_of,
            )
        )
    return out


# ---- quarter derivation ------------------------------------------------------------------


def derive_quarters(
    facts: list[Fact], warnings: list[QualityWarning], as_of: datetime
) -> list[Fact]:
    """Discrete quarters from YTD values; Q4 = FY - (Q1+Q2+Q3). Returns only new facts."""
    by_year: dict[tuple[str, str, int], dict[str, Fact]] = defaultdict(dict)
    for f in facts:
        if (
            f.period_type is PeriodType.DURATION
            and f.unit in ADDITIVE_UNITS
            and f.fiscal_year is not None
            and f.fiscal_period in {"Q1", "Q2", "Q3", "Q4", "H1", "9M", "FY"}
        ):
            by_year[(f.concept, f.unit, f.fiscal_year)][f.fiscal_period] = f
    new: list[Fact] = []
    for (concept, _unit, fy), p in sorted(by_year.items()):
        q: dict[str, Fact] = {k: p[k] for k in ("Q1", "Q2", "Q3") if k in p}
        if "Q2" not in q and "H1" in p and "Q1" in q:
            q["Q2"] = _derived(p["H1"], [q["Q1"]], "Q2", "H1 YTD − Q1", as_of)
            new.append(q["Q2"])
        if "Q3" not in q and "9M" in p:
            if "H1" in p:
                q["Q3"] = _derived(p["9M"], [p["H1"]], "Q3", "9M YTD − H1 YTD", as_of)
            elif "Q1" in q and "Q2" in q:
                q["Q3"] = _derived(p["9M"], [q["Q1"], q["Q2"]], "Q3", "9M YTD − (Q1+Q2)", as_of)
            if "Q3" in q:
                new.append(q["Q3"])
        if "FY" not in p:
            continue
        if all(k in q for k in ("Q1", "Q2", "Q3")):
            q4 = _derived(p["FY"], [q["Q1"], q["Q2"], q["Q3"]], "Q4", "FY − (Q1+Q2+Q3)", as_of)
            reported = p.get("Q4")
            if reported is None:
                new.append(q4)
            elif reported.value != q4.value:
                warnings.append(
                    QualityWarning(
                        code="q4_mismatch",
                        concept=concept,
                        message=f"FY{fy} {concept}: reported Q4 {reported.value:,.0f} != "
                        f"FY − (Q1+Q2+Q3) = {q4.value:,.0f}.",
                    )
                )
        elif any(k in p for k in ("Q1", "H1", "9M")):
            missing = [k for k in ("Q1", "Q2", "Q3") if k not in q]
            warnings.append(
                QualityWarning(
                    code="q4_not_derivable",
                    concept=concept,
                    message=f"FY{fy} {concept}: cannot derive Q4, missing {missing}.",
                )
            )
    return new


def _derived(base: Fact, minus: list[Fact], period: str, how: str, as_of: datetime) -> Fact:
    value = base.value - sum(m.value for m in minus)
    last_end = max(m.period_end for m in minus)
    start = last_end + timedelta(days=1)
    prov = base.provenance.model_copy(update={"period_start": start})
    return Fact(
        concept=base.concept,
        taxonomy=base.taxonomy,
        label=base.label,
        value=value,
        unit=base.unit,
        period_type=PeriodType.DURATION,
        period_start=start,
        period_end=base.period_end,
        fiscal_year=base.fiscal_year,
        fiscal_period=period,
        provenance=prov,
        restated=base.restated or any(m.restated for m in minus),
        amended=base.amended,
        derived=True,
        derivation=how,
        derived_from=[base.provenance, *(m.provenance for m in minus)],
        as_of=as_of,
    )


# ---- selection ---------------------------------------------------------------------------


def select_metric(
    norm: NormalizedFacts,
    metric: str,
    *,
    fiscal_years: list[int] | None = None,
    period: PeriodFilter = "annual",
) -> tuple[list[Fact], list[QualityWarning]]:
    """Facts for a metric, picking per fiscal year the first candidate concept with data."""
    candidates = [split_concept(c)[1] for c in resolve_metric(metric)]
    wanted_periods = {
        "annual": {"FY"},
        "quarterly": {"Q1", "Q2", "Q3", "Q4"},
        "all": {"FY", "Q1", "Q2", "Q3", "Q4"},
    }[period]
    pool: dict[int, dict[str, list[Fact]]] = defaultdict(lambda: defaultdict(list))
    for f in norm.facts:
        if (
            f.concept in candidates
            and f.fiscal_year is not None
            and f.fiscal_period in wanted_periods
        ):
            pool[f.fiscal_year][f.concept].append(f)
    warnings: list[QualityWarning] = []
    out: list[Fact] = []
    used: dict[int, str] = {}
    years = sorted(pool) if fiscal_years is None else fiscal_years
    for fy in years:
        by_concept = pool.get(fy, {})
        chosen = next((c for c in candidates if by_concept.get(c)), None)
        if chosen is None:
            warnings.append(
                QualityWarning(
                    code="no_data",
                    concept=metric,
                    message=f"No {period} data for {metric!r} in fiscal year {fy}.",
                )
            )
            continue
        used[fy] = chosen
        # Instants: keep only the fiscal-period-end balance (latest end in the period).
        chosen_facts = _one_per_period(by_concept[chosen])
        out.extend(sorted(chosen_facts, key=lambda f: (f.period_end, f.fiscal_period or "")))
    if len(set(used.values())) > 1:
        warnings.append(
            QualityWarning(
                code="concept_changed",
                concept=metric,
                message=f"{metric!r} is tagged with different concepts across years: "
                + ", ".join(f"FY{y}={c}" for y, c in sorted(used.items())),
            )
        )
    for f in out:
        if f.restated:
            before = (
                f"originally {f.prior_value:,.2f}, now {f.value:,.2f}"
                if f.prior_value is not None
                else f"derived from restated inputs, now {f.value:,.2f}"
            )
            warnings.append(
                QualityWarning(
                    code="restated",
                    concept=f.concept,
                    message=f"FY{f.fiscal_year} {f.fiscal_period} {f.concept} was restated: "
                    f"{before} (per {f.provenance.form} {f.provenance.accession_number}).",
                )
            )
    return out, warnings


def _one_per_period(facts: list[Fact]) -> list[Fact]:
    best: dict[str, Fact] = {}
    for f in facts:
        key = f.fiscal_period or ""
        cur = best.get(key)
        # Prefer the canonical (non-derived) fact, then the latest period end, then
        # the shortest duration (a discrete quarter over an odd span).
        rank = (not f.derived, f.period_end, -(_span(f)))
        if cur is None or rank > (not cur.derived, cur.period_end, -(_span(cur))):
            best[key] = f
    return list(best.values())


def _span(f: Fact) -> int:
    return _days(f.period_start, f.period_end) if f.period_start else 0


def check_units_match(facts: list[Fact]) -> str:
    """Raise ValueError if facts mix units; return the shared unit."""
    units = {f.unit for f in facts}
    if len(units) != 1:
        raise ValueError(f"Refusing arithmetic over mixed units: {sorted(units)}")
    return units.pop()
