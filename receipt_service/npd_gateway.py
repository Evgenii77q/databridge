from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .utils import utcnow


@dataclass
class IncomeSyncInput:
    operation_key: str
    payment_id: str
    operation_time: str
    amount_kopecks: int
    currency: str
    description: str | None
    customer: dict[str, Any]
    items: list[dict[str, Any]]
    metadata: dict[str, Any]
    source_payload: dict[str, Any]


@dataclass
class RefundSyncInput:
    operation_key: str
    refund_id: str
    payment_id: str
    operation_time: str
    amount_kopecks: int
    currency: str
    original_income_operation_id: str | None
    original_income_amount_kopecks: int | None
    is_partial: bool
    source_payload: dict[str, Any]


@dataclass
class GatewayResult:
    ok: bool
    provider_operation_id: str | None
    payload: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


class NPDGateway(Protocol):
    def register_income(self, data: IncomeSyncInput) -> GatewayResult:
        ...

    def cancel_income(self, data: RefundSyncInput) -> GatewayResult:
        ...


class MockNPDGateway:
    def register_income(self, data: IncomeSyncInput) -> GatewayResult:
        operation_id = f"MOCK-INCOME-{data.payment_id}"
        payload = {
            "provider": "mock",
            "status": "created",
            "operation_id": operation_id,
            "operation_key": data.operation_key,
            "received_at": utcnow().isoformat(),
        }
        return GatewayResult(ok=True, provider_operation_id=operation_id, payload=payload)

    def cancel_income(self, data: RefundSyncInput) -> GatewayResult:
        operation_id = f"MOCK-REFUND-{data.refund_id}"
        payload = {
            "provider": "mock",
            "status": "deleted",
            "operation_id": operation_id,
            "operation_key": data.operation_key,
            "is_partial": data.is_partial,
            "received_at": utcnow().isoformat(),
        }
        return GatewayResult(ok=True, provider_operation_id=operation_id, payload=payload)


class HttpNPDGateway:
    def __init__(self, *, base_url: str, token: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def register_income(self, data: IncomeSyncInput) -> GatewayResult:
        body = {
            "operation_key": data.operation_key,
            "payment_id": data.payment_id,
            "operation_time": data.operation_time,
            "amount": _from_kopecks(data.amount_kopecks),
            "currency": data.currency,
            "description": data.description,
            "customer": data.customer,
            "items": data.items,
            "metadata": data.metadata,
            "source_payload": data.source_payload,
        }
        return self._post("/income", body, operation_key=data.operation_key)

    def cancel_income(self, data: RefundSyncInput) -> GatewayResult:
        body = {
            "operation_key": data.operation_key,
            "refund_id": data.refund_id,
            "payment_id": data.payment_id,
            "operation_time": data.operation_time,
            "amount": _from_kopecks(data.amount_kopecks),
            "currency": data.currency,
            "is_partial": data.is_partial,
            "original_income_amount": _from_kopecks(data.original_income_amount_kopecks)
            if data.original_income_amount_kopecks is not None
            else None,
            "original_income_operation_id": data.original_income_operation_id,
            "reason": "refund",
            "source_payload": data.source_payload,
        }
        return self._post("/income/cancel", body, operation_key=data.operation_key)

    def _post(self, path: str, body: dict[str, Any], *, operation_key: str) -> GatewayResult:
        if not self._base_url:
            return GatewayResult(
                ok=False,
                provider_operation_id=None,
                payload={"error": "NPD_SYNC_API_URL is required"},
                error_code="CONFIG_ERROR",
                error_message="NPD_SYNC_API_URL is required",
                retryable=False,
            )

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": operation_key,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return self._map_success(payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="ignore")
            payload = _try_json(raw)
            if exc.code in {409, 422}:
                return self._map_duplicate(payload)
            return GatewayResult(
                ok=False,
                provider_operation_id=None,
                payload=payload,
                error_code=str(exc.code),
                error_message=raw or f"HTTP {exc.code}",
                retryable=exc.code >= 500,
            )
        except urllib.error.URLError as exc:
            return GatewayResult(
                ok=False,
                provider_operation_id=None,
                payload={"error": str(exc.reason)},
                error_code="NETWORK",
                error_message=str(exc.reason),
                retryable=True,
            )
        except json.JSONDecodeError as exc:
            return GatewayResult(
                ok=False,
                provider_operation_id=None,
                payload={"error": "Invalid JSON"},
                error_code="BAD_JSON",
                error_message=str(exc),
                retryable=False,
            )

    @staticmethod
    def _map_success(payload: dict[str, Any]) -> GatewayResult:
        status = str(payload.get("status") or "ok").lower()
        provider_operation_id = _as_optional_str(payload.get("operation_id") or payload.get("id"))

        if status in {"ok", "created", "deleted", "synced", "duplicate", "already_deleted"}:
            return GatewayResult(ok=True, provider_operation_id=provider_operation_id, payload=payload)

        if status in {"pending", "in_progress"}:
            return GatewayResult(
                ok=False,
                provider_operation_id=provider_operation_id,
                payload=payload,
                error_code="PENDING",
                error_message="Operation is still processing",
                retryable=True,
            )

        return GatewayResult(
            ok=False,
            provider_operation_id=provider_operation_id,
            payload=payload,
            error_code=_as_optional_str(payload.get("code")) or "UNKNOWN",
            error_message=_as_optional_str(payload.get("message")) or "Unknown provider error",
            retryable=False,
        )

    @staticmethod
    def _map_duplicate(payload: dict[str, Any]) -> GatewayResult:
        provider_operation_id = _as_optional_str(payload.get("operation_id") or payload.get("id"))
        return GatewayResult(
            ok=True,
            provider_operation_id=provider_operation_id,
            payload={"status": "duplicate", **payload},
        )


def build_npd_gateway(*, provider_name: str, base_url: str, token: str, timeout_seconds: float) -> NPDGateway:
    name = provider_name.strip().lower()
    if name == "mock":
        return MockNPDGateway()
    if name in {"http", "partner_api"}:
        if not base_url.strip():
            raise ValueError("NPD_SYNC_API_URL is required when NPD_SYNC_PROVIDER=http")
        return HttpNPDGateway(base_url=base_url, token=token, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unknown NPD sync provider '{provider_name}'")


def _from_kopecks(value: int | None) -> str | None:
    if value is None:
        return None
    return str((Decimal(value) / Decimal(100)).quantize(Decimal("0.01")))


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _try_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}
