from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from .config import get_settings
from .db import ReceiptDB
from .npd_gateway import build_npd_gateway
from .sync_service import YooKassaNpdSyncService
from .yookassa_client import YooKassaClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync YooKassa payments/refunds to NPD provider")
    parser.add_argument("--start-at", default=None, help="ISO 8601 datetime, default from env YOOKASSA_START_AT")
    parser.add_argument("--dry-run", action="store_true", help="Do not call NPD provider, only mark operations as dry run")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=300,
        help="Loop interval, default 300 seconds",
    )
    parser.add_argument(
        "--db-stats",
        action="store_true",
        help="Print aggregated DB sync stats after each run",
    )
    parser.add_argument(
        "--requeue-dry-run",
        action="store_true",
        help="Move synced DRY-RUN rows back to pending (for first real sync)",
    )
    parser.add_argument(
        "--requeue-mock",
        action="store_true",
        help="Move synced MOCK-* rows back to pending (for switch from mock to real provider)",
    )
    parser.add_argument(
        "--requeue-all-synced",
        action="store_true",
        help="Move all synced rows back to pending (use with caution)",
    )
    parser.add_argument(
        "--requeue-only",
        action="store_true",
        help="Apply requeue actions and exit without running sync",
    )
    return parser.parse_args()


def build_sync_service(*, start_at: str | None, dry_run: bool) -> tuple[YooKassaNpdSyncService, ReceiptDB]:
    settings = get_settings()

    yookassa_client = YooKassaClient(
        shop_id=settings.yookassa_shop_id,
        secret_key=settings.yookassa_secret_key,
        api_base=settings.yookassa_api_base,
        timeout_seconds=settings.yookassa_timeout_seconds,
        list_limit=settings.yookassa_list_limit,
    )

    gateway = build_npd_gateway(
        provider_name=settings.npd_sync_provider,
        base_url=settings.npd_sync_api_url,
        token=settings.npd_sync_api_token,
        timeout_seconds=settings.npd_sync_timeout_seconds,
    )

    db = ReceiptDB(settings.db_path)
    service = YooKassaNpdSyncService(
        db=db,
        yookassa_client=yookassa_client,
        gateway=gateway,
        start_at=start_at or settings.yookassa_start_at,
        max_attempts=settings.npd_sync_max_attempts,
        batch_limit=settings.npd_sync_batch_limit,
        lookback_seconds=settings.npd_sync_lookback_seconds,
        dry_run=dry_run,
    )
    return service, db


def main() -> int:
    args = parse_args()

    try:
        service, db = build_sync_service(start_at=args.start_at, dry_run=args.dry_run)
    except Exception as exc:
        print(_json_error(str(exc)))
        return 1

    requeued = _apply_requeue_flags(db, args)
    if requeued > 0:
        print(
            json.dumps(
                {
                    "status": "requeue",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "requeued": requeued,
                },
                ensure_ascii=False,
            )
        )

    if args.requeue_only:
        payload = {
            "status": "ok",
            "time": datetime.now(timezone.utc).isoformat(),
            "requeued": requeued,
            "sync_executed": False,
        }
        if args.db_stats:
            payload["db_stats"] = db.get_sync_stats()
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    if not args.loop:
        return _run_once(service, db, with_db_stats=args.db_stats)

    while True:
        _run_once(service, db, with_db_stats=args.db_stats)
        time.sleep(max(1, args.interval_seconds))


def _apply_requeue_flags(db: ReceiptDB, args: argparse.Namespace) -> int:
    total = 0
    if args.requeue_all_synced:
        return db.requeue_synced_operations()

    if args.requeue_dry_run:
        total += db.requeue_synced_operations(provider_operation_id="DRY-RUN")
    if args.requeue_mock:
        total += db.requeue_synced_operations(provider_operation_id_prefix="MOCK-")
    return total


def _run_once(service: YooKassaNpdSyncService, db: ReceiptDB, *, with_db_stats: bool) -> int:
    started = datetime.now(timezone.utc)
    try:
        stats = service.run_once()
        payload = _format_stats(stats, started=started)
        if with_db_stats:
            payload["db_stats"] = db.get_sync_stats()
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(_json_error(str(exc)))
        return 1


def _format_stats(stats, *, started: datetime) -> dict[str, int | str]:
    return {
        "status": "ok",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "payments_scan_from": stats.payments_scan_from,
        "refunds_scan_from": stats.refunds_scan_from,
        "payments_seen": stats.payments_seen,
        "refunds_seen": stats.refunds_seen,
        "operations_added": stats.operations_added,
        "operations_attempted": stats.operations_attempted,
        "operations_synced": stats.operations_synced,
        "operations_failed": stats.operations_failed,
        "operations_deferred": stats.operations_deferred,
    }


def _json_error(message: str) -> str:
    return json.dumps(
        {
            "time": datetime.now(timezone.utc).isoformat(),
            "status": "error",
            "message": message,
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
