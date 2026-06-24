from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator


class YooKassaApiError(RuntimeError):
    pass


class YooKassaClient:
    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        api_base: str,
        timeout_seconds: float,
        list_limit: int,
    ) -> None:
        self._shop_id = shop_id
        self._secret_key = secret_key
        self._api_base = api_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._list_limit = max(1, min(list_limit, 100))

    def validate(self) -> None:
        if not self._shop_id:
            raise ValueError("YOOKASSA_SHOP_ID is required")
        if not self._secret_key:
            raise ValueError("YOOKASSA_SECRET_KEY is required")

    def list_succeeded_payments(self, *, created_at_gte: str) -> Iterator[dict[str, Any]]:
        query = {
            "created_at.gte": created_at_gte,
            "status": "succeeded",
            "limit": str(self._list_limit),
        }
        yield from self._paginate("/payments", query)

    def list_succeeded_refunds(self, *, created_at_gte: str) -> Iterator[dict[str, Any]]:
        query = {
            "created_at.gte": created_at_gte,
            "status": "succeeded",
            "limit": str(self._list_limit),
        }
        yield from self._paginate("/refunds", query)

    def _paginate(self, path: str, query: dict[str, str]) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            params = dict(query)
            if cursor:
                params["cursor"] = cursor

            payload = self._get(path, params)
            items = payload.get("items")
            if not isinstance(items, list):
                raise YooKassaApiError(f"Unexpected list response for '{path}'")

            for item in items:
                if isinstance(item, dict):
                    yield item

            cursor_value = payload.get("next_cursor")
            cursor = str(cursor_value).strip() if cursor_value else None
            if not cursor:
                break
            if cursor in seen_cursors:
                raise YooKassaApiError(f"Pagination loop detected for '{path}'")
            seen_cursors.add(cursor)

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        auth = base64.b64encode(f"{self._shop_id}:{self._secret_key}".encode("utf-8")).decode("ascii")
        query = urllib.parse.urlencode(params)
        url = f"{self._api_base}{path}?{query}" if query else f"{self._api_base}{path}"

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
                payload = json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise YooKassaApiError(f"YooKassa HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise YooKassaApiError(f"YooKassa connection error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise YooKassaApiError("YooKassa returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise YooKassaApiError("YooKassa response is not JSON object")

        return payload
