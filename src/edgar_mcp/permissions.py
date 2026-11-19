"""Roles, scopes, allowlists, per-call caps and the JSONL audit log."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import yaml

from edgar_mcp.edgar import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "roles.yaml"
DEFAULT_AUDIT_LOG = PROJECT_ROOT / "audit.log.jsonl"


class PermissionDenied(Exception):
    """Raised when the active role may not make a call. The message is shown to the model."""


@dataclass
class Limits:
    max_filings: int = 50
    max_metrics: int = 15
    max_fiscal_years: int = 10
    max_section_chars: int = 50_000


@dataclass
class Policy:
    roles: dict[str, set[str]]
    tickers: set[str]
    form_types: set[str]
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> Policy:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roles = {name: set(spec.get("tools", [])) for name, spec in data.get("roles", {}).items()}
        allow = data.get("allowlists", {})
        return cls(
            roles=roles,
            tickers={str(t).upper() for t in allow.get("tickers", [])},
            form_types={str(f).upper() for f in allow.get("form_types", [])},
            limits=Limits(**data.get("limits", {})),
        )

    def ticker_allowed(self, ticker: str) -> bool:
        return "*" in self.tickers or ticker.strip().upper() in self.tickers

    def form_allowed(self, form: str) -> bool:
        return "*" in self.form_types or form.strip().upper() in self.form_types


class Guard:
    """Checks every tool call against the active role and allowlists, and audits it."""

    def __init__(
        self,
        policy: Policy | None = None,
        role: str | None = None,
        audit_path: Path | None = None,
    ) -> None:
        self.policy = policy or Policy.load()
        self.role = (role or os.environ.get("EDGAR_MCP_ROLE") or "analyst").strip().lower()
        self.audit_path = audit_path or Path(
            os.environ.get("EDGAR_MCP_AUDIT_LOG", str(DEFAULT_AUDIT_LOG))
        )
        if self.role not in self.policy.roles:
            raise PermissionDenied(
                f"Unknown role {self.role!r}; configured roles: {sorted(self.policy.roles)}."
            )

    def check(self, tool: str, args: dict[str, Any]) -> None:
        allowed = self.policy.roles[self.role]
        if tool not in allowed:
            raise PermissionDenied(
                f"Permission denied: role '{self.role}' may not call {tool}. "
                f"Tools available to this role: {sorted(allowed)}."
            )
        ticker = args.get("ticker")
        if isinstance(ticker, str) and not self.policy.ticker_allowed(ticker):
            raise PermissionDenied(
                f"Permission denied: ticker {ticker.upper()!r} is not on the allowlist "
                "(config/roles.yaml). Ask an administrator to add it."
            )
        forms = args.get("form_types") or []
        bad = [f for f in forms if not self.policy.form_allowed(f)]
        if bad:
            raise PermissionDenied(
                f"Permission denied: form types {bad} are not on the allowlist; "
                f"allowed: {sorted(self.policy.form_types)}."
            )

    def audit(
        self,
        tool: str,
        args: dict[str, Any],
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
        duration_ms: float | None = None,
    ) -> None:
        payload = json.dumps(result, sort_keys=True, default=str) if result is not None else ""
        line = {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": self.role,
            "tool": tool,
            "args": args,
            "status": status,
            "result_sha256": hashlib.sha256(payload.encode()).hexdigest() if payload else None,
            "error": error,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")


P = ParamSpec("P")
R = TypeVar("R")


def guarded(
    tool: str,
    get_guard: Callable[[], Guard],
    to_error: Callable[[str], Exception],
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wrap an async tool: permission check, audit line, and denial -> ``to_error``.

    ``functools.wraps`` keeps the signature so FastMCP still builds the right schema.
    """

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = sig.bind_partial(*args, **kwargs)
            call_args = {k: v for k, v in bound.arguments.items() if v is not None}
            guard = get_guard()
            start = time.perf_counter()
            try:
                guard.check(tool, call_args)
            except PermissionDenied as exc:
                guard.audit(tool, call_args, "denied", error=str(exc))
                raise to_error(str(exc)) from exc
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                guard.audit(
                    tool,
                    call_args,
                    "error",
                    error=str(exc),
                    duration_ms=(time.perf_counter() - start) * 1000,
                )
                raise
            guard.audit(
                tool,
                call_args,
                "ok",
                result=result,
                duration_ms=(time.perf_counter() - start) * 1000,
            )
            return result

        return wrapper

    return decorate
