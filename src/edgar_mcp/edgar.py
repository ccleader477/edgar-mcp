"""EDGAR HTTP client: User-Agent enforcement, rate limiting, retries, on-disk cache.

SEC etiquette (CLAUDE.md rule 1): every request sends the User-Agent from the
``SEC_USER_AGENT`` env var, we never exceed 8 requests/second, and responses are
cached on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx

from edgar_mcp.models import Filing

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache"

# Cache TTLs (seconds). Filing documents are immutable once filed.
TTL_TICKERS = 24 * 3600
TTL_SUBMISSIONS = 3600
TTL_FACTS = 6 * 3600
TTL_DOCUMENT = 365 * 24 * 3600

RETRY_STATUSES = {429, 500, 502, 503, 504}


class EdgarError(Exception):
    """Base class for recoverable EDGAR errors (bad ticker, missing data, HTTP failure)."""


class MissingUserAgentError(EdgarError):
    pass


class UnknownTickerError(EdgarError):
    pass


class NotFoundError(EdgarError):
    pass


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: sets vars that are not already set. Never logs values."""
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def get_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise MissingUserAgentError(
            "SEC_USER_AGENT is not set. SEC requires a contact User-Agent like "
            "'Jane Doe jane@example.com'. Set it in .env or the environment."
        )
    if "@" not in ua or " " not in ua:
        raise MissingUserAgentError(
            "SEC_USER_AGENT must look like 'Name email@example.com' (got a value without "
            "a name and an email address)."
        )
    return ua


def pad_cik(cik: str | int) -> str:
    digits = str(cik).strip()
    if digits.upper().startswith("CIK"):
        digits = digits[3:]
    if not digits.isdigit() or len(digits) > 10:
        raise EdgarError(f"Invalid CIK {cik!r}: expected up to 10 digits.")
    return digits.zfill(10)


def accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


class RateLimiter:
    """Spaces requests so at most ``rate`` start per second (across all callers)."""

    def __init__(self, rate: float = 8.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = 1.0 / rate
        self._clock = clock
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self.interval


class DiskCache:
    """One JSON file per URL: {"url", "fetched_at", "body"}."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, url: str) -> Path:
        return self.root / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, url: str, ttl: float) -> tuple[str, datetime] | None:
        path = self._path(url)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None
        age = (datetime.now(UTC) - fetched_at).total_seconds()
        if age > ttl:
            return None
        return str(entry["body"]), fetched_at

    def put(self, url: str, body: str, fetched_at: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(url).with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"url": url, "fetched_at": fetched_at.isoformat(), "body": body}),
            encoding="utf-8",
        )
        tmp.replace(self._path(url))


class EdgarClient:
    """Async EDGAR client. Use as ``async with EdgarClient() as client:``."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        rate: float = 8.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        timeout: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = get_user_agent()  # fail fast before any request
        if rate > 8.0:
            raise ValueError("SEC fair-access policy: rate must be <= 8 requests/second.")
        self.cache = DiskCache(cache_dir or DEFAULT_CACHE_DIR)
        self.limiter = RateLimiter(rate)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self.request_count = 0
        self._as_of: dict[str, datetime] = {}
        self._tickers: dict[str, dict[str, Any]] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def as_of(self, url: str) -> datetime:
        """When the cached/fetched body for ``url`` was retrieved from SEC."""
        return self._as_of.get(url, datetime.now(UTC))

    async def get_text(self, url: str, ttl: float) -> str:
        cached = self.cache.get(url, ttl)
        if cached is not None:
            body, fetched_at = cached
            self._as_of[url] = fetched_at
            return body
        body = await self._fetch(url)
        fetched_at = datetime.now(UTC)
        self.cache.put(url, body, fetched_at)
        self._as_of[url] = fetched_at
        return body

    async def get_json(self, url: str, ttl: float) -> Any:
        return json.loads(await self.get_text(url, ttl))

    async def _fetch(self, url: str) -> str:
        last_error = ""
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire()
            self.request_count += 1
            try:
                resp = await self._http.get(url)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc.__class__.__name__}"
            else:
                if resp.status_code == 200:
                    return decode_body(resp.content)
                if resp.status_code == 404:
                    raise NotFoundError(f"SEC returned 404 for {url}")
                if resp.status_code not in RETRY_STATUSES:
                    raise EdgarError(f"SEC returned HTTP {resp.status_code} for {url}")
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After", "")
                if attempt < self.max_retries and retry_after.isdigit():
                    await self._sleep(min(float(retry_after), 60.0))
                    continue
            if attempt < self.max_retries:
                await self._sleep(self.backoff_base * (2**attempt))
        raise EdgarError(f"Giving up on {url} after {self.max_retries + 1} attempts ({last_error})")

    # ---- endpoints -------------------------------------------------------------------

    async def _ticker_map(self) -> dict[str, dict[str, Any]]:
        if self._tickers is None:
            raw = await self.get_json(TICKERS_URL, TTL_TICKERS)
            self._tickers = {str(row["ticker"]).upper(): row for row in raw.values()}
        return self._tickers

    async def ticker_to_cik(self, ticker: str) -> str:
        key = ticker.strip().upper()
        if not key:
            raise UnknownTickerError("Ticker is empty.")
        row = (await self._ticker_map()).get(key)
        if row is None:
            raise UnknownTickerError(
                f"Unknown ticker {ticker!r}: not in SEC's company_tickers.json. "
                "Check the symbol (e.g. 'AAPL', 'MSFT') or pass a CIK."
            )
        return pad_cik(row["cik_str"])

    async def company_name(self, cik: str) -> str | None:
        target = int(pad_cik(cik))
        for row in (await self._ticker_map()).values():
            if int(row["cik_str"]) == target:
                return str(row["title"])
        return None

    async def get_submissions(self, cik: str) -> list[Filing]:
        cik10 = pad_cik(cik)
        data = await self.get_json(SUBMISSIONS_URL.format(cik=cik10), TTL_SUBMISSIONS)
        return parse_submissions(data, cik10)

    async def get_company_facts(self, cik: str) -> dict[str, Any]:
        url = COMPANYFACTS_URL.format(cik=pad_cik(cik))
        try:
            data: dict[str, Any] = await self.get_json(url, TTL_FACTS)
        except NotFoundError as exc:
            raise NotFoundError(f"No XBRL company facts for CIK {pad_cik(cik)}.") from exc
        return data

    def company_facts_url(self, cik: str) -> str:
        return COMPANYFACTS_URL.format(cik=pad_cik(cik))

    async def find_filing(self, cik: str, accession: str) -> Filing:
        for filing in await self.get_submissions(cik):
            if filing.accession_number == accession:
                return filing
        raise NotFoundError(
            f"Accession {accession} not found in recent filings for CIK {pad_cik(cik)}."
        )

    async def get_filing_document(self, accession: str, cik: str) -> str:
        filing = await self.find_filing(cik, accession)
        return await self.get_text(filing.primary_doc_url, TTL_DOCUMENT)


def decode_body(content: bytes) -> str:
    """SEC documents are mostly UTF-8 but some are Windows-1252 without a charset header."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def archive_url(cik: str, accession: str, doc: str) -> str:
    return ARCHIVES_URL.format(
        cik_int=int(pad_cik(cik)), acc_nodash=accession_nodash(accession), doc=doc
    )


def parse_submissions(data: dict[str, Any], cik10: str) -> list[Filing]:
    recent = data.get("filings", {}).get("recent", {})
    forms: list[str] = recent.get("form", [])
    filings: list[Filing] = []
    for i, form in enumerate(forms):
        accession = recent["accessionNumber"][i]
        primary = recent["primaryDocument"][i] or ""
        report = recent.get("reportDate", [""] * len(forms))[i]
        desc = recent.get("primaryDocDescription", [""] * len(forms))[i]
        filings.append(
            Filing(
                cik=cik10,
                form=form,
                accession_number=accession,
                filed_date=date.fromisoformat(recent["filingDate"][i]),
                report_date=date.fromisoformat(report) if report else None,
                primary_document=primary,
                primary_doc_url=archive_url(cik10, accession, primary),
                description=desc or None,
            )
        )
    return filings


# ---- 10-K section extraction ------------------------------------------------------------

_SECTION_END = {
    # Anchored to heading titles so inline references ("see Item 8 of this Form 10-K")
    # don't end a section early.
    "1A": r"item\s*1b\b\.?\s*\W?\s*unresolved|item\s*1c\b\.?\s*\W?\s*cybersecurity"
    r"|item\s*2\b\.?\s*\W?\s*properties",
    "7": r"item\s*7a\b\.?\s*\W?\s*quantitative|item\s*8\b\.?\s*\W?\s*financial\s+statements",
}
_SECTION_START = {
    "1A": r"item\s*1a\b\.?\s*[:.\-–—]?\s*risk\s+factors",
    "7": r"item\s*7\b\.?\s*[:.\-–—]?\s*management\W{0,3}s?\s+discussion",
}


def html_to_text(doc: str) -> str:
    text = re.sub(r"(?is)<(script|style|ix:header)\b.*?</\1>", " ", doc)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d|table)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_section(doc: str, item: str) -> str:
    """Return the body of 10-K Item 1A (Risk Factors) or Item 7 (MD&A).

    Picks the longest start..end span so the table-of-contents entry is skipped.
    Raises NotFoundError when the section can't be located.
    """
    key = item.strip().upper().removeprefix("ITEM").strip()
    if key not in _SECTION_START:
        raise EdgarError(f"Unsupported item {item!r}: supported items are 1A and 7.")
    text = html_to_text(doc) if "<" in doc[:5000] else doc
    flat = text.lower()
    end_re = re.compile(_SECTION_END[key])
    best = ""
    for start_m in re.finditer(_SECTION_START[key], flat):
        s = start_m.start()
        m = end_re.search(flat, start_m.end())
        if m is None:
            continue  # an unterminated span would swallow the rest of the filing
        if m.start() - s > len(best):
            best = text[s : m.start()]
    if len(best) < 500:
        raise NotFoundError(f"Could not locate Item {key} in this document.")
    return best.strip()
