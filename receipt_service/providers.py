from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .utils import utcnow


@dataclass
class ProviderIssueInput:
    receipt_id: str
    external_id: str | None
    source: str
    amount_kopecks: int
    currency: str
    description: str | None
    customer: dict[str, Any]
    items: list[dict[str, Any]]
    metadata: dict[str, Any]


@dataclass
class ProviderIssueResult:
    status: str
    provider_receipt_id: str | None
    payload: dict[str, Any]


class ProviderError(RuntimeError):
    pass


class FiscalProvider(Protocol):
    def issue_receipt(self, data: ProviderIssueInput) -> ProviderIssueResult:
        ...


class MockProvider:
    def issue_receipt(self, data: ProviderIssueInput) -> ProviderIssueResult:
        issued_at = utcnow().isoformat()
        provider_receipt_id = f"MOCK-{data.receipt_id[:8]}"
        payload = {
            "provider": "mock",
            "receipt_number": provider_receipt_id,
            "issued_at": issued_at,
            "amount_kopecks": data.amount_kopecks,
            "currency": data.currency,
            "description": data.description,
            "customer": data.customer,
            "items": data.items,
            "metadata": data.metadata,
        }
        return ProviderIssueResult(
            status="issued",
            provider_receipt_id=provider_receipt_id,
            payload=payload,
        )


class MoyNalogProvider:
    def __init__(self, api_url: str, token: str, timeout_seconds: float) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def issue_receipt(self, data: ProviderIssueInput) -> ProviderIssueResult:
        if not self._api_url or not self._token:
            raise ProviderError("MOY_NALOG_API_URL and MOY_NALOG_TOKEN are required")

        body = {
            "receipt_id": data.receipt_id,
            "external_id": data.external_id,
            "source": data.source,
            "amount_kopecks": data.amount_kopecks,
            "currency": data.currency,
            "description": data.description,
            "customer": data.customer,
            "items": data.items,
            "metadata": data.metadata,
        }

        request = urllib.request.Request(
            f"{self._api_url}/receipts",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise ProviderError(f"Provider HTTP error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Provider connection error: {exc.reason}") from exc

        provider_receipt_id = payload.get("receipt_id") or payload.get("id")
        status = str(payload.get("status") or "issued").lower()

        if status not in {"issued", "pending", "failed"}:
            status = "issued"

        return ProviderIssueResult(
            status=status,
            provider_receipt_id=provider_receipt_id,
            payload=payload,
        )


def build_provider(
    *,
    provider_name: str,
    moy_nalog_api_url: str,
    moy_nalog_token: str,
    timeout_seconds: float,
) -> FiscalProvider:
    name = provider_name.strip().lower()
    if name == "mock":
        return MockProvider()
    if name in {"moy_nalog", "moynalog"}:
        return MoyNalogProvider(moy_nalog_api_url, moy_nalog_token, timeout_seconds)
    raise ValueError(f"Unknown provider '{provider_name}'")
