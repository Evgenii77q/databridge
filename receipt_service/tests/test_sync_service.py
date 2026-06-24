from __future__ import annotations

from pathlib import Path

from receipt_service.db import ReceiptDB
from receipt_service.npd_gateway import GatewayResult
from receipt_service.sync_service import YooKassaNpdSyncService


class FakeYooKassaClient:
    def __init__(self, *, payments: list[dict] | None = None, refunds: list[dict] | None = None) -> None:
        self.payments = payments or []
        self.refunds = refunds or []

    def validate(self) -> None:
        return None

    def list_succeeded_payments(self, *, created_at_gte: str):
        return iter(self.payments)

    def list_succeeded_refunds(self, *, created_at_gte: str):
        return iter(self.refunds)


class CaptureGateway:
    def __init__(self) -> None:
        self.income_calls = []
        self.refund_calls = []

    def register_income(self, data):
        self.income_calls.append(data)
        return GatewayResult(
            ok=True,
            provider_operation_id=f"INC-{data.payment_id}",
            payload={"status": "created", "operation_id": f"INC-{data.payment_id}"},
        )

    def cancel_income(self, data):
        self.refund_calls.append(data)
        return GatewayResult(
            ok=True,
            provider_operation_id=f"REF-{data.refund_id}",
            payload={"status": "deleted", "operation_id": f"REF-{data.refund_id}"},
        )


def _payment(payment_id: str, *, amount: str, created_at: str) -> dict:
    return {
        "id": payment_id,
        "status": "succeeded",
        "created_at": created_at,
        "description": "service payment",
        "amount": {"value": amount, "currency": "RUB"},
        "metadata": {"customer_email": "u@example.com"},
    }


def _refund(refund_id: str, *, payment_id: str, amount: str, created_at: str) -> dict:
    return {
        "id": refund_id,
        "status": "succeeded",
        "payment_id": payment_id,
        "created_at": created_at,
        "amount": {"value": amount, "currency": "RUB"},
    }


def _build_service(
    tmp_path: Path,
    *,
    client: FakeYooKassaClient,
    gateway,
    dry_run: bool = False,
    max_attempts: int = 10,
) -> tuple[YooKassaNpdSyncService, ReceiptDB]:
    db = ReceiptDB(tmp_path / "sync.db")
    service = YooKassaNpdSyncService(
        db=db,
        yookassa_client=client,
        gateway=gateway,
        start_at="2026-01-01T00:00:00+03:00",
        max_attempts=max_attempts,
        batch_limit=1000,
        lookback_seconds=0,
        dry_run=dry_run,
    )
    return service, db


def test_dedup_same_payment_twice(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")]
    )
    gateway = CaptureGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    first = service.run_once()
    second = service.run_once()

    assert first.operations_added == 1
    assert first.operations_synced == 1
    assert second.operations_added == 0
    assert second.operations_attempted == 0
    assert db.get_sync_stats()["synced"] == 1


def test_partial_refund_is_marked_partial(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")],
        refunds=[_refund("r1", payment_id="p1", amount="40.00", created_at="2026-01-11T10:00:00+00:00")],
    )
    gateway = CaptureGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    stats = service.run_once()
    refund_row = db.get_sync_operation(kind="refund", source_id="r1")

    assert stats.operations_failed == 0
    assert len(gateway.refund_calls) == 1
    assert gateway.refund_calls[0].is_partial is True
    assert refund_row is not None
    assert refund_row["status"] == "synced"


def test_over_refund_marked_dead(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")],
        refunds=[
            _refund("r1", payment_id="p1", amount="60.00", created_at="2026-01-11T10:00:00+00:00"),
            _refund("r2", payment_id="p1", amount="50.00", created_at="2026-01-11T10:01:00+00:00"),
        ],
    )
    gateway = CaptureGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    stats = service.run_once()
    r1 = db.get_sync_operation(kind="refund", source_id="r1")
    r2 = db.get_sync_operation(kind="refund", source_id="r2")

    assert stats.operations_failed == 1
    assert len(gateway.refund_calls) == 1
    assert r1 is not None and r1["status"] == "synced"
    assert r2 is not None and r2["status"] == "dead"
    assert r2["error_code"] == "OVER_REFUND"


def test_refund_deferred_until_income_synced(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[],
        refunds=[_refund("r1", payment_id="p1", amount="20.00", created_at="2026-01-11T10:00:00+00:00")],
    )
    gateway = CaptureGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    first = service.run_once()
    deferred_row = db.get_sync_operation(kind="refund", source_id="r1")
    assert first.operations_deferred == 1
    assert deferred_row is not None and deferred_row["status"] == "pending"

    client.payments = [_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")]
    second = service.run_once()
    synced_row = db.get_sync_operation(kind="refund", source_id="r1")

    assert second.operations_synced >= 2
    assert synced_row is not None and synced_row["status"] == "synced"


def test_retryable_error_then_success(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")]
    )

    class RetryGateway(CaptureGateway):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def register_income(self, data):
            self.calls += 1
            if self.calls == 1:
                return GatewayResult(
                    ok=False,
                    provider_operation_id=None,
                    payload={"status": "error"},
                    error_code="500",
                    error_message="temporary",
                    retryable=True,
                )
            return super().register_income(data)

    gateway = RetryGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    first = service.run_once()
    first_row = db.get_sync_operation(kind="income", source_id="p1")
    assert first.operations_failed == 1
    assert first_row is not None and first_row["status"] == "failed"

    second = service.run_once()
    second_row = db.get_sync_operation(kind="income", source_id="p1")
    assert second.operations_synced == 1
    assert second_row is not None and second_row["status"] == "synced"
    assert int(second_row["attempt_count"]) == 2


def test_nonretryable_error_goes_dead_and_not_retried(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")]
    )

    class DeadGateway(CaptureGateway):
        def register_income(self, data):
            return GatewayResult(
                ok=False,
                provider_operation_id=None,
                payload={"status": "error", "code": "VALIDATION"},
                error_code="VALIDATION",
                error_message="invalid data",
                retryable=False,
            )

    gateway = DeadGateway()
    service, db = _build_service(tmp_path, client=client, gateway=gateway)

    first = service.run_once()
    first_row = db.get_sync_operation(kind="income", source_id="p1")
    assert first.operations_failed == 1
    assert first_row is not None and first_row["status"] == "dead"
    assert int(first_row["attempt_count"]) == 1

    second = service.run_once()
    second_row = db.get_sync_operation(kind="income", source_id="p1")
    assert second.operations_attempted == 0
    assert second_row is not None and int(second_row["attempt_count"]) == 1


def test_real_run_replays_previous_dry_run_rows(tmp_path: Path) -> None:
    client = FakeYooKassaClient(
        payments=[_payment("p1", amount="100.00", created_at="2026-01-10T10:00:00+00:00")]
    )

    dry_gateway = CaptureGateway()
    dry_service, db = _build_service(tmp_path, client=client, gateway=dry_gateway, dry_run=True)
    dry_stats = dry_service.run_once()
    dry_row = db.get_sync_operation(kind="income", source_id="p1")

    assert dry_stats.operations_synced == 1
    assert dry_row is not None
    assert dry_row["status"] == "synced"
    assert dry_row["provider_operation_id"] == "DRY-RUN"

    real_gateway = CaptureGateway()
    real_service = YooKassaNpdSyncService(
        db=db,
        yookassa_client=client,
        gateway=real_gateway,
        start_at="2026-01-01T00:00:00+03:00",
        max_attempts=10,
        batch_limit=1000,
        lookback_seconds=0,
        dry_run=False,
    )
    real_stats = real_service.run_once()
    real_row = db.get_sync_operation(kind="income", source_id="p1")

    assert real_stats.operations_attempted == 1
    assert len(real_gateway.income_calls) == 1
    assert real_row is not None
    assert real_row["status"] == "synced"
    assert real_row["provider_operation_id"] != "DRY-RUN"


def test_requeue_synced_operations_for_mock_and_dry_run(tmp_path: Path) -> None:
    db = ReceiptDB(tmp_path / "sync.db")

    row1, _ = db.upsert_sync_operation(
        kind="income",
        source_id="p1",
        source_payment_id="p1",
        source_created_at="2026-01-10T10:00:00+00:00",
        amount_kopecks=10000,
        currency="RUB",
        operation_key="yk-income-p1",
        source_payload_json="{}",
    )
    db.mark_sync_success(int(row1["id"]), provider_operation_id="DRY-RUN", provider_payload_json="{}")

    row2, _ = db.upsert_sync_operation(
        kind="income",
        source_id="p2",
        source_payment_id="p2",
        source_created_at="2026-01-10T10:01:00+00:00",
        amount_kopecks=10000,
        currency="RUB",
        operation_key="yk-income-p2",
        source_payload_json="{}",
    )
    db.mark_sync_success(int(row2["id"]), provider_operation_id="MOCK-INCOME-p2", provider_payload_json="{}")

    dry_requeued = db.requeue_synced_operations(provider_operation_id="DRY-RUN")
    mock_requeued = db.requeue_synced_operations(provider_operation_id_prefix="MOCK-")

    p1 = db.get_sync_operation(kind="income", source_id="p1")
    p2 = db.get_sync_operation(kind="income", source_id="p2")

    assert dry_requeued == 1
    assert mock_requeued == 1
    assert p1 is not None and p1["status"] == "pending" and int(p1["attempt_count"]) == 0
    assert p2 is not None and p2["status"] == "pending" and int(p2["attempt_count"]) == 0
