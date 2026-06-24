from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="NPD Partner Proxy", version="1.0.0")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


TARGET_BASE_URL = _env("PROXY_TARGET_BASE_URL")
TARGET_INCOME_PATH = _env("PROXY_TARGET_INCOME_PATH", "/income")
TARGET_CANCEL_PATH = _env("PROXY_TARGET_CANCEL_PATH", "/income/cancel")
TARGET_TOKEN = _env("PROXY_TARGET_TOKEN")
TARGET_TIMEOUT_SECONDS = float(_env("PROXY_TIMEOUT_SECONDS", "20"))


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "proxy_target_configured": bool(TARGET_BASE_URL),
        "target_base_url": TARGET_BASE_URL,
    }


@app.post("/income")
async def income(request: Request) -> JSONResponse:
    payload = await request.json()
    operation_key = request.headers.get("Idempotency-Key", "").strip()
    return _forward(
        upstream_path=TARGET_INCOME_PATH,
        payload=payload,
        operation_key=operation_key,
        duplicate_status="duplicate",
    )


@app.post("/income/cancel")
async def income_cancel(request: Request) -> JSONResponse:
    payload = await request.json()
    operation_key = request.headers.get("Idempotency-Key", "").strip()
    return _forward(
        upstream_path=TARGET_CANCEL_PATH,
        payload=payload,
        operation_key=operation_key,
        duplicate_status="already_deleted",
    )


def _forward(
    *,
    upstream_path: str,
    payload: dict[str, Any],
    operation_key: str,
    duplicate_status: str,
) -> JSONResponse:
    if not TARGET_BASE_URL:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "code": "PROXY_CONFIG_ERROR",
                "message": "PROXY_TARGET_BASE_URL is required",
            },
        )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if operation_key:
        headers["Idempotency-Key"] = operation_key
    if TARGET_TOKEN:
        headers["Authorization"] = f"Bearer {TARGET_TOKEN}"

    url = f"{TARGET_BASE_URL.rstrip('/')}/{upstream_path.lstrip('/')}"
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )

    try:
        with urllib.request.urlopen(request, timeout=TARGET_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="ignore")
            parsed = _json_or_raw(raw)
            if isinstance(parsed, dict):
                return JSONResponse(status_code=200, content=parsed)
            return JSONResponse(status_code=200, content={"status": "ok", "upstream": parsed})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        parsed = _json_or_raw(raw)

        if exc.code in {409, 422}:
            if isinstance(parsed, dict):
                content = {"status": duplicate_status, **parsed}
            else:
                content = {"status": duplicate_status, "upstream": parsed}
            return JSONResponse(status_code=200, content=content)

        status_code = 503 if exc.code >= 500 else 400
        code = "UPSTREAM_5XX" if exc.code >= 500 else "UPSTREAM_4XX"
        message = f"Upstream returned HTTP {exc.code}"
        content = {
            "status": "error",
            "code": code,
            "message": message,
            "upstream_status": exc.code,
        }
        if isinstance(parsed, dict):
            content["upstream"] = parsed
        else:
            content["upstream_raw"] = parsed

        return JSONResponse(status_code=status_code, content=content)
    except urllib.error.URLError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "code": "UPSTREAM_NETWORK",
                "message": str(exc.reason),
            },
        )


def _json_or_raw(raw: str) -> dict[str, Any] | str:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    if isinstance(parsed, dict):
        return parsed
    return {"raw": parsed}
