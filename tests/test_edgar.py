from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from edgar_mcp.edgar import (
    COMPANYFACTS_URL,
    TICKERS_URL,
    EdgarClient,
    EdgarError,
    MissingUserAgentError,
    NotFoundError,
    RateLimiter,
    UnknownTickerError,
    decode_body,
    extract_section,
    pad_cik,
)
from tests.conftest import aapl_10k_html, load_fixture

FACTS_URL = COMPANYFACTS_URL.format(cik="0000320193")


def test_missing_user_agent_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(MissingUserAgentError, match="SEC_USER_AGENT is not set"):
        EdgarClient()


def test_user_agent_must_have_name_and_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "just-a-name")
    with pytest.raises(MissingUserAgentError):
        EdgarClient()


def test_rate_above_sec_limit_rejected() -> None:
    with pytest.raises(ValueError, match="8 requests/second"):
        EdgarClient(rate=10)


async def test_user_agent_header_sent(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    await client.get_company_facts("320193")
    request = sec_mock.calls.last.request
    assert request.headers["User-Agent"] == "Test Runner test@example.com"


def test_pad_cik() -> None:
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("CIK0000789019") == "0000789019"
    with pytest.raises(EdgarError):
        pad_cik("12ab")


async def test_ticker_to_cik(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    assert await client.ticker_to_cik("aapl") == "0000320193"
    assert await client.ticker_to_cik("MSFT") == "0000789019"


async def test_unknown_ticker(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    with pytest.raises(UnknownTickerError, match="Unknown ticker 'ZZZZ'"):
        await client.ticker_to_cik("ZZZZ")


async def test_cache_hit_avoids_second_request(
    client: EdgarClient, sec_mock: respx.MockRouter
) -> None:
    first = await client.get_company_facts("0000320193")
    second = await client.get_company_facts("0000320193")
    assert first == second
    assert client.request_count == 1


async def test_cache_shared_across_clients(tmp_path: Path, sec_mock: respx.MockRouter) -> None:
    async with EdgarClient(cache_dir=tmp_path) as c1:
        await c1.get_company_facts("320193")
    async with EdgarClient(cache_dir=tmp_path) as c2:
        await c2.get_company_facts("320193")
        assert c2.request_count == 0


async def test_retries_on_server_error(client: EdgarClient, router: respx.MockRouter) -> None:
    route = router.get(FACTS_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"cik": 320193, "facts": {}}),
        ]
    )
    data = await client.get_company_facts("320193")
    assert data["cik"] == 320193
    assert route.call_count == 3


async def test_429_honors_retry_after(tmp_path: Path, router: respx.MockRouter) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    router.get(FACTS_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with EdgarClient(cache_dir=tmp_path, sleep=fake_sleep) as c:
        assert await c.get_company_facts("320193") == {"ok": True}
    assert 7.0 in slept


async def test_gives_up_after_max_retries(client: EdgarClient, router: respx.MockRouter) -> None:
    route = router.get(FACTS_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(EdgarError, match="after 4 attempts"):
        await client.get_company_facts("320193")
    assert route.call_count == 4


async def test_404_is_not_found_without_retry(
    client: EdgarClient, router: respx.MockRouter
) -> None:
    route = router.get(FACTS_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFoundError, match="No XBRL company facts"):
        await client.get_company_facts("320193")
    assert route.call_count == 1


async def test_rate_limiter_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("edgar_mcp.edgar.asyncio.sleep", fake_sleep)
    limiter = RateLimiter(rate=8.0, clock=lambda: now[0])
    for _ in range(9):
        await limiter.acquire()
    # 9 acquisitions at 8/s need at least 1 second of spacing in total.
    assert sum(sleeps) == pytest.approx(1.0)


async def test_get_submissions(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    filings = await client.get_submissions("320193")
    tenk = next(f for f in filings if f.form == "10-K")
    assert tenk.cik == "0000320193"
    assert tenk.primary_doc_url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    assert tenk.accession_number.replace("-", "") in tenk.primary_doc_url


async def test_get_filing_document_and_sections(
    client: EdgarClient, sec_mock: respx.MockRouter
) -> None:
    meta = load_fixture("aapl_10k.meta.json")
    doc = await client.get_filing_document(meta["accession_number"], "320193")
    risk = extract_section(doc, "1A")
    mdna = extract_section(doc, "Item 7")
    assert risk.startswith("Item 1A")
    assert "Risk Factors" in risk[:40]
    assert mdna.startswith("Item 7")
    assert "Discussion and Analysis" in mdna[:80]
    # Sections end at the next heading, not at the end of the document.
    assert len(risk) < len(doc) / 2
    assert "Unresolved Staff Comments" not in risk[200:]


async def test_unknown_accession(client: EdgarClient, sec_mock: respx.MockRouter) -> None:
    with pytest.raises(NotFoundError):
        await client.get_filing_document("0000000000-00-000000", "320193")


def test_extract_section_rejects_unknown_item() -> None:
    with pytest.raises(EdgarError, match="supported items"):
        extract_section(aapl_10k_html(), "9B")


def test_extract_section_missing() -> None:
    with pytest.raises(NotFoundError):
        extract_section("<html><body>No items here</body></html>", "7")


def test_decode_body_falls_back_to_cp1252() -> None:
    assert decode_body("Management’s".encode("cp1252")) == "Management’s"
    assert decode_body("Management’s".encode()) == "Management’s"


def test_no_live_network_in_tests() -> None:
    with pytest.raises(Exception):  # noqa: B017 - respx raises for unmocked routes
        httpx.get(TICKERS_URL)
