from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_kopecks(value: Decimal) -> int:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int((quantized * 100).to_integral_value())


def from_kopecks(value: int) -> Decimal:
    return (Decimal(value) / Decimal(100)).quantize(Decimal("0.01"))


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def verify_hmac_signature(secret: str, payload: bytes, signature: str) -> bool:
    if not secret:
        return True
    if not signature:
        return False

    provided = signature.strip()
    if "=" in provided:
        _, provided = provided.split("=", 1)

    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    expected_hex = digest.hex()
    expected_b64 = base64.b64encode(digest).decode("ascii")

    return hmac.compare_digest(provided.lower(), expected_hex.lower()) or hmac.compare_digest(provided, expected_b64)
