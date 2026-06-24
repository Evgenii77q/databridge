from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import ReceiptDB
from .models import CreateReceiptRequest, Customer, ReceiptItem, ReceiptResponse
from .providers import FiscalProvider, ProviderError, ProviderIssueInput
from .utils import from_kopecks, json_dumps, to_kopecks


class ReceiptService:
    def __init__(self, db: ReceiptDB, provider: FiscalProvider, provider_name: str) -> None:
        self._db = db
        self._provider = provider
        self._provider_name = provider_name

    def create_manual_receipt(self, payload: CreateReceiptRequest) -> ReceiptResponse:
        return self._create_receipt(payload=payload, source_event_id=None)

    def process_yookassa_webhook(
        self,
        *,
        event: dict[str, Any],
        signature_valid: bool,
    ) -> tuple[ReceiptResponse | None, bool, str]:
        event_type = str(event.get("event") or "unknown")
        event_obj = event.get("object") or {}
        payment_id = str(event_obj.get("id") or "")
        source_event_id = f"{event_type}:{payment_id}" if payment_id else None

        self._db.log_webhook_event(
            event_id=payment_id or None,
            event_type=event_type,
            signature_valid=signature_valid,
            body_json=json_dumps(event),
        )

        if event_type != "payment.succeeded":
            return None, False, event_type

        if not source_event_id:
            raise ValueError("YooKassa event has no payment id")

        existing = self._db.get_receipt_by_source_event(source_event_id)
        if existing:
            return self.row_to_response(existing), True, event_type

        payload = self._build_create_payload_from_yookassa(event_obj)
        receipt = self._create_receipt(payload=payload, source_event_id=source_event_id)
        return receipt, False, event_type

    def list_receipts(self, limit: int = 50) -> list[ReceiptResponse]:
        rows = self._db.list_receipts(limit=limit)
        return [self.row_to_response(row) for row in rows]

    def get_receipt(self, receipt_id: str) -> ReceiptResponse | None:
        row = self._db.get_receipt(receipt_id)
        return self.row_to_response(row) if row else None

    def _create_receipt(self, payload: CreateReceiptRequest, source_event_id: str | None) -> ReceiptResponse:
        amount_kopecks, items = self._normalize_amount_and_items(payload)
        customer_dict = payload.customer.model_dump() if payload.customer else {}
        metadata_dict = payload.metadata or {}

        try:
            row = self._db.create_receipt(
                external_id=payload.external_id,
                source=payload.source,
                source_event_id=source_event_id,
                amount_kopecks=amount_kopecks,
                currency=payload.currency.upper(),
                description=payload.description,
                customer_json=json_dumps(customer_dict),
                items_json=json_dumps([item.model_dump(mode="json") for item in items]),
                metadata_json=json_dumps(metadata_dict),
                provider=self._provider_name,
                status="pending",
            )
        except sqlite3.IntegrityError as exc:
            if source_event_id:
                existing = self._db.get_receipt_by_source_event(source_event_id)
                if existing:
                    return self.row_to_response(existing)
            raise RuntimeError("Duplicate receipt") from exc

        provider_input = ProviderIssueInput(
            receipt_id=row["id"],
            external_id=payload.external_id,
            source=payload.source,
            amount_kopecks=amount_kopecks,
            currency=payload.currency.upper(),
            description=payload.description,
            customer=customer_dict,
            items=[item.model_dump(mode="json") for item in items],
            metadata=metadata_dict,
        )

        try:
            result = self._provider.issue_receipt(provider_input)
            updated_row = self._db.update_receipt_result(
                row["id"],
                status=result.status,
                provider_receipt_id=result.provider_receipt_id,
                provider_payload_json=json_dumps(result.payload),
            )
        except ProviderError as exc:
            updated_row = self._db.update_receipt_result(
                row["id"],
                status="failed",
                provider_receipt_id=None,
                provider_payload_json=json_dumps({"error": str(exc)}),
            )

        return self.row_to_response(updated_row or row)

    def _normalize_amount_and_items(self, payload: CreateReceiptRequest) -> tuple[int, list[ReceiptItem]]:
        items = payload.items
        if not items:
            if payload.amount is None:
                raise ValueError("Amount is required")
            items = [
                ReceiptItem(
                    description=payload.description or "Service",
                    quantity=Decimal("1"),
                    amount=payload.amount,
                )
            ]

        total = Decimal("0")
        for item in items:
            total += item.amount * item.quantity

        if payload.amount is not None:
            amount = payload.amount
            declared_kopecks = to_kopecks(amount)
            calculated_kopecks = to_kopecks(total)
            if declared_kopecks != calculated_kopecks:
                raise ValueError("Provided amount does not match items total")
            return declared_kopecks, items

        return to_kopecks(total), items

    def _build_create_payload_from_yookassa(self, payment_obj: dict[str, Any]) -> CreateReceiptRequest:
        amount_info = payment_obj.get("amount") or {}
        value_raw = amount_info.get("value")
        if value_raw is None:
            raise ValueError("YooKassa payload missing object.amount.value")

        try:
            amount = Decimal(str(value_raw))
        except InvalidOperation as exc:
            raise ValueError("Invalid payment amount") from exc

        currency = str(amount_info.get("currency") or "RUB")
        description = payment_obj.get("description")

        metadata = payment_obj.get("metadata") if isinstance(payment_obj.get("metadata"), dict) else {}
        customer = Customer(
            name=_as_optional_str(metadata.get("customer_name")),
            email=_as_optional_str(metadata.get("customer_email")),
            phone=_as_optional_str(metadata.get("customer_phone")),
        )

        items = _parse_items(metadata.get("items"), default_description=description or "Payment")

        external_id = _as_optional_str(metadata.get("order_id")) or _as_optional_str(payment_obj.get("id"))

        return CreateReceiptRequest(
            external_id=external_id,
            source="yookassa",
            description=description,
            amount=amount,
            currency=currency,
            items=items,
            customer=customer,
            metadata=metadata,
        )

    @staticmethod
    def row_to_response(row: dict[str, Any]) -> ReceiptResponse:
        customer_dict = json.loads(row["customer_json"] or "{}")
        items_list = json.loads(row["items_json"] or "[]")
        metadata = json.loads(row["metadata_json"] or "{}")
        provider_payload = json.loads(row["provider_payload_json"]) if row.get("provider_payload_json") else None

        customer = Customer(**customer_dict) if customer_dict else None
        items = [ReceiptItem(**item) for item in items_list]

        return ReceiptResponse(
            id=row["id"],
            external_id=row["external_id"],
            source=row["source"],
            status=row["status"],
            amount=from_kopecks(int(row["amount_kopecks"])),
            currency=row["currency"],
            description=row["description"],
            customer=customer,
            items=items,
            metadata=metadata,
            provider=row["provider"],
            provider_receipt_id=row["provider_receipt_id"],
            provider_payload=provider_payload,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_items(raw_items: Any, default_description: str) -> list[ReceiptItem]:
    if not isinstance(raw_items, list) or not raw_items:
        return []

    parsed: list[ReceiptItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        description = _as_optional_str(raw.get("description")) or default_description

        quantity_raw = raw.get("quantity", "1")
        amount_raw = raw.get("amount", raw.get("price"))
        if amount_raw is None:
            continue

        try:
            quantity = Decimal(str(quantity_raw))
            amount = Decimal(str(amount_raw))
        except InvalidOperation:
            continue

        if quantity <= 0 or amount <= 0:
            continue

        parsed.append(ReceiptItem(description=description, quantity=quantity, amount=amount))

    if not parsed:
        return []

    return parsed
