"""Fetch EDGAR snapshots once and save them as test fixtures (CLAUDE.md rule 2).

Usage: uv run python scripts/fetch_fixtures.py

Companyfacts files are trimmed to the concepts the project uses (see quality.METRICS) so the
fixtures stay small; every kept row is byte-for-byte what SEC returned.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

from edgar_mcp.edgar import TICKERS_URL, EdgarClient, load_dotenv
from edgar_mcp.quality import ALL_METRIC_CONCEPTS, split_concept

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
# AAPL: Sept fiscal year. MSFT: June fiscal year. CELH: filed a 10-K/A restating FY2021/2022.
COMPANIES = {"AAPL": "0000320193", "MSFT": "0000789019", "CELH": "0001341766"}
SUBMISSIONS_KEEP = 300  # most recent filings per company


def trim_facts(raw: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, dict[str, Any]] = {}
    for name in ALL_METRIC_CONCEPTS:
        taxonomy, concept = split_concept(name)
        body = raw["facts"].get(taxonomy, {}).get(concept)
        if body is not None:
            keep.setdefault(taxonomy, {})[concept] = body
    return {"cik": raw["cik"], "entityName": raw["entityName"], "facts": keep}


def trim_submissions(raw: dict[str, Any]) -> dict[str, Any]:
    recent = raw["filings"]["recent"]
    trimmed = {k: v[:SUBMISSIONS_KEEP] for k, v in recent.items()}
    out = {k: raw[k] for k in ("cik", "name", "tickers", "fiscalYearEnd") if k in raw}
    out["filings"] = {"recent": trimmed}
    return out


def dump(name: str, data: Any) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {path.name} ({path.stat().st_size / 1024:.0f} KB)")


async def main() -> None:
    load_dotenv()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    async with EdgarClient() as client:
        tickers = await client.get_json(TICKERS_URL, 0)
        wanted = set(COMPANIES)
        dump(
            "company_tickers.json",
            {k: v for k, v in tickers.items() if v["ticker"] in wanted},
        )
        for ticker, cik in COMPANIES.items():
            dump(f"companyfacts_{ticker}.json", trim_facts(await client.get_company_facts(cik)))
            subs_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            dump(f"submissions_{ticker}.json", trim_submissions(await client.get_json(subs_url, 0)))

        filings = await client.get_submissions(COMPANIES["AAPL"])
        tenk = next(f for f in filings if f.form == "10-K")
        doc = await client.get_filing_document(tenk.accession_number, COMPANIES["AAPL"])
        path = FIXTURES / "aapl_10k.htm.gz"
        path.write_bytes(gzip.compress(doc.encode("utf-8")))
        (FIXTURES / "aapl_10k.meta.json").write_text(
            json.dumps(tenk.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        print(
            f"wrote {path.name} ({path.stat().st_size / 1024:.0f} KB) for {tenk.accession_number}"
        )
        print(f"{client.request_count} SEC requests")


if __name__ == "__main__":
    asyncio.run(main())
