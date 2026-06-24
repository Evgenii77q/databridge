from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="Mock NPD Partner API", version="1.0.0")

_income_by_key: dict[str, str] = {}
_cancel_by_key: dict[str, str] = {}


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "income_keys": len(_income_by_key),
        "cancel_keys": len(_cancel_by_key),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/income")
async def income(request: Request) -> dict[str, Any]:
    payload = await request.json()
    operation_key = _get_operation_key(request, payload)
    if not operation_key:
        return {
            "status": "error",
            "code": "VALIDATION",
            "message": "operation_key is required",
        }

    existing = _income_by_key.get(operation_key)
    if existing:
        return {"status": "duplicate", "operation_id": existing}

    operation_id = f"INC-{payload.get('payment_id') or uuid.uuid4().hex[:12]}"
    _income_by_key[operation_key] = operation_id
    return {"status": "created", "operation_id": operation_id}


@app.post("/income/cancel")
async def income_cancel(request: Request) -> dict[str, Any]:
    payload = await request.json()
    operation_key = _get_operation_key(request, payload)
    if not operation_key:
        return {
            "status": "error",
            "code": "VALIDATION",
            "message": "operation_key is required",
        }

    existing = _cancel_by_key.get(operation_key)
    if existing:
        return {"status": "already_deleted", "operation_id": existing}

    operation_id = f"CANCEL-{payload.get('refund_id') or uuid.uuid4().hex[:12]}"
    _cancel_by_key[operation_key] = operation_id
    return {"status": "deleted", "operation_id": operation_id}


def _get_operation_key(request: Request, payload: dict[str, Any]) -> str | None:
    key = request.headers.get("Idempotency-Key") or payload.get("operation_key")
    if key is None:
        return None
    text = str(key).strip()
    return text or None
