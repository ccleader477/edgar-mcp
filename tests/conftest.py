from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from edgar_mcp.edgar import COMPANYFACTS_URL, SUBMISSIONS_URL, TICKERS_URL, EdgarClient

pytest_plugins = ["tests.mcp_session"]

FIXTURES = Path(__file__).parent / "fixtures"
CIKS = {"AAPL": "0000320193", "MSFT": "0000789019", "CELH": "0001341766"}
AS_OF = datetime(2026, 11, 16, tzinfo=UTC)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def aapl_10k_html() -> str:
    return gzip.decompress((FIXTURES / "aapl_10k.htm.gz").read_bytes()).decode("utf-8")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Test Runner test@example.com")


@pytest.fixture(autouse=True)
def _no_network() -> Iterator[respx.MockRouter]:
    """Any HTTP request a test didn't explicitly mock fails (CLAUDE.md rule 2)."""
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def router(_no_network: respx.MockRouter) -> respx.MockRouter:
    return _no_network


@pytest.fixture
def sec_mock(_no_network: respx.MockRouter) -> respx.MockRouter:
    """Mock the SEC endpoints with saved fixtures."""
    router = _no_network
    router.get(TICKERS_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("company_tickers.json"))
    )
    for ticker, cik in CIKS.items():
        router.get(SUBMISSIONS_URL.format(cik=cik)).mock(
            return_value=httpx.Response(200, json=load_fixture(f"submissions_{ticker}.json"))
        )
        router.get(COMPANYFACTS_URL.format(cik=cik)).mock(
            return_value=httpx.Response(200, json=load_fixture(f"companyfacts_{ticker}.json"))
        )
    meta = load_fixture("aapl_10k.meta.json")
    router.get(meta["primary_doc_url"]).mock(
        return_value=httpx.Response(200, content=aapl_10k_html().encode("utf-8"))
    )
    return router


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[EdgarClient]:
    async with EdgarClient(cache_dir=tmp_path / "cache", sleep=_noop_sleep) as c:
        yield c
