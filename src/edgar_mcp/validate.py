"""Filing-specific validation checks.

Every check returns ``CheckResult(name, status, detail, facts_used)``. Missing inputs are a
``warn`` ("could not check"), never a silent pass (CLAUDE.md rule 6).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from edgar_mcp.edgar import EdgarClient
from edgar_mcp.models import CheckResult, Fact
from edgar_mcp.quality import NormalizedFacts, normalize_company_facts, select_metric

BALANCE_TOLERANCE = 0.005  # 0.5%
SUM_TOLERANCE = 0.005
YOY_WARN = 0.50
TAX_RATE_BOUNDS = (0.0, 0.50)
GROSS_MARGIN_BOUNDS = (-0.5, 1.0)
NET_MARGIN_BOUNDS = (-1.0, 1.0)

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
CIK_RE = re.compile(r"^\d{10}$")
KNOWN_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "10-KT",
    "10-KT/A",
    "8-K",
    "8-K/A",
    "20-F",
    "40-F",
}
QUARTER_SUM_METRICS = ("revenue", "net_income", "operating_cash_flow")


def _annual(norm: NormalizedFacts, metric: str, fy: int) -> Fact | None:
    facts, _ = select_metric(norm, metric, fiscal_years=[fy], period="annual")
    return facts[0] if facts else None


def _fmt(v: float) -> str:
    return f"{v:,.0f}"


def _rel_diff(a: float, b: float) -> float:
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom


# ---- checks -----------------------------------------------------------------------------------


def check_balance_sheet(norm: NormalizedFacts, fy: int) -> CheckResult:
    name = "balance_sheet_identity"
    assets = _annual(norm, "total_assets", fy)
    liabilities = _annual(norm, "total_liabilities", fy)
    equity = _annual(norm, "total_equity_incl_nci", fy)
    temp = _annual(norm, "temporary_equity", fy)
    if assets is None:
        return CheckResult(name=name, status="warn", detail=f"No total assets for FY{fy}.")
    if liabilities is None or equity is None:
        total = _annual(norm, "liabilities_and_equity", fy)
        missing = "Liabilities" if liabilities is None else "equity"
        if total is None:
            return CheckResult(
                name=name,
                status="warn",
                detail=f"Could not check FY{fy}: {missing} not tagged.",
                facts_used=[assets],
            )
        diff = _rel_diff(assets.value, total.value)
        status = "warn" if diff <= BALANCE_TOLERANCE else "fail"
        return CheckResult(
            name=name,
            status=status,
            detail=f"{missing} not tagged separately; Assets {_fmt(assets.value)} vs "
            f"LiabilitiesAndStockholdersEquity {_fmt(total.value)} (diff {diff:.3%}).",
            facts_used=[assets, total],
        )
    parts = [liabilities, equity] + ([temp] if temp else [])
    rhs = sum(p.value for p in parts)
    diff = _rel_diff(assets.value, rhs)
    detail = (
        f"FY{fy}: Assets {_fmt(assets.value)} vs Liabilities + Equity"
        f"{' + Temporary equity' if temp else ''} {_fmt(rhs)} "
        f"(diff {diff:.3%}, tolerance {BALANCE_TOLERANCE:.1%})."
    )
    if diff <= BALANCE_TOLERANCE:
        return CheckResult(name=name, status="pass", detail=detail, facts_used=[assets, *parts])
    # The components don't add up. If the filing's own total still equals Assets, the gap
    # is a component (e.g. mezzanine equity) tagged with a dimension or company extension,
    # which companyfacts doesn't expose: warn, don't fail.
    total = _annual(norm, "liabilities_and_equity", fy)
    if total is not None and _rel_diff(assets.value, total.value) <= BALANCE_TOLERANCE:
        return CheckResult(
            name=name,
            status="warn",
            detail=detail
            + f" The reported LiabilitiesAndStockholdersEquity {_fmt(total.value)} does equal "
            f"Assets, so {_fmt(total.value - rhs)} sits in a component companyfacts doesn't "
            "expose (e.g. mezzanine/preferred equity). Check the balance sheet before using "
            "equity values.",
            facts_used=[assets, *parts, total],
        )
    return CheckResult(name=name, status="fail", detail=detail, facts_used=[assets, *parts])


def check_quarters_sum(norm: NormalizedFacts, fy: int, metric: str) -> CheckResult:
    """Quarters reconcile to the annual figure.

    Q4 is usually derived as FY - (Q1+Q2+Q3), which sums by construction, so the check
    also cross-checks reported discrete quarters against reported YTD totals (Q1+Q2 vs H1,
    Q1+Q2+Q3 vs 9M), any reported Q4, and the plausibility of a derived Q4.
    """
    name = f"quarters_sum_to_annual:{metric}"
    facts, _ = select_metric(norm, metric, fiscal_years=[fy], period="all")
    by = {f.fiscal_period: f for f in facts}
    if "FY" not in by:
        return CheckResult(name=name, status="warn", detail=f"No FY{fy} {metric} reported.")
    missing = [q for q in ("Q1", "Q2", "Q3", "Q4") if q not in by]
    if missing:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"Could not check FY{fy} {metric}: missing quarters {missing}.",
            facts_used=list(by.values()),
        )
    fy_fact = by["FY"]
    quarters = [by[q] for q in ("Q1", "Q2", "Q3", "Q4")]
    used: list[Fact] = [fy_fact, *quarters]
    problems: list[str] = []

    total = sum(q.value for q in quarters)
    if _rel_diff(total, fy_fact.value) > SUM_TOLERANCE:
        problems.append(f"Q1..Q4 sum {_fmt(total)} != FY {_fmt(fy_fact.value)}")

    # Reported YTD totals vs reported discrete quarters (independent filings).
    ytd = {
        f.fiscal_period: f
        for f in norm.facts
        if f.concept == fy_fact.concept
        and f.fiscal_year == fy
        and f.fiscal_period in ("H1", "9M")
        and f.unit == fy_fact.unit
    }
    for label, parts in (("H1", ("Q1", "Q2")), ("9M", ("Q1", "Q2", "Q3"))):
        y = ytd.get(label)
        qs = [by[p] for p in parts]
        if y is None or any(q.derived for q in qs):
            continue
        s = sum(q.value for q in qs)
        used.append(y)
        if _rel_diff(s, y.value) > SUM_TOLERANCE:
            problems.append(
                f"{'+'.join(parts)} = {_fmt(s)} but reported {label} YTD is {_fmt(y.value)}"
            )
    for w in norm.warnings:
        if w.code == "q4_mismatch" and w.concept == fy_fact.concept and f"FY{fy} " in w.message:
            problems.append(w.message)

    q4 = by["Q4"]
    note = ""
    if q4.derived:
        note = " (Q4 derived as FY − (Q1+Q2+Q3))"
        if metric == "revenue" and q4.value < 0:
            problems.append(f"derived Q4 revenue is negative ({_fmt(q4.value)})")
        biggest = max(abs(q.value) for q in quarters[:3])
        if biggest and abs(q4.value) > 3 * biggest:
            return CheckResult(
                name=name,
                status="warn" if not problems else "fail",
                detail=f"FY{fy} {metric}: derived Q4 {_fmt(q4.value)} is over 3x the largest "
                f"other quarter ({_fmt(biggest)}); check for YTD/period errors. "
                + "; ".join(problems),
                facts_used=used,
            )
    if problems:
        return CheckResult(
            name=name,
            status="fail",
            detail=f"FY{fy} {metric}: " + "; ".join(problems),
            facts_used=used,
        )
    return CheckResult(
        name=name,
        status="pass",
        detail=f"FY{fy} {metric}: quarters reconcile to FY {_fmt(fy_fact.value)}{note}; "
        "reported YTD totals agree with reported quarters.",
        facts_used=used,
    )


def check_gross_profit(norm: NormalizedFacts, fy: int) -> CheckResult:
    name = "gross_profit_identity"
    rev = _annual(norm, "revenue", fy)
    cogs = _annual(norm, "cost_of_revenue", fy)
    gp = _annual(norm, "gross_profit", fy)
    present = [f for f in (rev, cogs, gp) if f is not None]
    if rev is None or cogs is None or gp is None:
        return CheckResult(
            name=name,
            status="warn",
            detail=f"Skipped for FY{fy}: needs revenue, cost of revenue and gross profit; "
            f"found {[f.concept for f in present]}.",
            facts_used=present,
        )
    expected = rev.value - cogs.value
    diff = abs(gp.value - expected) / max(abs(rev.value), 1.0)
    status = "pass" if diff <= SUM_TOLERANCE else "fail"
    return CheckResult(
        name=name,
        status=status,
        detail=f"FY{fy}: GrossProfit {_fmt(gp.value)} vs Revenue − COGS {_fmt(expected)} "
        f"(diff {diff:.3%} of revenue).",
        facts_used=[rev, cogs, gp],
    )


def check_sign_and_range(norm: NormalizedFacts, fy: int) -> CheckResult:
    name = "sign_and_range"
    rev = _annual(norm, "revenue", fy)
    if rev is None:
        return CheckResult(name=name, status="warn", detail=f"No FY{fy} revenue.")
    used = [rev]
    fails: list[str] = []
    warns: list[str] = []
    if rev.value < 0:
        fails.append(f"revenue is negative ({_fmt(rev.value)})")

    def bounded(label: str, num: Fact | None, den: Fact | None, lo: float, hi: float) -> None:
        if num is None or den is None or den.value <= 0:
            return
        used.extend([num, den])
        ratio = num.value / den.value
        if not lo <= ratio <= hi:
            warns.append(f"{label} {ratio:.1%} outside plausible [{lo:.0%}, {hi:.0%}]")

    bounded(
        "effective tax rate",
        _annual(norm, "income_tax", fy),
        _annual(norm, "pretax_income", fy),
        *TAX_RATE_BOUNDS,
    )
    bounded("gross margin", _annual(norm, "gross_profit", fy), rev, *GROSS_MARGIN_BOUNDS)
    bounded("net margin", _annual(norm, "net_income", fy), rev, *NET_MARGIN_BOUNDS)
    used = list({id(f): f for f in used}.values())
    if fails:
        return CheckResult(
            name=name, status="fail", detail="; ".join(fails + warns), facts_used=used
        )
    if warns:
        return CheckResult(name=name, status="warn", detail="; ".join(warns), facts_used=used)
    return CheckResult(
        name=name,
        status="pass",
        detail=f"FY{fy}: revenue ≥ 0; tax rate and margins within plausible bounds.",
        facts_used=used,
    )


def check_yoy(norm: NormalizedFacts, fy: int) -> CheckResult:
    name = "yoy_change"
    used: list[Fact] = []
    flagged: list[str] = []
    compared: list[str] = []
    for metric in ("revenue", "net_income", "total_assets"):
        cur, prev = _annual(norm, metric, fy), _annual(norm, metric, fy - 1)
        if cur is None or prev is None or prev.value == 0:
            continue
        used.extend([prev, cur])
        change = (cur.value - prev.value) / abs(prev.value)
        compared.append(f"{metric} {change:+.1%}")
        if abs(change) > YOY_WARN:
            flagged.append(
                f"{metric} changed {change:+.1%} (FY{fy - 1} {_fmt(prev.value)} → "
                f"FY{fy} {_fmt(cur.value)})"
            )
    if not compared:
        return CheckResult(name=name, status="warn", detail=f"No FY{fy - 1} data to compare.")
    if flagged:
        return CheckResult(
            name=name,
            status="warn",
            detail="Large YoY change (>50%), verify before use: " + "; ".join(flagged),
            facts_used=used,
        )
    return CheckResult(
        name=name,
        status="pass",
        detail="FY{} YoY: {}".format(fy, ", ".join(compared)),
        facts_used=used,
    )


def check_formats(cik: str, facts: Iterable[Fact]) -> CheckResult:
    name = "format_checks"
    problems: list[str] = []
    if not CIK_RE.match(cik):
        problems.append(f"CIK {cik!r} is not 10 digits")
    seen: set[tuple[str, str]] = set()
    checked: list[Fact] = []
    for f in facts:
        p = f.provenance
        key = (p.accession_number, p.form)
        if key in seen:
            continue
        seen.add(key)
        checked.append(f)
        if not ACCESSION_RE.match(p.accession_number):
            problems.append(f"bad accession number {p.accession_number!r}")
        if p.form not in KNOWN_FORMS:
            problems.append(f"unexpected form type {p.form!r} ({p.accession_number})")
    if problems:
        return CheckResult(name=name, status="fail", detail="; ".join(problems), facts_used=checked)
    return CheckResult(
        name=name,
        status="pass",
        detail=f"CIK {cik} and {len(seen)} accession/form pairs are well-formed.",
        facts_used=checked,
    )


# ---- runners ----------------------------------------------------------------------------------


def run_checks(norm: NormalizedFacts, fiscal_year: int, cik: str) -> list[CheckResult]:
    results = [
        check_balance_sheet(norm, fiscal_year),
        *(check_quarters_sum(norm, fiscal_year, m) for m in QUARTER_SUM_METRICS),
        check_gross_profit(norm, fiscal_year),
        check_sign_and_range(norm, fiscal_year),
        check_yoy(norm, fiscal_year),
    ]
    used = [f for r in results for f in r.facts_used]
    results.append(check_formats(cik, used))
    return results


async def run_all(
    ticker: str, fiscal_year: int, client: EdgarClient | None = None
) -> list[CheckResult]:
    """Fetch a company's facts and run every check for one fiscal year."""
    own = client is None
    c = client or EdgarClient()
    try:
        cik = await c.ticker_to_cik(ticker)
        raw = await c.get_company_facts(cik)
        norm = normalize_company_facts(raw, as_of=c.as_of(c.company_facts_url(cik)))
        return run_checks(norm, fiscal_year, cik)
    finally:
        if own:
            await c.aclose()


def summarize(results: list[CheckResult]) -> dict[str, int]:
    out = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        out[r.status] += 1
    return out
