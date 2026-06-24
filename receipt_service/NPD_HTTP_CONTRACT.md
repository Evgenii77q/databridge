# NPD HTTP Contract

Used when `NPD_SYNC_PROVIDER=http`.

## Auth

- Optional bearer token: `Authorization: Bearer <NPD_SYNC_API_TOKEN>`
- Idempotency header is always sent:
  - `Idempotency-Key: <operation_key>`

## Endpoint: `POST /income`

### Request

```json
{
  "operation_key": "yk-income-<payment_id>",
  "payment_id": "...",
  "operation_time": "2026-01-15T07:00:10+00:00",
  "amount": "100.00",
  "currency": "RUB",
  "description": "...",
  "customer": {"email": "..."},
  "items": [{"description": "...", "quantity": "1", "amount": "100.00"}],
  "metadata": {},
  "source_payload": {}
}
```

### Success response examples

```json
{"status":"created","operation_id":"..."}
```

```json
{"status":"duplicate","operation_id":"..."}
```

## Endpoint: `POST /income/cancel`

### Request

```json
{
  "operation_key": "yk-refund-<refund_id>",
  "refund_id": "...",
  "payment_id": "...",
  "operation_time": "2026-01-16T08:00:00+00:00",
  "amount": "50.00",
  "currency": "RUB",
  "is_partial": true,
  "original_income_amount": "100.00",
  "original_income_operation_id": "...",
  "reason": "refund",
  "source_payload": {}
}
```

### Success response examples

```json
{"status":"deleted","operation_id":"..."}
```

```json
{"status":"already_deleted","operation_id":"..."}
```

## Error behavior expected by worker

- `409` or `422` are treated as idempotent duplicate and considered successful.
- `5xx` and network errors are retried.
- Business errors with non-retryable status should return:

```json
{"status":"error","code":"...","message":"..."}
```

These will be marked as terminal (`dead`) and no longer retried.
