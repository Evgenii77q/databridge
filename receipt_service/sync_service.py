from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import ReceiptDB
from .npd_gateway import GatewayResult, IncomeSyncInput, NPDGateway, RefundSyncInput
from .utils import json_dumps, to_kopecks, utcnow
from .yookassa_client import YooKassaClient

PAYMENTS_CURSOR_KEY = "yookassa_payments_created_at"
REFUNDS_CURSOR_KEY = "yookassa_refunds_created_at"


@dataclass
class SyncStats:
    payments_scan_from: str | None = None
    refunds_scan_from: str | None = None
    payments_seen: int = 0
    refunds_seen: int = 0
    operations_added: int = 0
    operations_attempted: int = 0
    operations_synced: int = 0
    operations_failed: int = 0
    operations_deferred: int = 0


class YooKassaNpdSyncService:
    def __init__(
        self,
        *,
        db: ReceiptDB,
        yookassa_client: YooKassaClient,
        gateway: NPDGateway,
        start_at: str,
        max_attempts: int,
        batch_limit: int,
        lookback_seconds: int,
        dry_run: bool,
    ) -> None:
        self._db = db
        self._yookassa_client = yookassa_client
        self._gateway = gateway
        self._start_at = normalize_start_at(start_at)
        self._max_attempts = max_attempts
        self._batch_limit = batch_limit
        self._lookback_seconds = max(0, int(lookback_seconds))
        self._dry_run = dry_run

    def run_once(self) -> SyncStats:
        self._yookassa_client.validate()
        stats = SyncStats()

        payments_scan_from = self._resolve_scan_start(PAYMENTS_CURSOR_KEY)
        stats.payments_scan_from = payments_scan_from
        max_payment_dt = self._ingest_payments(stats, payments_scan_from)
        if max_payment_dt is not None:
            self._advance_cursor(PAYMENTS_CURSOR_KEY, max_payment_dt)

        refunds_scan_from = self._resolve_scan_start(REFUNDS_CURSOR_KEY)
        stats.refunds_scan_from = refunds_scan_from
        max_refund_dt = self._ingest_refunds(stats, refunds_scan_from)
        if max_refund_dt is not None:
            self._advance_cursor(REFUNDS_CURSOR_KEY, max_refund_dt)

        self._process_queue(stats)
        return stats

    def _resolve_scan_start(self, cursor_key: str) -> str:
        base_dt = _parse_iso_datetime(self._start_at)
        cursor_raw = self._db.get_sync_state(cursor_key)
        if not cursor_raw:
            return base_dt.isoformat()

        try:
            cursor_dt = _parse_iso_datetime(cursor_raw)
        except Exception:
            return base_dt.isoformat()

        if self._lookback_seconds > 0:
            cursor_dt = cursor_dt - timedelta(seconds=self._lookback_seconds)

        if cursor_dt < base_dt:
            cursor_dt = base_dt

        return cursor_dt.isoformat()

    def _advance_cursor(self, cursor_key: str, new_dt: datetime) -> None:
        current_raw = self._db.get_sync_state(cursor_key)
        if current_raw:
            try:
                current_dt = _parse_iso_datetime(current_raw)
                if new_dt <= current_dt:
                    return
            except Exception:
                pass

        self._db.set_sync_state(cursor_key, new_dt.isoformat())

    def _ingest_payments(self, stats: SyncStats, scan_from: str) -> datetime | None:
        max_created_dt: datetime | None = None
        scan_from_dt = _parse_iso_datetime(scan_from)

        for payment in self._yookassa_client.list_succeeded_payments(created_at_gte=scan_from):
            stats.payments_seen += 1

            payment_id = _as_optional_str(payment.get("id"))
            if not payment_id:
                continue

            amount_kopecks = _extract_amount_kopecks(payment)
            if amount_kopecks is None:
                continue

            created_dt = _extract_event_datetime(_as_optional_str(payment.get("created_at")), fallback=scan_from_dt)
            if max_created_dt is None or created_dt > max_created_dt:
                max_created_dt = created_dt

            _, created = self._db.upsert_sync_operation(
                kind="income",
                source_id=payment_id,
                source_payment_id=payment_id,
                source_created_at=created_dt.isoformat(),
                amount_kopecks=amount_kopecks,
                currency=_extract_currency(payment),
                operation_key=f"yk-income-{payment_id}",
                source_payload_json=json_dumps(payment),
            )
            if created:
                stats.operations_added += 1

        return max_created_dt

    def _ingest_refunds(self, stats: SyncStats, scan_from: str) -> datetime | None:
        max_created_dt: datetime | None = None
        scan_from_dt = _parse_iso_datetime(scan_from)

        for refund in self._yookassa_client.list_succeeded_refunds(created_at_gte=scan_from):
            stats.refunds_seen += 1

            refund_id = _as_optional_str(refund.get("id"))
            if not refund_id:
                continue

            amount_kopecks = _extract_amount_kopecks(refund)
            if amount_kopecks is None:
                continue

            created_dt = _extract_event_datetime(_as_optional_str(refund.get("created_at")), fallback=scan_from_dt)
            if max_created_dt is None or created_dt > max_created_dt:
                max_created_dt = created_dt

            payment_id = _as_optional_str(refund.get("payment_id"))
            _, created = self._db.upsert_sync_operation(
                kind="refund",
                source_id=refund_id,
                source_payment_id=payment_id,
                source_created_at=created_dt.isoformat(),
                amount_kopecks=amount_kopecks,
                currency=_extract_currency(refund),
                operation_key=f"yk-refund-{refund_id}",
                source_payload_json=json_dumps(refund),
            )
            if created:
                stats.operations_added += 1

        return max_created_dt

    def _process_queue(self, stats: SyncStats) -> None:
        rows = self._db.list_retryable_sync_operations(limit=self._batch_limit, max_attempts=self._max_attempts)

        for row in rows:
            stats.operations_attempted += 1
            if row["kind"] == "income":
                self._process_income(row, stats)
            else:
                self._process_refund(row, stats)

    def _process_income(self, row: dict[str, Any], stats: SyncStats) -> None:
        payload = _safe_json_dict(row["source_payload_json"])
        income_input = _build_income_input(row, payload)

        if self._dry_run:
            self._db.mark_sync_success(
                int(row["id"]),
                provider_operation_id="DRY-RUN",
                provider_payload_json=json_dumps({"status": "dry_run", "kind": "income"}),
            )
            stats.operations_synced += 1
            return

        result = self._gateway.register_income(income_input)
        self._apply_result(row, result, stats)

    def _process_refund(self, row: dict[str, Any], stats: SyncStats) -> None:
        payment_id = _as_optional_str(row.get("source_payment_id"))
        if not payment_id:
            self._db.mark_sync_dead(
                int(row["id"]),
                error_code="MISSING_PAYMENT_ID",
                error_message="Refund has no payment_id",
            )
            stats.operations_failed += 1
            return

        income_row = self._db.get_sync_operation(kind="income", source_id=payment_id)
        if not income_row or income_row.get("status") != "synced":
            self._db.defer_sync_operation(
                int(row["id"]),
                reason="Income operation is not synced yet",
            )
            stats.operations_deferred += 1
            return

        original_income_amount = int(income_row["amount_kopecks"])
        refunded_before = self._db.get_synced_refund_total_kopecks(
            payment_id,
            include_dry_run=self._dry_run,
            exclude_operation_id=int(row["id"]),
        )
        current_refund_amount = int(row["amount_kopecks"])

        if original_income_amount > 0 and refunded_before + current_refund_amount > original_income_amount:
            self._db.mark_sync_dead(
                int(row["id"]),
                error_code="OVER_REFUND",
                error_message=(
                    "Refund amount exceeds payment amount: "
                    f"payment={original_income_amount}, already_refunded={refunded_before}, refund={current_refund_amount}"
                ),
            )
            stats.operations_failed += 1
            return

        is_partial = original_income_amount > 0 and (refunded_before + current_refund_amount) < original_income_amount

        payload = _safe_json_dict(row["source_payload_json"])
        refund_input = RefundSyncInput(
            operation_key=row["operation_key"],
            refund_id=row["source_id"],
            payment_id=payment_id,
            operation_time=(_normalize_optional_datetime(_as_optional_str(payload.get("created_at"))) or row["source_created_at"]),
            amount_kopecks=current_refund_amount,
            currency=row["currency"],
            original_income_operation_id=_as_optional_str(income_row.get("provider_operation_id")),
            original_income_amount_kopecks=original_income_amount,
            is_partial=is_partial,
            source_payload=payload,
        )

        if self._dry_run:
            self._db.mark_sync_success(
                int(row["id"]),
                provider_operation_id="DRY-RUN",
                provider_payload_json=json_dumps(
                    {
                        "status": "dry_run",
                        "kind": "refund",
                        "is_partial": is_partial,
                    }
                ),
            )
            stats.operations_synced += 1
            return

        result = self._gateway.cancel_income(refund_input)
        self._apply_result(row, result, stats)

    def _apply_result(self, row: dict[str, Any], result: GatewayResult, stats: SyncStats) -> None:
        if result.ok:
            self._db.mark_sync_success(
                int(row["id"]),
                provider_operation_id=result.provider_operation_id,
                provider_payload_json=json_dumps(result.payload),
            )
            stats.operations_synced += 1
            return

        if result.retryable:
            self._db.mark_sync_failed(
                int(row["id"]),
                error_code=result.error_code,
                error_message=result.error_message or "Temporary sync error",
                provider_payload_json=json_dumps(result.payload),
            )
        else:
            self._db.mark_sync_dead(
                int(row["id"]),
                error_code=result.error_code,
                error_message=result.error_message or "Non-retryable sync error",
                provider_payload_json=json_dumps(result.payload),
            )
        stats.operations_failed += 1


def normalize_start_at(value: str) -> str:
    return _parse_iso_datetime(value).isoformat()


def _extract_amount_kopecks(payload: dict[str, Any]) -> int | None:
    amount = payload.get("amount")
    if not isinstance(amount, dict):
        return None

    value = amount.get("value")
    if value is None:
        return None

    try:
        return to_kopecks(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _extract_currency(payload: dict[str, Any]) -> str:
    amount = payload.get("amount")
    if not isinstance(amount, dict):
        return "RUB"
    currency = _as_optional_str(amount.get("currency"))
    return currency.upper() if currency else "RUB"


def _build_income_input(row: dict[str, Any], payload: dict[str, Any]) -> IncomeSyncInput:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    description = _as_optional_str(payload.get("description"))

    customer = {
        "name": _as_optional_str(metadata.get("customer_name")),
        "email": _as_optional_str(metadata.get("customer_email")),
        "phone": _as_optional_str(metadata.get("customer_phone")),
    }
    customer = {k: v for k, v in customer.items() if v is not None}

    items = _build_items_from_metadata(metadata, description, row)

    operation_time = (
        _normalize_optional_datetime(_as_optional_str(payload.get("captured_at")))
        or _normalize_optional_datetime(_as_optional_str(payload.get("created_at")))
        or row["source_created_at"]
    )

    return IncomeSyncInput(
        operation_key=row["operation_key"],
        payment_id=row["source_id"],
        operation_time=operation_time,
        amount_kopecks=int(row["amount_kopecks"]),
        currency=row["currency"],
        description=description,
        customer=customer,
        items=items,
        metadata=metadata,
        source_payload=payload,
    )


def _build_items_from_metadata(
    metadata: dict[str, Any],
    description: str | None,
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_items = metadata.get("items") if isinstance(metadata.get("items"), list) else []

    parsed: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        item_description = _as_optional_str(item.get("description")) or description or "Payment"
        quantity = _as_optional_str(item.get("quantity")) or "1"
        amount = _as_optional_str(item.get("amount")) or _as_optional_str(item.get("price"))
        if not amount:
            continue

        parsed.append(
            {
                "description": item_description,
                "quantity": quantity,
                "amount": amount,
            }
        )

    if parsed:
        return parsed

    fallback_amount = str((Decimal(int(row["amount_kopecks"])) / Decimal(100)).quantize(Decimal("0.01")))
    return [
        {
            "description": description or "Payment",
            "quantity": "1",
            "amount": fallback_amount,
        }
    ]


def _safe_json_dict(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError("Datetime must include timezone")

    return dt.astimezone(timezone.utc)


def _normalize_optional_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _parse_iso_datetime(value).isoformat()
    except Exception:
        return None


def _extract_event_datetime(value: str | None, *, fallback: datetime) -> datetime:
    normalized = _normalize_optional_datetime(value)
    if not normalized:
        return fallback
    return _parse_iso_datetime(normalized)
