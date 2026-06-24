from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Query, Request

from .config import get_settings
from .db import ReceiptDB
from .models import CreateReceiptRequest, ReceiptResponse, YookassaWebhookAck
from .providers import build_provider
from .service import ReceiptService
from .utils import verify_hmac_signature

settings = get_settings()
db = ReceiptDB(settings.db_path)
provider = build_provider(
    provider_name=settings.provider,
    moy_nalog_api_url=settings.moy_nalog_api_url,
    moy_nalog_token=settings.moy_nalog_token,
    timeout_seconds=settings.provider_timeout_seconds,
)
service = ReceiptService(db, provider, settings.provider)

app = FastAPI(title="Self Receipt Service", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": settings.provider}


@app.post("/receipts", response_model=ReceiptResponse)
def create_receipt(payload: CreateReceiptRequest) -> ReceiptResponse:
    try:
        return service.create_manual_receipt(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/receipts", response_model=list[ReceiptResponse])
def list_receipts(limit: int = Query(default=50, ge=1, le=500)) -> list[ReceiptResponse]:
    return service.list_receipts(limit=limit)


@app.get("/receipts/{receipt_id}", response_model=ReceiptResponse)
def get_receipt(receipt_id: str) -> ReceiptResponse:
    receipt = service.get_receipt(receipt_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


@app.post("/webhooks/yookassa", response_model=YookassaWebhookAck)
async def yookassa_webhook(request: Request) -> YookassaWebhookAck:
    raw = await request.body()
    signature = request.headers.get(settings.yookassa_signature_header, "")
    signature_valid = verify_hmac_signature(settings.yookassa_webhook_secret, raw, signature)

    if settings.yookassa_require_signature and not signature_valid:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    try:
        receipt, duplicate, event_type = service.process_yookassa_webhook(
            event=event,
            signature_valid=signature_valid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return YookassaWebhookAck(
        event_received=True,
        event_type=event_type,
        receipt_id=receipt.id if receipt else None,
        duplicate=duplicate,
    )
