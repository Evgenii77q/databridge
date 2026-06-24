# Receipt Service (self-employed)

Standalone FastAPI service with two modes:
- receipt API mode (`POST /receipts`, webhook endpoint)
- YooKassa sync mode (`python -m receipt_service.sync_cli`) for automatic payment/refund import

## Features

- `POST /webhooks/yookassa` for `payment.succeeded`
- `POST /receipts` manual receipt creation
- `GET /receipts`, `GET /receipts/{id}`
- optional webhook signature validation
- sync worker for YooKassa payments/refunds starting from fixed date (`YOOKASSA_START_AT`)
- strict anti-duplication in DB (`UNIQUE(kind, source_id)` + unique operation key)
- refund pipeline (`refund.succeeded` -> cancellation operation)
- incremental sync cursors with overlap window (`NPD_SYNC_LOOKBACK_SECONDS`)
- dry-run backfill safety: rows synced as `DRY-RUN` are automatically replayed in real mode

## Quick start (API mode)

```bash
cd receipt_service
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn receipt_service.app:app --reload --port 8090
```

Health check:

```bash
curl http://127.0.0.1:8090/health
```

## Sync mode (YooKassa -> NPD)

### 1) Configure env

Use `.env.example` values.
Required for YooKassa import:
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`
- `YOOKASSA_START_AT=2026-01-01T00:00:00+03:00`

Config loader reads `.env` from current directory or from `receipt_service/.env`.

### 2) Dry run (safe)

```bash
python -m receipt_service.sync_cli --dry-run --db-stats
```

`dry-run` marks provider operation as `DRY-RUN` and does not lose data for future real sync.

### 3) Real one-shot run

```bash
python -m receipt_service.sync_cli --db-stats
```

Rows previously marked as `DRY-RUN` will be sent to provider in this real run automatically.

If you previously ran one-shot in `mock` mode and need to resend data to real provider:

```bash
python -m receipt_service.sync_cli --requeue-mock --requeue-dry-run --requeue-only --db-stats
python -m receipt_service.sync_cli --db-stats
```

### 4) Continuous mode

```bash
python -m receipt_service.sync_cli --loop --interval-seconds 300 --db-stats
```

### 5) Ready loop script

```bash
./receipt_service/run_sync.sh
```

### 6) One-shot script for cron

```bash
./receipt_service/run_sync_once.sh
```

## NPD sync provider

`NPD_SYNC_PROVIDER`:
- `mock` - no external requests (safe testing)
- `http` - sends JSON to:
  - `POST {NPD_SYNC_API_URL}/income`
  - `POST {NPD_SYNC_API_URL}/income/cancel`

`http` mode is an adapter contract for your partner gateway.
Detailed payload format: `receipt_service/NPD_HTTP_CONTRACT.md`.

If partner API differs, run thin proxy adapter and point worker to proxy URL:

```bash
uvicorn receipt_service.partner_proxy:app --host 127.0.0.1 --port 8091
```

Then set:

```env
NPD_SYNC_PROVIDER=http
NPD_SYNC_API_URL=http://127.0.0.1:8091
```

### Production `.env` minimum

```env
YOOKASSA_SHOP_ID=464355
YOOKASSA_SECRET_KEY=live_...
YOOKASSA_START_AT=2026-01-01T00:00:00+03:00
NPD_SYNC_PROVIDER=http
NPD_SYNC_API_URL=https://partner.example/api
NPD_SYNC_API_TOKEN=...
NPD_SYNC_LOOKBACK_SECONDS=21600
NPD_SYNC_BATCH_LIMIT=1000
NPD_SYNC_MAX_ATTEMPTS=20
```

When `NPD_SYNC_PROVIDER=http`, `NPD_SYNC_API_URL` is required.

## Anti-duplication and retries

- every payment is stored as `income` operation keyed by `payment.id`
- every refund is stored as `refund` operation keyed by `refund.id`
- duplicate YooKassa pulls do not create duplicate operations
- failed operations are retried up to `NPD_SYNC_MAX_ATTEMPTS`
- refunds are deferred until corresponding income is synced
- over-refund is blocked (if refund total exceeds payment amount)

## Incremental sync behavior

- worker stores two cursors:
  - `yookassa_payments_created_at`
  - `yookassa_refunds_created_at`
- each run scans from `(cursor - lookback)` to avoid missing late arrivals
- first run starts from `YOOKASSA_START_AT`

## Cron (every 5 minutes)

Install cron entry:

```bash
./receipt_service/install_cron.sh
```

Manual cron line equivalent:

```cron
*/5 * * * * cd /absolute/path/to/project && /bin/sh /absolute/path/to/project/receipt_service/run_sync_once.sh >> /absolute/path/to/project/receipt_service/logs/sync_cron.log 2>&1
```

`run_sync_once.sh` uses lock directory and skips overlap if previous sync is still running.

## Optional Partner Proxy Env

`receipt_service.partner_proxy` reads:

```env
PROXY_TARGET_BASE_URL=https://actual-partner-api.example
PROXY_TARGET_INCOME_PATH=/income
PROXY_TARGET_CANCEL_PATH=/income/cancel
PROXY_TARGET_TOKEN=
PROXY_TIMEOUT_SECONDS=20
```

## Local NPD API mock (for end-to-end check)

Run local mock server:

```bash
./receipt_service/run_local_npd_mock.sh
```

In another terminal run worker against mock server (without changing your real `.env`):

```bash
RECEIPT_DB_PATH=receipt_service/data/receipts.e2e.db \
NPD_SYNC_PROVIDER=http \
NPD_SYNC_API_URL=http://127.0.0.1:18091 \
NPD_SYNC_API_TOKEN=local-test \
python -m receipt_service.sync_cli --db-stats
```

Second run should keep idempotency and avoid duplicates.

## Refund behavior

- source event: `refund.succeeded`
- mapped to cancellation operation (`income/cancel` in adapter)
- payload includes `is_partial` flag and original income amount

## Legal note

Official self-employed fiscalization in Russia requires an accepted integration flow (for example, FNS partner contour or a connected partner gateway). This service automates data synchronization on your side.

## Tests

```bash
python -m pip install -r receipt_service/requirements-dev.txt
python -m pytest -q receipt_service/tests
```
