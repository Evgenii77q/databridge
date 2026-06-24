from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .utils import utcnow


class ReceiptDB:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY,
                    external_id TEXT,
                    source TEXT NOT NULL,
                    source_event_id TEXT UNIQUE,
                    status TEXT NOT NULL,
                    amount_kopecks INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    description TEXT,
                    customer_json TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_receipt_id TEXT,
                    provider_payload_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_receipts_external_id
                    ON receipts (external_id);

                CREATE INDEX IF NOT EXISTS idx_receipts_created_at
                    ON receipts (created_at DESC);

                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    event_type TEXT NOT NULL,
                    signature_valid INTEGER NOT NULL,
                    body_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_payment_id TEXT,
                    source_created_at TEXT NOT NULL,
                    amount_kopecks INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    source_payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    provider_operation_id TEXT,
                    provider_payload_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, source_id),
                    UNIQUE(operation_key)
                );

                CREATE INDEX IF NOT EXISTS idx_sync_operations_status
                    ON sync_operations (status, attempt_count, source_created_at);

                CREATE INDEX IF NOT EXISTS idx_sync_operations_payment
                    ON sync_operations (source_payment_id);

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_receipt(
        self,
        *,
        external_id: str | None,
        source: str,
        source_event_id: str | None,
        amount_kopecks: int,
        currency: str,
        description: str | None,
        customer_json: str,
        items_json: str,
        metadata_json: str,
        provider: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        receipt_id = str(uuid.uuid4())
        now = utcnow().isoformat()

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO receipts (
                    id, external_id, source, source_event_id, status,
                    amount_kopecks, currency, description,
                    customer_json, items_json, metadata_json,
                    provider, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    external_id,
                    source,
                    source_event_id,
                    status,
                    amount_kopecks,
                    currency,
                    description,
                    customer_json,
                    items_json,
                    metadata_json,
                    provider,
                    now,
                    now,
                ),
            )

        return self.get_receipt(receipt_id)

    def update_receipt_result(
        self,
        receipt_id: str,
        *,
        status: str,
        provider_receipt_id: str | None,
        provider_payload_json: str,
    ) -> dict[str, Any] | None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE receipts
                SET status = ?, provider_receipt_id = ?, provider_payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, provider_receipt_id, provider_payload_json, now, receipt_id),
            )

        return self.get_receipt(receipt_id)

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        return dict(row) if row else None

    def get_receipt_by_source_event(self, source_event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM receipts WHERE source_event_id = ?", (source_event_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_receipts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def log_webhook_event(
        self,
        *,
        event_id: str | None,
        event_type: str,
        signature_valid: bool,
        body_json: str,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO webhook_events (
                    event_id, event_type, signature_valid, body_json, received_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, event_type, int(signature_valid), body_json, now),
            )

    def upsert_sync_operation(
        self,
        *,
        kind: str,
        source_id: str,
        source_payment_id: str | None,
        source_created_at: str,
        amount_kopecks: int,
        currency: str,
        operation_key: str,
        source_payload_json: str,
    ) -> tuple[dict[str, Any], bool]:
        now = utcnow().isoformat()
        created = False

        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO sync_operations (
                        kind, source_id, source_payment_id, source_created_at,
                        amount_kopecks, currency, operation_key, source_payload_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        kind,
                        source_id,
                        source_payment_id,
                        source_created_at,
                        amount_kopecks,
                        currency,
                        operation_key,
                        source_payload_json,
                        now,
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT id FROM sync_operations WHERE kind = ? AND source_id = ?",
                    (kind, source_id),
                ).fetchone()
                if not existing:
                    raise

                conn.execute(
                    """
                    UPDATE sync_operations
                    SET source_payment_id = ?,
                        source_created_at = ?,
                        amount_kopecks = ?,
                        currency = ?,
                        source_payload_json = ?,
                        updated_at = ?
                    WHERE kind = ? AND source_id = ?
                    """,
                    (
                        source_payment_id,
                        source_created_at,
                        amount_kopecks,
                        currency,
                        source_payload_json,
                        now,
                        kind,
                        source_id,
                    ),
                )

            row = conn.execute(
                "SELECT * FROM sync_operations WHERE kind = ? AND source_id = ?",
                (kind, source_id),
            ).fetchone()

        if not row:
            raise RuntimeError("Sync operation not found after upsert")

        return dict(row), created

    def get_sync_operation(self, *, kind: str, source_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_operations WHERE kind = ? AND source_id = ?",
                (kind, source_id),
            ).fetchone()
        return dict(row) if row else None

    def list_retryable_sync_operations(self, *, limit: int, max_attempts: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sync_operations
                WHERE (
                    (status IN ('pending', 'failed') AND attempt_count < ?)
                    OR (status = 'synced' AND provider_operation_id = 'DRY-RUN')
                )
                ORDER BY
                    CASE kind WHEN 'income' THEN 0 ELSE 1 END,
                    source_created_at ASC,
                    id ASC
                LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_sync_success(
        self,
        operation_id: int,
        *,
        provider_operation_id: str | None,
        provider_payload_json: str,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_operations
                SET status = 'synced',
                    attempt_count = attempt_count + 1,
                    provider_operation_id = ?,
                    provider_payload_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (provider_operation_id, provider_payload_json, now, operation_id),
            )

    def mark_sync_failed(
        self,
        operation_id: int,
        *,
        error_code: str | None,
        error_message: str,
        provider_payload_json: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_operations
                SET status = 'failed',
                    attempt_count = attempt_count + 1,
                    provider_payload_json = COALESCE(?, provider_payload_json),
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (provider_payload_json, error_code, error_message, now, operation_id),
            )

    def mark_sync_dead(
        self,
        operation_id: int,
        *,
        error_code: str | None,
        error_message: str,
        provider_payload_json: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_operations
                SET status = 'dead',
                    attempt_count = attempt_count + 1,
                    provider_payload_json = COALESCE(?, provider_payload_json),
                    error_code = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (provider_payload_json, error_code, error_message, now, operation_id),
            )

    def defer_sync_operation(self, operation_id: int, *, reason: str) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_operations
                SET status = 'pending',
                    error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, operation_id),
            )

    def get_synced_refund_total_kopecks(
        self,
        payment_id: str,
        *,
        include_dry_run: bool = False,
        exclude_operation_id: int | None = None,
    ) -> int:
        where = [
            "kind = 'refund'",
            "source_payment_id = ?",
            "status = 'synced'",
        ]
        params: list[Any] = [payment_id]

        if not include_dry_run:
            where.append("(provider_operation_id IS NULL OR provider_operation_id != 'DRY-RUN')")
        if exclude_operation_id is not None:
            where.append("id != ?")
            params.append(exclude_operation_id)

        where_sql = " AND ".join(where)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(amount_kopecks), 0) AS total
                FROM sync_operations
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
        return int(row["total"] or 0) if row else 0

    def get_sync_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN status = 'synced' AND provider_operation_id = 'DRY-RUN' THEN 'dry_run'
                        ELSE status
                    END AS status_group,
                    COUNT(*) AS cnt
                FROM sync_operations
                GROUP BY status_group
                """
            ).fetchall()

        stats = {"pending": 0, "failed": 0, "synced": 0, "dry_run": 0, "dead": 0}
        for row in rows:
            stats[str(row["status_group"])] = int(row["cnt"])
        return stats

    def get_sync_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return str(row["value"])

    def requeue_synced_operations(
        self,
        *,
        provider_operation_id: str | None = None,
        provider_operation_id_prefix: str | None = None,
    ) -> int:
        where = ["status = 'synced'"]
        params: list[Any] = []

        if provider_operation_id is not None:
            where.append("provider_operation_id = ?")
            params.append(provider_operation_id)

        if provider_operation_id_prefix is not None:
            where.append("provider_operation_id LIKE ?")
            params.append(f"{provider_operation_id_prefix}%")

        where_sql = " AND ".join(where)
        now = utcnow().isoformat()

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE sync_operations
                SET status = 'pending',
                    attempt_count = 0,
                    provider_operation_id = NULL,
                    provider_payload_json = NULL,
                    error_code = NULL,
                    error_message = 'Requeued for replay',
                    updated_at = ?
                WHERE {where_sql}
                """,
                [now, *params],
            )
            return int(cur.rowcount or 0)

    def set_sync_state(self, key: str, value: str) -> None:
        now = utcnow().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
