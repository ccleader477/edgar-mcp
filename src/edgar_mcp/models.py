"""Pydantic models shared by the data layer, quality controls, validation and server."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class PeriodType(StrEnum):
    DURATION = "duration"  # has start + end (revenue, net income, cash flow)
    INSTANT = "instant"  # end only (assets, cash)


class Provenance(BaseModel):
    """Where a number came from. Every financial value carries one (CLAUDE.md rule 3)."""

    accession_number: str
    form: str
    filed_date: date
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    source_url: str


class Fact(BaseModel):
    """One normalized XBRL fact, after dedup and period assignment."""

    concept: str
    taxonomy: str = "us-gaap"
    label: str | None = None
    value: float
    unit: str
    period_type: PeriodType
    period_start: date | None
    period_end: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None  # FY, Q1..Q4, H1, 9M, or None for instants off a FYE
    provenance: Provenance
    restated: bool = False
    prior_value: float | None = None
    amended: bool = False
    derived: bool = False
    derivation: str | None = None
    derived_from: list[Provenance] = Field(default_factory=list)
    as_of: datetime


class Filing(BaseModel):
    cik: str
    form: str
    accession_number: str
    filed_date: date
    report_date: date | None
    primary_document: str
    primary_doc_url: str
    description: str | None = None


class QualityWarning(BaseModel):
    code: str
    message: str
    concept: str | None = None


CheckStatus = Literal["pass", "warn", "fail"]


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    detail: str
    facts_used: list[Fact] = Field(default_factory=list)
