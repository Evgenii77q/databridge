from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReceiptItem(BaseModel):
    description: str = Field(min_length=1, max_length=256)
    quantity: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    amount: Decimal = Field(gt=Decimal("0"))


class Customer(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)


class CreateReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    external_id: str | None = Field(default=None, max_length=200)
    source: str = Field(default="manual", max_length=64)
    description: str | None = Field(default=None, max_length=255)
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    amount: Decimal | None = Field(default=None, gt=Decimal("0"))
    items: list[ReceiptItem] = Field(default_factory=list)
    customer: Customer | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_amount_or_items(self) -> "CreateReceiptRequest":
        if self.amount is None and not self.items:
            raise ValueError("Either 'amount' or 'items' must be provided")
        return self


class ReceiptResponse(BaseModel):
    id: str
    external_id: str | None
    source: str
    status: str
    amount: Decimal
    currency: str
    description: str | None
    customer: Customer | None
    items: list[ReceiptItem]
    metadata: dict[str, Any]
    provider: str
    provider_receipt_id: str | None
    provider_payload: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class YookassaWebhookAck(BaseModel):
    event_received: bool
    event_type: str
    receipt_id: str | None = None
    duplicate: bool = False
