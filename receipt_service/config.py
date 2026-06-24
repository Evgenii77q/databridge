import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue

        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip().strip('"').strip("'")



def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path

    provider: str
    yookassa_webhook_secret: str
    yookassa_signature_header: str
    yookassa_require_signature: bool

    yookassa_shop_id: str
    yookassa_secret_key: str
    yookassa_api_base: str
    yookassa_start_at: str
    yookassa_timeout_seconds: float
    yookassa_list_limit: int

    moy_nalog_api_url: str
    moy_nalog_token: str
    provider_timeout_seconds: float

    npd_sync_provider: str
    npd_sync_api_url: str
    npd_sync_api_token: str
    npd_sync_timeout_seconds: float
    npd_sync_max_attempts: int
    npd_sync_batch_limit: int
    npd_sync_lookback_seconds: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv_once()

    db_path = Path(os.getenv("RECEIPT_DB_PATH", "receipt_service/data/receipts.db"))
    provider = os.getenv("RECEIPT_PROVIDER", "mock").strip().lower()

    yookassa_webhook_secret = os.getenv("YOOKASSA_WEBHOOK_SECRET", "")
    yookassa_signature_header = os.getenv("YOOKASSA_SIGNATURE_HEADER", "X-YooKassa-Signature")
    yookassa_require_signature = _env_bool("YOOKASSA_REQUIRE_SIGNATURE", default=bool(yookassa_webhook_secret))

    yookassa_shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    yookassa_secret_key = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    yookassa_api_base = os.getenv("YOOKASSA_API_BASE", "https://api.yookassa.ru/v3").strip().rstrip("/")
    yookassa_start_at = os.getenv("YOOKASSA_START_AT", "2026-01-01T00:00:00+03:00").strip()
    yookassa_timeout_seconds = float(os.getenv("YOOKASSA_TIMEOUT_SECONDS", "20"))
    yookassa_list_limit = int(os.getenv("YOOKASSA_LIST_LIMIT", "100"))

    moy_nalog_api_url = os.getenv("MOY_NALOG_API_URL", "").strip()
    moy_nalog_token = os.getenv("MOY_NALOG_TOKEN", "").strip()
    provider_timeout_seconds = float(os.getenv("RECEIPT_PROVIDER_TIMEOUT_SECONDS", "10"))

    npd_sync_provider = os.getenv("NPD_SYNC_PROVIDER", "mock").strip().lower()
    npd_sync_api_url = os.getenv("NPD_SYNC_API_URL", "").strip()
    npd_sync_api_token = os.getenv("NPD_SYNC_API_TOKEN", "").strip()
    npd_sync_timeout_seconds = float(os.getenv("NPD_SYNC_TIMEOUT_SECONDS", "20"))
    npd_sync_max_attempts = int(os.getenv("NPD_SYNC_MAX_ATTEMPTS", "20"))
    npd_sync_batch_limit = int(os.getenv("NPD_SYNC_BATCH_LIMIT", "1000"))
    npd_sync_lookback_seconds = int(os.getenv("NPD_SYNC_LOOKBACK_SECONDS", "21600"))

    db_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        db_path=db_path,
        provider=provider,
        yookassa_webhook_secret=yookassa_webhook_secret,
        yookassa_signature_header=yookassa_signature_header,
        yookassa_require_signature=yookassa_require_signature,
        yookassa_shop_id=yookassa_shop_id,
        yookassa_secret_key=yookassa_secret_key,
        yookassa_api_base=yookassa_api_base,
        yookassa_start_at=yookassa_start_at,
        yookassa_timeout_seconds=yookassa_timeout_seconds,
        yookassa_list_limit=yookassa_list_limit,
        moy_nalog_api_url=moy_nalog_api_url,
        moy_nalog_token=moy_nalog_token,
        provider_timeout_seconds=provider_timeout_seconds,
        npd_sync_provider=npd_sync_provider,
        npd_sync_api_url=npd_sync_api_url,
        npd_sync_api_token=npd_sync_api_token,
        npd_sync_timeout_seconds=npd_sync_timeout_seconds,
        npd_sync_max_attempts=npd_sync_max_attempts,
        npd_sync_batch_limit=npd_sync_batch_limit,
        npd_sync_lookback_seconds=npd_sync_lookback_seconds,
    )
