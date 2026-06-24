import os
import json
import uuid
import subprocess
import sys
import re
import unicodedata
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from decimal import Decimal
import csv
import io

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import psycopg2
from psycopg2 import sql
from psycopg2 import extras
import pandas as pd

from company_profiles import (
    DEFAULT_PROFILE_ID,
    ensure_default_profile,
    list_profiles,
    load_profile,
    read_profile_file,
    sanitize_profile_id,
    save_profile,
)

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
UPLOAD_DIR = Path("/tmp/documino_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SCRIPT1 = PROJECT_ROOT / "match_excel_to_pg.py"
SCRIPT2 = PROJECT_ROOT / "match_mapping_to_pg.py"
MAPPING_ROOT = Path(os.environ.get("DOCUMINO_MAPPING_ROOT", "/Users/evgeniy/Desktop/ID2"))
ui_env = os.environ.get("DOCUMINO_UI_DIR")
UI_DIR = Path(ui_env).expanduser() if ui_env else None
if not UI_DIR or not UI_DIR.exists():
    desktop_platform_ui = Path("/Users/evgeniy/Desktop/documino_platform")
    desktop_ui = Path("/Users/evgeniy/Desktop/documino_ui")
    project_ui = PROJECT_ROOT / "documino_ui"
    if desktop_platform_ui.exists():
        UI_DIR = desktop_platform_ui
    elif desktop_ui.exists():
        UI_DIR = desktop_ui
    else:
        UI_DIR = project_ui

app = FastAPI(title="DataBrigde Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


RUNS = {}
CONNECTORS = [
    {"id": "powerbi", "name": "Power BI", "category": "BI"},
    {"id": "jira", "name": "Jira", "category": "ITSM"},
    {"id": "bitrix24", "name": "Bitrix24", "category": "CRM"},
    {"id": "sap", "name": "SAP", "category": "ERP"},
    {"id": "onec", "name": "1C", "category": "ERP"},
]

LLM_BASE_URL = os.environ.get("DOCUMINO_LLM_BASE_URL", "https://api.openai.com/v1/chat/completions").strip()
LLM_MODEL = os.environ.get("DOCUMINO_LLM_MODEL", "gpt-4o-mini").strip()
LLM_API_KEY = os.environ.get("DOCUMINO_LLM_API_KEY", "").strip()
LLM_TIMEOUT_SEC = int(os.environ.get("DOCUMINO_LLM_TIMEOUT_SEC", "25"))

ensure_default_profile()


def default_migration_progress():
    return {
        "state": "idle",
        "stage": "Idle",
        "percent": 0,
        "message": "Нет запусков",
        "current_table": "",
        "processed_tables": 0,
        "total_tables": 0,
        "processed_rows": 0,
        "total_rows": 0,
        "rows_per_sec": 0.0,
        "eta_sec": None,
        "started_at": None,
        "finished_at": None,
    }


def set_migration_progress(run: dict, **kwargs):
    progress = run.get("migration_progress")
    if not isinstance(progress, dict):
        progress = default_migration_progress()
        run["migration_progress"] = progress
    progress.update(kwargs)
    active_run_id = run.get("active_migration_run_id")
    if active_run_id:
        history_payload = {
            "state": progress.get("state"),
            "stage": progress.get("stage"),
            "percent": progress.get("percent"),
            "message": progress.get("message"),
            "current_table": progress.get("current_table"),
            "processed_tables": progress.get("processed_tables"),
            "total_tables": progress.get("total_tables"),
            "processed_rows": progress.get("processed_rows"),
            "total_rows": progress.get("total_rows"),
            "rows_per_sec": progress.get("rows_per_sec"),
            "eta_sec": progress.get("eta_sec"),
            "source_type": run.get("active_migration_source_type"),
            "mode": run.get("active_migration_mode"),
            "tables": list(run.get("active_migration_tables") or []),
        }
        if str(progress.get("state") or "").lower() in {"completed", "error", "cancelled"}:
            history_payload["finished_at"] = progress.get("finished_at") or datetime.utcnow().isoformat()
        update_migration_history(run, active_run_id, **history_payload)
    run["updated_at"] = datetime.utcnow().isoformat()
    return progress


def is_migration_cancel_requested(run: dict):
    return bool(run.get("migration_cancel_requested"))


def append_migration_event(run: dict, message: str, level: str = "info", table: Optional[str] = None):
    events = run.setdefault("migration_events", [])
    event = {
        "ts": datetime.utcnow().isoformat(),
        "level": str(level or "info").lower(),
        "message": str(message or ""),
    }
    if table:
        event["table"] = table
    events.append(event)
    # Keep a reasonable rolling window.
    if len(events) > 400:
        del events[:-400]
    active_run_id = run.get("active_migration_run_id")
    if active_run_id:
        item = update_migration_history(run, str(active_run_id))
        item_events = item.setdefault("events", [])
        item_events.append(dict(event))
        if len(item_events) > 600:
            del item_events[:-600]
        item["event_count"] = len(item_events)
    run["updated_at"] = datetime.utcnow().isoformat()
    return event


def update_migration_history(run: dict, run_id: str, **kwargs):
    history = run.setdefault("migration_history", [])
    item = None
    for rec in reversed(history):
        if rec.get("id") == run_id:
            item = rec
            break
    if item is None:
        item = {"id": run_id, "started_at": datetime.utcnow().isoformat()}
        history.append(item)
    item.update(kwargs)
    if "finished_at" not in item:
        item["finished_at"] = None
    item["updated_at"] = datetime.utcnow().isoformat()
    if len(history) > 300:
        del history[:-300]
    run["updated_at"] = datetime.utcnow().isoformat()
    return item


def serialize_migration_history_item(item: dict, include_events: bool = False):
    row = dict(item or {})
    events = row.get("events")
    if isinstance(events, list):
        row["event_count"] = len(events)
        if not include_events:
            row.pop("events", None)
    else:
        row["event_count"] = int(row.get("event_count") or 0)
        if include_events:
            row["events"] = []
    return row


def latest_migration_history(run: dict, limit: int = 120, include_events: bool = False):
    history = list(run.get("migration_history", []))
    history.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    items = history[: max(int(limit or 120), 1)]
    return [serialize_migration_history_item(i, include_events=include_events) for i in items]


def get_migration_history_item(run: dict, run_id: str, include_events: bool = False):
    for rec in reversed(run.get("migration_history", [])):
        if str(rec.get("id") or "") == str(run_id):
            return serialize_migration_history_item(rec, include_events=include_events)
    return None


def migration_level_counts(events):
    out = {"info": 0, "warn": 0, "error": 0, "success": 0}
    for ev in events or []:
        level = str((ev or {}).get("level") or "info").lower()
        out[level] = out.get(level, 0) + 1
    return out


def migration_diff_items(base: dict, target: dict):
    a_tables = list(base.get("tables") or [])
    b_tables = list(target.get("tables") or [])
    a_set = set(a_tables)
    b_set = set(b_tables)
    a_levels = migration_level_counts(base.get("events") or [])
    b_levels = migration_level_counts(target.get("events") or [])
    return {
        "from_run": serialize_migration_history_item(base, include_events=False),
        "to_run": serialize_migration_history_item(target, include_events=False),
        "delta": {
            "percent": int(target.get("percent") or 0) - int(base.get("percent") or 0),
            "processed_tables": int(target.get("processed_tables") or 0) - int(base.get("processed_tables") or 0),
            "processed_rows": int(target.get("processed_rows") or 0) - int(base.get("processed_rows") or 0),
            "total_rows": int(target.get("total_rows") or 0) - int(base.get("total_rows") or 0),
            "event_count": int(target.get("event_count") or 0) - int(base.get("event_count") or 0),
            "rows_per_sec": round(float(target.get("rows_per_sec") or 0.0) - float(base.get("rows_per_sec") or 0.0), 3),
            "errors": int(b_levels.get("error", 0)) - int(a_levels.get("error", 0)),
            "warns": int(b_levels.get("warn", 0)) - int(a_levels.get("warn", 0)),
        },
        "tables_added": sorted(list(b_set - a_set)),
        "tables_removed": sorted(list(a_set - b_set)),
        "same_tables": sorted(list(a_set & b_set)),
        "levels": {
            "from": a_levels,
            "to": b_levels,
        },
    }


def normalize_mapping_override_rows(rows):
    out = []
    seen = set()
    for idx, raw in enumerate(rows or [], start=1):
        row = raw or {}
        excel_col = str(
            row.get("excel_column")
            or row.get("excel")
            or row.get("source_column")
            or ""
        ).strip()
        db_col = str(
            row.get("db_column")
            or row.get("db")
            or row.get("target_column")
            or ""
        ).strip()
        if not excel_col:
            continue
        key = (excel_col.lower(), db_col.lower())
        if key in seen:
            continue
        seen.add(key)
        match_raw = row.get("match_count")
        if match_raw is None:
            match_raw = row.get("matches")
        try:
            match_count = int(str(match_raw).split("/")[0]) if match_raw is not None else (10 if db_col and not db_col.startswith("(") else 0)
        except Exception:
            match_count = 10 if db_col and not db_col.startswith("(") else 0
        out.append(
            {
                "index": len(out) + 1,
                "excel_column": excel_col,
                "db_column": db_col,
                "match_count": max(match_count, 0),
            }
        )
    if not out:
        raise HTTPException(status_code=400, detail="Пустой mapping override.")
    return out


def build_profile_summary(profile: dict):
    matching = profile.get("matching") or {}
    return {
        "profile_id": profile.get("profile_id") or DEFAULT_PROFILE_ID,
        "company_name": profile.get("company_name") or "",
        "description": profile.get("description") or "",
        "inherit_default": bool(profile.get("inherit_default")),
        "path": f"company_profiles/{profile.get('profile_id')}/profile.json",
        "stats": {
            "manual_mappings": len(matching.get("manual_mappings") or {}),
            "name_keywords": len(matching.get("name_keywords") or {}),
            "table_keywords": len(matching.get("table_name_keywords") or {}),
            "date_intents": len(matching.get("date_intent_keywords") or {}),
            "bonus_rules": len(matching.get("column_bonus_rules") or []),
            "table_bias_rules": len(matching.get("table_bias_rules") or []),
        },
    }


def is_truthy_text(value):
    val = str(value or "").strip().lower()
    return val in {"1", "true", "t", "yes", "y", "да"}


DATE_TYPES = {
    "date",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
}

NUM_TYPES = {"smallint", "integer", "bigint", "numeric", "decimal", "real", "double precision"}
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RunStep1Request(BaseModel):
    file_id: str
    sheet_name: Optional[str] = None
    profile_id: Optional[str] = None


class RunStep2Request(BaseModel):
    file_id: str
    target_table: Optional[str] = None
    sheet_name: Optional[str] = None


class MigrationPlanRequest(BaseModel):
    file_id: str
    source: dict = {}
    mode: str = "full"
    cdc: bool = True
    dry_run: bool = False
    masking: bool = False
    rollback: bool = False


class MigrationTestRequest(BaseModel):
    type: str = "postgres"
    host: Optional[str] = None
    port: Optional[int] = None
    db: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None


class MigrationTablesRequest(BaseModel):
    source: dict = {}


class MigrationPreviewRequest(BaseModel):
    source: dict = {}
    table: str


class MigrationValidateRequest(BaseModel):
    source: dict = {}
    table: str
    file_id: Optional[str] = None


class MigrationConflictsRequest(BaseModel):
    source: dict = {}
    table: str
    file_id: Optional[str] = None
    target_table: Optional[str] = None


class MigrationSchemaDiffRequest(BaseModel):
    source: dict = {}
    table: str
    file_id: Optional[str] = None
    target_table: Optional[str] = None


class MigrationRecommendRequest(BaseModel):
    source: dict = {}
    table: str
    file_id: Optional[str] = None
    target_table: Optional[str] = None


class MigrationApplyRecommendationRequest(BaseModel):
    file_id: str
    action: dict = {}
    table: Optional[str] = None
    source: dict = {}
    target_table: Optional[str] = None


class MigrationGateRequest(BaseModel):
    source: dict = {}
    table: str
    file_id: Optional[str] = None
    target_table: Optional[str] = None


class IntegrationConnectRequest(BaseModel):
    file_id: str
    connector_id: str
    config: dict = {}


class IntegrationActionRequest(BaseModel):
    file_id: str
    connector_id: str
    config: dict = {}


class IntegrationSyncRequest(BaseModel):
    file_id: str
    connector_id: str
    direction: str = "push"
    options: dict = {}


class MigrationRunRequest(BaseModel):
    file_id: str
    source: dict = {}
    tables: list = []
    mode: str = "upsert"
    target_schema: str = "public"
    cdc: bool = True
    dry_run: bool = False
    masking: bool = False
    rollback: bool = False
    start_stage: str = "schema"
    retry_of: Optional[str] = None


class MigrationCancelRequest(BaseModel):
    file_id: str


class MigrationRetryRequest(BaseModel):
    file_id: str
    run_id: str
    start_stage: str = "schema"
    source: dict = {}


class MappingOverrideRequest(BaseModel):
    file_id: str
    sheet_name: Optional[str] = None
    rows: list = []


class MappingSelectTableRequest(BaseModel):
    file_id: str
    table: str


class CompanyProfileSaveRequest(BaseModel):
    profile_id: Optional[str] = None
    company_name: str
    description: str = ""
    inherit_default: bool = True
    matching: dict = {}


class AIMappingInsightsRequest(BaseModel):
    file_id: Optional[str] = None
    rows: list = []
    limit: int = 12


class AIAssistantRequest(BaseModel):
    question: str
    file_id: Optional[str] = None
    sheet_name: Optional[str] = None
    mode: str = "platform"
    include_mapping: bool = True
    include_migration: bool = True
    conversation: list = []


def find_latest_mapping(excel_path: Path):
    base = excel_path.stem
    output_dir = excel_path.parent / f"{base}_mapping_json"
    if not output_dir.exists():
        return None, None
    json_files = sorted(output_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    csv_files = sorted(output_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    json_path = json_files[0] if json_files else None
    csv_path = csv_files[0] if csv_files else None
    return json_path, csv_path


def load_sheet(json_path: Path, sheet_name: Optional[str]):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise HTTPException(status_code=400, detail="Mapping JSON is empty")
    if sheet_name:
        for sheet in data:
            if sheet.get("sheet_name") == sheet_name:
                return sheet
        raise HTTPException(status_code=404, detail=f"Sheet '{sheet_name}' not found")
    return data[0]


def resolve_default_pg_database(host=None, port=None, user=None, password=None):
    env_doc_db = os.environ.get("DOCUMINO_PGDATABASE", "").strip()
    env_pg_db = os.environ.get("PGDATABASE", "").strip()
    host = host or os.environ.get("DOCUMINO_PGHOST") or os.environ.get("PGHOST") or "localhost"
    port = port or os.environ.get("DOCUMINO_PGPORT") or os.environ.get("PGPORT") or "5432"
    user = user or os.environ.get("DOCUMINO_PGUSER") or os.environ.get("PGUSER")
    password = password or os.environ.get("DOCUMINO_PGPASSWORD") or os.environ.get("PGPASSWORD")

    preferred = []
    for item in [env_doc_db, env_pg_db, "sedo_mock", "sedo", "postgres"]:
        name = str(item or "").strip()
        if name and name not in preferred:
            preferred.append(name)

    try:
        conn = psycopg2.connect(
            dbname="postgres",
            user=user,
            password=password,
            host=host,
            port=port,
            connect_timeout=3,
        )
        try:
            cur = conn.cursor()
            cur.execute("select datname from pg_database where datistemplate = false")
            dbs = {str(row[0]) for row in cur.fetchall() if row and row[0]}
        finally:
            conn.close()

        for name in preferred:
            if name in dbs:
                return name
        if dbs:
            return sorted(dbs)[0]
    except Exception:
        pass

    return preferred[0] if preferred else "sedo"


def build_script_pg_env(base_env: dict):
    env = base_env.copy()
    env["PG_NO_PROMPT"] = "1"
    env["MAPPING_ROOT"] = str(MAPPING_ROOT)
    env["PGHOST"] = os.environ.get("DOCUMINO_PGHOST", os.environ.get("PGHOST", "localhost"))
    env["PGPORT"] = os.environ.get("DOCUMINO_PGPORT", os.environ.get("PGPORT", "5432"))
    if os.environ.get("DOCUMINO_PGUSER") or os.environ.get("PGUSER"):
        env["PGUSER"] = os.environ.get("DOCUMINO_PGUSER", os.environ.get("PGUSER", ""))
    if os.environ.get("DOCUMINO_PGPASSWORD") or os.environ.get("PGPASSWORD"):
        env["PGPASSWORD"] = os.environ.get("DOCUMINO_PGPASSWORD", os.environ.get("PGPASSWORD", ""))
    env["PGDATABASE"] = resolve_default_pg_database(
        host=env.get("PGHOST"),
        port=env.get("PGPORT"),
        user=env.get("PGUSER"),
        password=env.get("PGPASSWORD"),
    )
    return env


def can_connect_script_pg(env: dict):
    host = env.get("PGHOST") or "localhost"
    port = env.get("PGPORT") or "5432"
    dbname = env.get("PGDATABASE") or resolve_default_pg_database(host=host, port=port, user=env.get("PGUSER"), password=env.get("PGPASSWORD"))
    user = env.get("PGUSER")
    password = env.get("PGPASSWORD")
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            connect_timeout=4,
        )
        conn.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def summarize_script_error(stderr: str, stdout: str):
    text = "\n".join([stderr or "", stdout or ""]).strip()
    if not text:
        return "Unknown script error"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    low_text = text.lower()

    if "modulenotfounderror" in low_text and "no module named" in low_text:
        for ln in lines:
            if "No module named" in ln:
                return ln
        return "Missing Python dependency in script environment."

    if (
        "postgres connection failed" in low_text
        or "operationalerror" in low_text
        or "connection to server at" in low_text
        or "could not connect to server" in low_text
        or "getpasswarning" in low_text
        or "password:" in low_text
    ):
        return "Postgres connection failed. Start Postgres and verify PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD."

    if "permission denied" in low_text:
        return "Permission denied while running step 1 script."

    if "file not found" in low_text or "no such file or directory" in low_text:
        return "Input or output file was not found during script run."

    compact = []
    for ln in lines:
        if ln.startswith("Traceback"):
            continue
        if ln.startswith("File "):
            continue
        if ln.startswith("dt = pd.to_datetime"):
            continue
        if "UserWarning:" in ln:
            continue
        compact.append(ln)
    msg = "\n".join(compact[:4] if compact else lines[:4])
    return msg[:500]


def to_snake_name(text: str, default: str = "value"):
    ru_map = str.maketrans(
        {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
            "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
            "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
            "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
        }
    )
    source = str(text or "").lower().translate(ru_map)
    base = unicodedata.normalize("NFKD", source)
    base = base.encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").lower()
    return base or default


def build_offline_step1(run: dict, file_id: str, excel_path: Path, sheet_name: Optional[str]):
    try:
        xls = pd.ExcelFile(excel_path)
        all_sheets = xls.sheet_names or []
        selected_sheet = sheet_name if sheet_name in all_sheets else (all_sheets[0] if all_sheets else None)
        if not selected_sheet:
            raise RuntimeError("Листы не найдены в Excel файле.")
        df = pd.read_excel(excel_path, sheet_name=selected_sheet, dtype=str)
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать Excel для offline режима: {exc}") from exc

    columns = []
    for col in df.columns.tolist():
        col_name = str(col or "").strip()
        if not col_name:
            continue
        if col_name.lower().startswith("unnamed:"):
            continue
        columns.append(col_name)
    if not columns:
        columns = ["column_1"]

    step2 = [{"excel_column": c, "db_column": f"({c})", "match_count": 0} for c in columns]
    original_name = str(run.get("filename") or excel_path.name)
    table_slug = to_snake_name(Path(original_name).stem, "uploaded_file")
    table = f"public.ddt_{table_slug}"

    payload = [{"sheet_name": selected_sheet, "step2": step2, "step4": {"selection": table}}]
    json_path = UPLOAD_DIR / f"{file_id}_offline_mapping.json"
    csv_path = UPLOAD_DIR / f"{file_id}_offline_mapping.csv"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Excel column", "DB column", "Matches"])
        for row in step2:
            writer.writerow([row["excel_column"], row["db_column"], row["match_count"]])

    run["json"] = str(json_path)
    run["csv"] = str(csv_path)
    run["sheet"] = selected_sheet
    run["updated_at"] = datetime.utcnow().isoformat()
    run["table"] = table
    run["step2"] = step2
    run["offline_fallback"] = True
    run["offline_reason"] = "Postgres недоступен — использован offline режим (сопоставление по структуре файла)."

    return {
        "sheet_name": selected_sheet,
        "db_name": "offline",
        "table": table,
        "step2": step2,
        "profile_id": run.get("profile_id") or DEFAULT_PROFILE_ID,
        "profile": run.get("profile_summary"),
        "csv_url": f"/api/download?file_id={file_id}&kind=csv",
        "json_url": f"/api/download?file_id={file_id}&kind=json",
        "warning": run["offline_reason"],
    }


def friendly_backend_error(exc: Exception):
    text = str(exc or "")
    low = text.lower()
    if "connection to server at" in low or "could not connect" in low:
        return "Не удалось подключиться к Postgres. Проверь host/port/db/user/password и доступность сервера."
    if "access denied" in low or "password authentication failed" in low:
        return "Ошибка аутентификации. Проверь пользователя и пароль."
    if "unknown database" in low or "database" in low and "does not exist" in low:
        return "База данных не найдена. Проверь имя базы."
    if "driver not installed" in low:
        return text
    return text


def get_postgres_conn(source: Optional[dict] = None):
    source = source or {}
    return psycopg2.connect(
        dbname=source.get("db") or resolve_default_pg_database(
            host=source.get("host"),
            port=source.get("port"),
            user=source.get("user"),
            password=source.get("password"),
        ),
        user=source.get("user") or os.environ.get("PGUSER"),
        password=source.get("password") or os.environ.get("PGPASSWORD"),
        host=source.get("host") or os.environ.get("PGHOST", "localhost"),
        port=source.get("port") or os.environ.get("PGPORT", 5432),
        connect_timeout=5,
    )


def get_conn():
    # Target (platform) Postgres connection.
    return get_postgres_conn({})


def get_external_stats(source: dict):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")

    stats = {
        "tables": 0,
        "rows": 0,
        "size_bytes": 0,
        "schemas": 1,
        "top_tables": [],
    }

    if source_type == "oracle":
        try:
            import oracledb  # type: ignore
        except Exception as exc:
            raise RuntimeError("Oracle driver not installed") from exc
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), COALESCE(SUM(num_rows),0) FROM user_tables")
            stats["tables"], stats["rows"] = [int(v or 0) for v in cur.fetchone()]
            try:
                cur.execute("SELECT COALESCE(SUM(bytes),0) FROM user_segments")
                stats["size_bytes"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                cur.execute(
                    """
                    SELECT table_name, COALESCE(num_rows,0) FROM (
                      SELECT table_name, COALESCE(num_rows,0) AS num_rows
                      FROM user_tables
                      ORDER BY num_rows DESC
                    ) WHERE ROWNUM <= 5
                    """
                )
                stats["top_tables"] = [
                    {"name": r[0], "rows": int(r[1] or 0)} for r in cur.fetchall()
                ]
            except Exception:
                pass
        return stats

    if source_type == "mysql":
        try:
            import pymysql  # type: ignore
        except Exception as exc:
            raise RuntimeError("MySQL driver not installed") from exc
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(table_rows),0),
                           COALESCE(SUM(data_length+index_length),0)
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                    """
                )
                tables, rows, size_bytes = cur.fetchone()
                stats["tables"] = int(tables or 0)
                stats["rows"] = int(rows or 0)
                stats["size_bytes"] = int(size_bytes or 0)
                cur.execute(
                    """
                    SELECT table_name, COALESCE(table_rows,0)
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                    ORDER BY table_rows DESC
                    LIMIT 5
                    """
                )
                stats["top_tables"] = [
                    {"name": r[0], "rows": int(r[1] or 0)} for r in cur.fetchall()
                ]
        finally:
            conn.close()
        return stats

    if source_type == "db2":
        try:
            import ibm_db  # type: ignore
        except Exception as exc:
            raise RuntimeError("DB2 driver not installed") from exc
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db.connect(conn_str, "", "")
        try:
            stmt = ibm_db.exec_immediate(
                conn,
                """
                SELECT COUNT(*), COALESCE(SUM(card),0)
                FROM syscat.tables
                WHERE tabschema = CURRENT SCHEMA
                """,
            )
            row = ibm_db.fetch_tuple(stmt)
            if row:
                stats["tables"] = int(row[0] or 0)
                stats["rows"] = int(row[1] or 0)
            try:
                stmt = ibm_db.exec_immediate(
                    conn,
                    """
                    SELECT tabname, COALESCE(card,0)
                    FROM syscat.tables
                    WHERE tabschema = CURRENT SCHEMA
                    ORDER BY card DESC
                    FETCH FIRST 5 ROWS ONLY
                    """,
                )
                rows = []
                r = ibm_db.fetch_tuple(stmt)
                while r:
                    rows.append({"name": r[0], "rows": int(r[1] or 0)})
                    r = ibm_db.fetch_tuple(stmt)
                stats["top_tables"] = rows
            except Exception:
                pass
        finally:
            ibm_db.close(conn)
        return stats

    return stats


def normalize_value(val):
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    return val


def ensure_ident(name: str):
    if not name or not IDENT_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid identifier")
    return name


def parse_table_name(table: str):
    if not table or any(ch.isspace() for ch in table):
        raise HTTPException(status_code=400, detail="Invalid table name")
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = None, table
    if schema:
        ensure_ident(schema)
    ensure_ident(name)
    return schema, name


def collect_offline_schema_tables(limit: int = 200):
    rows = {}
    for run in RUNS.values():
        table_ref = run.get("table")
        if not table_ref:
            continue
        try:
            schema, table = resolve_table(run)
        except Exception:
            continue
        key = (schema, table)
        rows[key] = max(rows.get(key, 0), len(run.get("step2") or []))
    out = [{"schema": k[0], "table": k[1], "rows": int(v)} for k, v in rows.items()]
    out.sort(key=lambda r: r["rows"], reverse=True)
    return out[: max(int(limit or 200), 1)]


def build_offline_table_card(schema: str, name: str):
    for run in RUNS.values():
        table_ref = run.get("table")
        if not table_ref:
            continue
        try:
            s, t = resolve_table(run)
        except Exception:
            continue
        if s != schema or t != name:
            continue
        step2 = run.get("step2") or []
        cols = []
        seen = set()
        for idx, row in enumerate(step2, 1):
            db_col = str(row.get("db_column") or "").strip()
            excel_col = str(row.get("excel_column") or "").strip()
            if not db_col or db_col.startswith("("):
                db_col = to_snake_name(excel_col, f"col_{idx}")
            while db_col in seen:
                db_col = f"{db_col}_{idx}"
            seen.add(db_col)
            cols.append({"name": db_col, "type": "text"})
        if not cols:
            cols = [{"name": "r_object_id", "type": "text"}]
        return {
            "schema": schema,
            "table": name,
            "columns": cols,
            "foreign_keys": [],
            "incoming_foreign_keys": [],
            "offline": True,
        }
    return None


def get_external_tables(source: dict):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            cur.execute("SELECT table_name FROM user_tables ORDER BY table_name")
            return [r[0] for r in cur.fetchall()]

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name"
                )
                return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tabschema, tabname
                FROM syscat.tables
                WHERE type = 'T' AND tabschema NOT LIKE 'SYS%'
                ORDER BY tabschema, tabname
                """
            )
            return [f"{r[0]}.{r[1]}" for r in cur.fetchall()]
        finally:
            conn.close()

    # postgres by default
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog','information_schema')
                ORDER BY table_schema, table_name
                """
            )
            return [f"{r[0]}.{r[1]}" for r in cur.fetchall()]
    finally:
        conn.close()


def get_external_preview(source: dict, table: str, limit: Optional[int] = 100):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT column_name
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table
                ORDER BY column_id
                """,
                {"owner": owner, "table": table_name},
            )
            columns = [r[0] for r in cur.fetchall()]
            if limit and limit > 0:
                try:
                    cur.execute(f"SELECT * FROM {owner}.{table_name} FETCH FIRST {int(limit)} ROWS ONLY")
                except Exception:
                    cur.execute(f"SELECT * FROM {owner}.{table_name} WHERE ROWNUM <= {int(limit)}")
            else:
                cur.execute(f"SELECT * FROM {owner}.{table_name}")
            rows = [list(r) for r in cur.fetchall()]
        return columns, rows

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (name,),
            )
            columns = [r[0] for r in cur.fetchall()]
            if limit and limit > 0:
                cur.execute(f"SELECT * FROM `{name}` LIMIT {int(limit)}")
            else:
                cur.execute(f"SELECT * FROM `{name}`")
            rows = cur.fetchall()
        finally:
            conn.close()
        return columns, [list(r) for r in rows]

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT colname
                FROM syscat.columns
                WHERE tabschema = ? AND tabname = ?
                ORDER BY colno
                """,
                (schema_name, table_name),
            )
            columns = [r[0] for r in cur.fetchall()]
            if limit and limit > 0:
                cur.execute(f"SELECT * FROM {schema_name}.{table_name} FETCH FIRST {int(limit)} ROWS ONLY")
            else:
                cur.execute(f"SELECT * FROM {schema_name}.{table_name}")
            rows = cur.fetchall()
        finally:
            conn.close()
        return columns, [list(r) for r in rows]

    # postgres default
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            schema_name = schema or "public"
            table_name = name
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, table_name),
            )
            columns = [r[0] for r in cur.fetchall()]
            query = sql.SQL("SELECT * FROM {}.{}").format(
                sql.Identifier(schema_name),
                sql.Identifier(table_name),
            )
            if limit and limit > 0:
                query = query + sql.SQL(" LIMIT {}").format(sql.Literal(int(limit)))
            cur.execute(query)
            rows = cur.fetchall()
        return columns, [list(r) for r in rows]
    finally:
        conn.close()


def get_primary_key_columns(source: dict, table: str):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT cols.column_name
                FROM all_constraints cons
                JOIN all_cons_columns cols
                  ON cons.constraint_name = cols.constraint_name
                 AND cons.owner = cols.owner
                WHERE cons.constraint_type = 'P'
                  AND cons.owner = :owner
                  AND cons.table_name = :table
                ORDER BY cols.position
                """,
                {"owner": owner, "table": table_name},
            )
            return [r[0] for r in cur.fetchall()]

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = DATABASE()
                  AND tc.table_name = %s
                ORDER BY kcu.ordinal_position
                """,
                (name,),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT colname
                FROM syscat.keycoluse
                WHERE constname IN (
                  SELECT constname FROM syscat.tabconst
                  WHERE tabschema = ? AND tabname = ? AND type = 'P'
                )
                ORDER BY colseq
                """,
                (schema_name, table_name),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    # postgres
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            schema_name = schema or "public"
            cur.execute(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s
                  AND tc.table_name = %s
                ORDER BY kcu.ordinal_position
                """,
                (schema_name, name),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def get_columns_for_source(source: dict, table: str):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT column_name
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table
                ORDER BY column_id
                """,
                {"owner": owner, "table": table_name},
            )
            return [r[0] for r in cur.fetchall()]

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (name,),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT colname
                FROM syscat.columns
                WHERE tabschema = ? AND tabname = ?
                ORDER BY colno
                """,
                (schema_name, table_name),
            )
            return [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    # postgres
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            schema_name = schema or "public"
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, name),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def get_columns_with_types(source: dict, table: str):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT column_name, data_type
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table
                ORDER BY column_id
                """,
                {"owner": owner, "table": table_name},
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (name,),
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            cur.execute(
                """
                SELECT colname, typename
                FROM syscat.columns
                WHERE tabschema = ? AND tabname = ?
                ORDER BY colno
                """,
                (schema_name, table_name),
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        finally:
            conn.close()

    # postgres default
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            schema_name = schema or "public"
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, name),
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def type_category(dtype: str):
    if not dtype:
        return "other"
    d = dtype.lower()
    if any(k in d for k in ["char", "text", "clob", "varchar", "nchar", "nvarchar"]):
        return "string"
    if any(k in d for k in ["date", "time", "timestamp"]):
        return "date"
    if "bool" in d or d in {"boolean", "bit"}:
        return "bool"
    if any(k in d for k in ["int", "number", "numeric", "decimal", "real", "double", "float"]):
        return "numeric"
    if any(k in d for k in ["json", "xml"]):
        return "semi"
    return "other"


def looks_like_valid_for_type(value, dtype: str):
    if value is None:
        return True
    text = str(value).strip()
    if text == "":
        return True
    cat = type_category(dtype)
    if cat == "date":
        try:
            pd.to_datetime(text, errors="raise", dayfirst=True)
            return True
        except Exception:
            return False
    if cat == "bool":
        return text.lower() in {"1", "0", "true", "false", "t", "f", "yes", "no", "y", "n", "да", "нет"}
    if cat == "numeric":
        try:
            Decimal(text.replace(",", "."))
            return True
        except Exception:
            return False
    return True


def map_mysql_type(data_type: str, column_type: str = "", char_len: Optional[int] = None):
    dt = (data_type or "").lower()
    ct = (column_type or "").lower()
    if dt == "tinyint" and ct.startswith("tinyint(1"):
        return "boolean"
    if dt in {"tinyint", "smallint"}:
        return "smallint"
    if dt in {"mediumint", "int", "integer"}:
        return "integer"
    if dt == "bigint":
        return "bigint"
    if dt in {"decimal", "numeric"}:
        return "numeric"
    if dt in {"float", "double", "real"}:
        return "double precision"
    if dt in {"date"}:
        return "date"
    if dt in {"datetime", "timestamp"}:
        return "timestamp"
    if dt in {"time"}:
        return "time"
    if dt in {"json"}:
        return "jsonb"
    if dt in {"blob", "longblob", "mediumblob", "varbinary", "binary"}:
        return "bytea"
    if dt in {"text", "longtext", "mediumtext"}:
        return "text"
    if dt in {"char", "varchar"}:
        if char_len:
            return f"varchar({char_len})"
        return "varchar"
    return "text"


def map_generic_type_to_pg(dtype: str):
    d = (dtype or "").lower()
    if d in {"smallint", "integer", "bigint", "numeric", "real", "double precision"}:
        return d
    if d in {"boolean", "bool"}:
        return "boolean"
    if d in {"date"}:
        return "date"
    if d.startswith("timestamp") or d in {"datetime"}:
        return "timestamp"
    if d.startswith("time"):
        return "time"
    if "json" in d:
        return "jsonb"
    if "uuid" in d:
        return "uuid"
    if "char" in d or "text" in d or "clob" in d or "varchar" in d:
        return "text"
    if "int" in d:
        return "bigint"
    if any(k in d for k in ["number", "decimal", "float", "double", "real"]):
        return "numeric"
    if "date" in d or "time" in d:
        return "timestamp"
    return "text"


SENSITIVE_COL_HINTS = (
    "email",
    "mail",
    "phone",
    "mobile",
    "passport",
    "inn",
    "snils",
    "card",
    "iban",
    "account",
    "address",
    "адрес",
    "телефон",
    "почта",
)


def is_sensitive_column(col_name: str):
    lower = str(col_name or "").lower()
    return any(h in lower for h in SENSITIVE_COL_HINTS)


def mask_scalar(value):
    if value is None:
        return None
    text = str(value)
    if not text:
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def mask_row(row, col_names):
    out = []
    for idx, val in enumerate(row):
        name = col_names[idx] if idx < len(col_names) else ""
        out.append(mask_scalar(val) if is_sensitive_column(name) else val)
    return out


def mysql_columns_with_types(conn, table: str):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, column_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    return cur.fetchall()


def ensure_postgres_table(conn, schema: str, table: str, columns, mode: str):
    with conn.cursor() as cur:
        if mode == "overwrite":
            cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                )
            )
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.{} ({})"
            ).format(
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.SQL(", ").join(
                    [sql.SQL("{} {}").format(sql.Identifier(c[0]), sql.SQL(c[1])) for c in columns]
                ),
            )
        )


def mysql_primary_keys(conn, table: str):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
         AND tc.table_name = kcu.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = DATABASE()
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def get_source_row_count(source: dict, table: str, source_type: Optional[str] = None, mysql_conn=None):
    source_type = (source_type or source.get("type") or "postgres").lower()
    schema, name = parse_table_name(table)

    if source_type == "mysql":
        if mysql_conn is None:
            import pymysql  # type: ignore
            mysql_conn = pymysql.connect(
                host=source.get("host") or "localhost",
                port=int(source.get("port") or 3306),
                user=source.get("user"),
                password=source.get("password"),
                database=source.get("db"),
                connect_timeout=5,
            )
            close_after = True
        else:
            close_after = False
        try:
            cur = mysql_conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM `{name}`")
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            if close_after:
                mysql_conn.close()

    if source_type == "oracle":
        import oracledb  # type: ignore

        host = source.get("host")
        port = source.get("port")
        db = source.get("db")
        user = source.get("user")
        password = source.get("password")
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        owner = (schema or user).upper()
        table_name = name.upper()
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {owner}.{table_name}")
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore

        host = source.get("host")
        port = source.get("port")
        db = source.get("db")
        user = source.get("user")
        password = source.get("password")
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        schema_name = (schema or user).upper()
        table_name = name.upper()
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {schema_name}.{table_name}")
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    # postgres default
    conn = get_postgres_conn(source)
    try:
        schema_name = schema or "public"
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(name),
                )
            )
            row = cur.fetchone()
            return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def estimate_total_rows(source: dict, tables: list, source_type: str, mysql_conn=None):
    total = 0
    for table in tables:
        try:
            total += get_source_row_count(source, table, source_type=source_type, mysql_conn=mysql_conn)
        except Exception:
            continue
    return int(total)


def build_migration_stages(schema_done=False, data_done=False, validation_done=False, cutover_done=False):
    return [
        {"name": "Schema", "status": "done" if schema_done else "pending"},
        {"name": "Data", "status": "done" if data_done else "pending"},
        {"name": "Validation", "status": "done" if validation_done else "pending"},
        {"name": "Cutover", "status": "done" if cutover_done else "pending"},
    ]


def execute_migration_job(run: dict, payload: dict):
    run_id = str(payload.get("_run_id") or run.get("active_migration_run_id") or uuid.uuid4().hex[:12])
    run["active_migration_run_id"] = run_id
    source = payload.get("source") or {}
    source_type = (source.get("type") or "postgres").lower()
    tables = list(payload.get("tables") or [])
    mode = payload.get("mode") or "upsert"
    target_schema = payload.get("target_schema") or "public"
    dry_run = bool(payload.get("dry_run"))
    masking = bool(payload.get("masking"))
    rollback = bool(payload.get("rollback"))
    start_stage = str(payload.get("start_stage") or "schema").lower()
    if start_stage not in {"schema", "data", "validation", "cutover"}:
        start_stage = "schema"

    conn_src = None
    conn_tgt = None
    summary = []
    total_rows_loaded = 0
    processed_tables = 0
    processed_rows = 0
    start_ts = time.monotonic()
    total_tables = max(len(tables), 1)
    cancel_marker = "__MIGRATION_CANCELLED__"
    stage_order = {"schema": 0, "data": 1, "validation": 2, "cutover": 3}
    start_idx = stage_order.get(start_stage, 0)
    append_migration_event(run, f"Миграция стартовала. Таблиц: {len(tables)}. Режим: {mode}. start_stage={start_stage}.")

    def check_cancel():
        if is_migration_cancel_requested(run):
            append_migration_event(run, "Получен запрос на отмену. Останавливаем миграцию...", level="warn")
            set_migration_progress(
                run,
                state="running",
                stage="Cancelling",
                message="Отмена миграции...",
                eta_sec=None,
            )
            raise RuntimeError(cancel_marker)

    def update_data_progress(table_name: str, table_seen_rows: int, table_total_rows: int = 0):
        elapsed = max(time.monotonic() - start_ts, 0.001)
        rps = processed_rows / elapsed if processed_rows > 0 else 0.0
        total_rows = int(run.get("migration_progress", {}).get("total_rows") or 0)
        if total_rows > 0:
            ratio = min(processed_rows / max(total_rows, 1), 1.0)
            percent = 15 + int(ratio * 75)
            eta = int((total_rows - processed_rows) / rps) if rps > 0 and total_rows > processed_rows else 0
            eta_val = eta if eta > 0 else None
        else:
            table_fraction = min(table_seen_rows / max(table_total_rows, 1), 1.0) if table_total_rows > 0 else 0.0
            table_ratio = min((processed_tables + table_fraction) / total_tables, 1.0)
            percent = 15 + int(table_ratio * 75)
            eta_val = None
        set_migration_progress(
            run,
            state="running",
            stage="Data",
            percent=min(max(percent, 15), 95),
            current_table=table_name,
            processed_tables=processed_tables,
            total_tables=total_tables,
            processed_rows=processed_rows,
            rows_per_sec=round(rps, 2),
            eta_sec=eta_val,
            message=f"Загрузка данных: {table_name}",
        )

    try:
        check_cancel()
        if source_type == "mysql":
            import pymysql  # type: ignore

            conn_src = pymysql.connect(
                host=source.get("host") or "localhost",
                port=int(source.get("port") or 3306),
                user=source.get("user"),
                password=source.get("password"),
                database=source.get("db"),
                connect_timeout=10,
            )
        conn_tgt = get_conn()
        conn_tgt.autocommit = False

        total_rows_estimate = estimate_total_rows(source, tables, source_type, mysql_conn=conn_src)
        set_migration_progress(run, total_rows=total_rows_estimate)
        append_migration_event(
            run,
            f"Оценка объёма завершена: ~{total_rows_estimate} строк в {len(tables)} таблицах.",
        )

        # 1) Schema phase
        schema_done = start_idx > 0
        data_done = start_idx > 1
        validation_done = start_idx > 2
        cutover_done = False
        run["migration_stages"] = build_migration_stages(
            schema_done=schema_done,
            data_done=data_done,
            validation_done=validation_done,
            cutover_done=cutover_done,
        )
        update_migration_history(run, run_id, stages=list(run["migration_stages"]), summary=list(summary))

        if start_idx <= 0:
            for idx, table in enumerate(tables, start=1):
                check_cancel()
                append_migration_event(run, "Подготовка схемы таблицы.", table=table)
                set_migration_progress(
                    run,
                    state="running",
                    stage="Schema",
                    percent=min(14, max(1, int((idx - 1) / total_tables * 15))),
                    current_table=table,
                    processed_tables=processed_tables,
                    total_tables=total_tables,
                    processed_rows=processed_rows,
                    rows_per_sec=0.0,
                    eta_sec=None,
                    message=f"Подготовка схемы: {table}",
                )
                if source_type == "mysql":
                    cols = mysql_columns_with_types(conn_src, table)
                    if not cols:
                        processed_tables += 1
                        continue
                    mapped_cols = []
                    for name, data_type, column_type, char_len in cols:
                        mapped_cols.append((name, map_mysql_type(data_type, column_type, char_len)))
                    _, target_table_name = parse_table_name(table)
                    ensure_postgres_table(conn_tgt, target_schema, target_table_name, mapped_cols, mode)
                else:
                    src_cols = get_columns_with_types(source, table)
                    if not src_cols:
                        processed_tables += 1
                        continue
                    _, target_table_name = parse_table_name(table)
                    mapped_cols = [(c["name"], map_generic_type_to_pg(c["type"])) for c in src_cols]
                    ensure_postgres_table(conn_tgt, target_schema, target_table_name, mapped_cols, mode)

                set_migration_progress(
                    run,
                    percent=min(15, max(1, int(idx / total_tables * 15))),
                    current_table=table,
                    message=f"Схема готова: {table}",
                )
                append_migration_event(run, "Схема таблицы подготовлена.", table=table)

            schema_done = True
            run["migration_stages"] = build_migration_stages(
                schema_done=schema_done,
                data_done=data_done,
                validation_done=validation_done,
                cutover_done=cutover_done,
            )
            update_migration_history(
                run,
                run_id,
                stages=list(run["migration_stages"]),
                summary=list(summary),
            )
            append_migration_event(run, "Этап Schema завершён.")
        else:
            append_migration_event(run, f"Этап Schema пропущен (resume from {start_stage}).", level="warn")

        # 2) Data phase
        if start_idx <= 1:
            set_migration_progress(run, stage="Data", percent=15, message="Загрузка данных...")
            append_migration_event(run, "Старт этапа Data.")
            for table in tables:
                check_cancel()
                table_seen = 0
                table_total_rows = 0
                append_migration_event(run, "Начата загрузка данных таблицы.", table=table)
                if source_type == "mysql":
                    cols = mysql_columns_with_types(conn_src, table)
                    if not cols:
                        processed_tables += 1
                        continue
                    col_names = [c[0] for c in cols]
                    pk_cols = mysql_primary_keys(conn_src, table)
                    _, target_table_name = parse_table_name(table)
                    try:
                        table_total_rows = get_source_row_count(source, table, source_type=source_type, mysql_conn=conn_src)
                    except Exception:
                        table_total_rows = 0

                    src_cur = conn_src.cursor()
                    src_cur.execute(f"SELECT * FROM `{table}`")
                    while True:
                        check_cancel()
                        rows = src_cur.fetchmany(1000)
                        if not rows:
                            break
                        batch_size = len(rows)
                        table_seen += batch_size
                        processed_rows += batch_size
                        total_rows_loaded += batch_size

                        if not dry_run:
                            if masking:
                                rows = [mask_row(r, col_names) for r in rows]
                            insert_cols = [sql.Identifier(c) for c in col_names]
                            values = [tuple(normalize_value(v) for v in r) for r in rows]
                            if mode in {"overwrite", "append"} or not pk_cols:
                                extras.execute_values(
                                    conn_tgt.cursor(),
                                    sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                    ),
                                    values,
                                    page_size=1000,
                                )
                            else:
                                conflict_cols = sql.SQL(", ").join([sql.Identifier(c) for c in pk_cols])
                                update_cols = [c for c in col_names if c not in pk_cols]
                                if update_cols:
                                    update_expr = sql.SQL(", ").join(
                                        [
                                            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                                            for c in update_cols
                                        ]
                                    )
                                    query = sql.SQL(
                                        "INSERT INTO {}.{} ({}) VALUES %s ON CONFLICT ({}) DO UPDATE SET {}"
                                    ).format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                        conflict_cols,
                                        update_expr,
                                    )
                                else:
                                    query = sql.SQL(
                                        "INSERT INTO {}.{} ({}) VALUES %s ON CONFLICT ({}) DO NOTHING"
                                    ).format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                        conflict_cols,
                                    )
                                extras.execute_values(conn_tgt.cursor(), query, values, page_size=1000)
                        update_data_progress(table, table_seen, table_total_rows)
                else:
                    src_cols = get_columns_with_types(source, table)
                    if not src_cols:
                        processed_tables += 1
                        continue
                    _, target_table_name = parse_table_name(table)
                    col_names = [c["name"] for c in src_cols]
                    pk_cols = get_primary_key_columns(source, table)
                    _, rows = get_external_preview(source, table, limit=None)
                    if not rows:
                        processed_tables += 1
                        continue
                    table_total_rows = len(rows)
                    for i in range(0, len(rows), 1000):
                        check_cancel()
                        batch = rows[i : i + 1000]
                        batch_size = len(batch)
                        table_seen += batch_size
                        processed_rows += batch_size
                        total_rows_loaded += batch_size

                        if not dry_run:
                            if masking:
                                batch = [mask_row(r, col_names) for r in batch]
                            insert_cols = [sql.Identifier(c) for c in col_names]
                            values = [tuple(normalize_value(v) for v in r) for r in batch]
                            if mode in {"overwrite", "append"} or not pk_cols:
                                extras.execute_values(
                                    conn_tgt.cursor(),
                                    sql.SQL("INSERT INTO {}.{} ({}) VALUES %s").format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                    ),
                                    values,
                                    page_size=1000,
                                )
                            else:
                                conflict_cols = sql.SQL(", ").join([sql.Identifier(c) for c in pk_cols])
                                update_cols = [c for c in col_names if c not in pk_cols]
                                if update_cols:
                                    update_expr = sql.SQL(", ").join(
                                        [
                                            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                                            for c in update_cols
                                        ]
                                    )
                                    query = sql.SQL(
                                        "INSERT INTO {}.{} ({}) VALUES %s ON CONFLICT ({}) DO UPDATE SET {}"
                                    ).format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                        conflict_cols,
                                        update_expr,
                                    )
                                else:
                                    query = sql.SQL(
                                        "INSERT INTO {}.{} ({}) VALUES %s ON CONFLICT ({}) DO NOTHING"
                                    ).format(
                                        sql.Identifier(target_schema),
                                        sql.Identifier(target_table_name),
                                        sql.SQL(", ").join(insert_cols),
                                        conflict_cols,
                                    )
                                extras.execute_values(conn_tgt.cursor(), query, values, page_size=1000)
                        update_data_progress(table, table_seen, table_total_rows)

                processed_tables += 1
                summary.append({"table": table, "rows": table_seen, "mode": mode})
                set_migration_progress(run, processed_tables=processed_tables, message=f"Таблица загружена: {table}")
                append_migration_event(run, f"Загрузка завершена: {table_seen} строк.", table=table)

            if dry_run or rollback:
                conn_tgt.rollback()
                append_migration_event(run, "Транзакция откатана (dry-run/rollback).", level="warn")
            else:
                conn_tgt.commit()
                append_migration_event(run, "Транзакция зафиксирована (commit).")

            data_done = True
            run["migration_stages"] = build_migration_stages(
                schema_done=schema_done,
                data_done=data_done,
                validation_done=validation_done,
                cutover_done=cutover_done,
            )
            update_migration_history(run, run_id, stages=list(run["migration_stages"]), summary=list(summary))
        else:
            append_migration_event(run, f"Этап Data пропущен (resume from {start_stage}).", level="warn")

        # 3) Validation phase
        if start_idx <= 2:
            set_migration_progress(
                run,
                stage="Validation",
                percent=96,
                message="Выполнение валидации после миграции...",
                eta_sec=None,
            )
            gate_table = tables[0] if tables else (run.get("table") or "")
            try:
                gate_result = compute_quality_gate(
                    source=source,
                    table=gate_table,
                    file_id=payload.get("file_id"),
                    target_table=run.get("table"),
                )
                run["migration_gate"] = gate_result
                append_migration_event(run, f"Validation/Gate: {gate_result.get('status')} (score={gate_result.get('score')}).")
            except Exception as gate_exc:
                append_migration_event(run, f"Validation warning: {friendly_backend_error(gate_exc)}", level="warn")
            validation_done = True
            run["migration_stages"] = build_migration_stages(
                schema_done=schema_done,
                data_done=data_done,
                validation_done=validation_done,
                cutover_done=cutover_done,
            )
            update_migration_history(run, run_id, stages=list(run["migration_stages"]), summary=list(summary))
        else:
            append_migration_event(run, f"Этап Validation пропущен (resume from {start_stage}).", level="warn")

        # 4) Cutover mark
        cutover_done = True
        run["migration_stages"] = build_migration_stages(
            schema_done=schema_done,
            data_done=data_done,
            validation_done=validation_done,
            cutover_done=cutover_done,
        )

        output = (
            f"{source_type.upper()} → Postgres: {len(summary)} таблиц, {total_rows_loaded} строк "
            f"(mode={mode}, start_stage={start_stage}, dry_run={'on' if dry_run else 'off'}, "
            f"masking={'on' if masking else 'off'}, rollback={'on' if rollback else 'off'})"
        )
        run["migration_output"] = output
        run["migration_summary"] = summary
        update_migration_history(
            run,
            run_id,
            output=output,
            summary=list(summary),
            stages=list(run["migration_stages"]),
        )
        set_migration_progress(
            run,
            state="completed",
            stage="Done",
            percent=100,
            message=output,
            current_table="",
            processed_tables=processed_tables,
            total_tables=total_tables,
            processed_rows=processed_rows,
            rows_per_sec=round(processed_rows / max(time.monotonic() - start_ts, 0.001), 2),
            eta_sec=0,
            finished_at=datetime.utcnow().isoformat(),
        )
        append_migration_event(run, "Миграция успешно завершена.", level="success")
    except Exception as exc:
        if conn_tgt is not None:
            try:
                conn_tgt.rollback()
            except Exception:
                pass
        if str(exc) == cancel_marker:
            run["migration_output"] = "Миграция отменена пользователем."
            run["migration_summary"] = summary
            update_migration_history(
                run,
                run_id,
                output=run["migration_output"],
                summary=list(summary),
                stages=list(run.get("migration_stages") or []),
            )
            set_migration_progress(
                run,
                state="cancelled",
                stage="Cancelled",
                percent=max(int(run.get("migration_progress", {}).get("percent") or 0), 1),
                message="Миграция отменена пользователем.",
                finished_at=datetime.utcnow().isoformat(),
                eta_sec=None,
            )
            append_migration_event(run, "Миграция отменена пользователем.", level="warn")
        else:
            err_msg = friendly_backend_error(exc)
            run["migration_output"] = f"Ошибка миграции: {err_msg}"
            run["migration_summary"] = summary
            update_migration_history(
                run,
                run_id,
                output=run["migration_output"],
                summary=list(summary),
                stages=list(run.get("migration_stages") or []),
            )
            set_migration_progress(
                run,
                state="error",
                stage="Error",
                message=err_msg,
                finished_at=datetime.utcnow().isoformat(),
            )
            append_migration_event(run, f"Ошибка миграции: {err_msg}", level="error")
    finally:
        run["migration_cancel_requested"] = False
        run["migration_job_thread"] = None
        if run.get("active_migration_run_id") == run_id:
            run["active_migration_run_id"] = None
            run["active_migration_source_type"] = None
            run["active_migration_mode"] = None
            run["active_migration_tables"] = []
            run["active_migration_start_stage"] = None
            run["active_migration_retry_of"] = None
        if conn_src is not None:
            try:
                conn_src.close()
            except Exception:
                pass
        if conn_tgt is not None:
            try:
                conn_tgt.close()
            except Exception:
                pass


def build_recommendations(added, missing, mismatched):
    recs = []
    for col in added:
        recs.append(f"Добавить колонку '{col['column']}' в target с типом {col['source_type']}.")
    for col in missing:
        recs.append(f"Колонка '{col['column']}' есть в target, отсутствует в источнике — удалить/игнорировать или заполнить default.")
    for item in mismatched:
        col = item["column"]
        src = item["source"]
        tgt = item["target"]
        sc = type_category(src)
        tc = type_category(tgt)
        if sc == tc:
            recs.append(f"{col}: типы похожи ({src} → {tgt}). Можно привести CAST.")
        elif sc == "string" and tc == "numeric":
            recs.append(f"{col}: строка → число. Проверь, что значения числовые, затем CAST.")
        elif sc == "numeric" and tc == "string":
            recs.append(f"{col}: число → строка. Безопасно CAST в text/varchar.")
        elif sc == "string" and tc == "date":
            recs.append(f"{col}: строка → дата. Используй TO_DATE/STR_TO_DATE.")
        elif sc == "date" and tc == "string":
            recs.append(f"{col}: дата → строка. CAST в text/varchar.")
        elif sc == "bool" and tc == "numeric":
            recs.append(f"{col}: bool → число. Правило: TRUE=1, FALSE=0.")
        elif sc == "numeric" and tc == "bool":
            recs.append(f"{col}: число → bool. Правило: 0=FALSE, 1=TRUE.")
        else:
            recs.append(f"{col}: несоответствие типов ({src} → {tgt}) — требуется ручная проверка.")
    return recs


def build_recommendation_actions(target_schema: str, target_table: str, added, missing, mismatched):
    actions = []
    idx = 0
    for col in added:
        idx += 1
        column = str(col.get("column") or "").strip()
        src_type = str(col.get("source_type") or "")
        if not column:
            continue
        mapped_type = map_generic_type_to_pg(src_type)
        sql_text = f'ALTER TABLE "{target_schema}"."{target_table}" ADD COLUMN IF NOT EXISTS "{column}" {mapped_type};'
        actions.append(
            {
                "id": f"add_{idx}_{column}",
                "kind": "add_column",
                "column": column,
                "source_type": src_type,
                "target_type": mapped_type,
                "sql": sql_text,
                "label": f"Добавить колонку {column}",
                "risk": "low",
                "applyable": True,
            }
        )

    for item in mismatched:
        idx += 1
        column = str(item.get("column") or "").strip()
        src_type = str(item.get("source") or "")
        tgt_type = str(item.get("target") or "")
        if not column:
            continue
        mapped_type = map_generic_type_to_pg(src_type)
        sql_text = (
            f'ALTER TABLE "{target_schema}"."{target_table}" '
            f'ALTER COLUMN "{column}" TYPE {mapped_type} USING "{column}"::{mapped_type};'
        )
        actions.append(
            {
                "id": f"alter_{idx}_{column}",
                "kind": "alter_type",
                "column": column,
                "source_type": src_type,
                "target_type": tgt_type,
                "to_type": mapped_type,
                "sql": sql_text,
                "label": f"Привести тип {column} -> {mapped_type}",
                "risk": "medium",
                "applyable": True,
            }
        )

    for col in missing:
        idx += 1
        column = str(col.get("column") or "").strip()
        if not column:
            continue
        actions.append(
            {
                "id": f"review_{idx}_{column}",
                "kind": "review_missing",
                "column": column,
                "target_type": str(col.get("target_type") or ""),
                "sql": "",
                "label": f"Проверить лишнюю колонку {column}",
                "risk": "low",
                "applyable": False,
            }
        )
    return actions


def resolve_gate_target_table(file_id: Optional[str], explicit_target: Optional[str]):
    if explicit_target:
        return explicit_target
    if not file_id:
        return None
    run = RUNS.get(file_id)
    if run and run.get("table"):
        return run.get("table")
    return None


def compute_quality_gate(source: dict, table: str, file_id: Optional[str] = None, target_table: Optional[str] = None):
    reasons = []
    source_type = (source or {}).get("type") or "postgres"

    columns = get_columns_for_source(source or {}, table)
    pk_cols = get_primary_key_columns(source or {}, table)
    if not pk_cols:
        fallback = next((c for c in columns if c.lower() == "r_object_id"), None)
        if fallback:
            pk_cols = [fallback]
        elif columns:
            pk_cols = [columns[0]]

    total_rows, null_key, duplicate_rows = get_table_counts(source or {}, table, pk_cols)
    null_top = get_null_top(source or {}, table, columns) if columns else []

    if null_key > 0:
        reasons.append(
            {
                "level": "BLOCK",
                "code": "null_key",
                "message": f"Ключевые поля содержат NULL ({null_key}).",
                "value": null_key,
            }
        )
    if duplicate_rows > 0:
        reasons.append(
            {
                "level": "BLOCK",
                "code": "duplicate_key",
                "message": f"Найдены дубликаты по ключу ({duplicate_rows}).",
                "value": duplicate_rows,
            }
        )

    if any((item.get("nulls") or 0) > 0 for item in null_top[:5]):
        reasons.append(
            {
                "level": "WARN",
                "code": "null_density",
                "message": "В таблице есть поля с высокой долей пустых значений.",
                "value": sum(1 for item in null_top[:5] if (item.get("nulls") or 0) > 0),
            }
        )

    resolved_target = resolve_gate_target_table(file_id, target_table)
    conflict_summary = None
    schema_summary = None

    if resolved_target:
        schema, name = resolve_table({"table": resolved_target})

        src_columns, src_rows = fetch_source_preview(source or {}, table, limit=200)
        if src_columns and src_rows:
            key_col = pk_cols[0] if pk_cols else src_columns[0]
            source_map = {}
            keys = []
            for row in src_rows:
                row_map = {src_columns[i]: normalize_value(row[i]) for i in range(len(src_columns))}
                key_val = row_map.get(key_col)
                if key_val is None:
                    continue
                keys.append(key_val)
                source_map[key_val] = row_map
            overlap = src_columns[:]
            target_conn = get_conn()
            try:
                target_columns = [c["name"] for c in get_columns(target_conn, schema, name)]
            finally:
                target_conn.close()
            overlap = [c for c in overlap if c in target_columns]
            if key_col not in overlap:
                overlap.insert(0, key_col)
            target_rows = fetch_target_rows(schema, name, key_col, keys, overlap)
            missing_in_target = len([k for k in keys if k not in target_rows])
            mismatch_rows = 0
            for key in keys:
                if key not in target_rows:
                    continue
                src_row = source_map.get(key, {})
                tgt_row = target_rows.get(key, {})
                for col in overlap:
                    if col == key_col:
                        continue
                    if src_row.get(col) != tgt_row.get(col):
                        mismatch_rows += 1
                        break
            conflict_summary = {
                "sampled": len(keys),
                "missing_in_target": missing_in_target,
                "mismatch_rows": mismatch_rows,
                "key_column": key_col,
            }
            if mismatch_rows > 0:
                reasons.append(
                    {
                        "level": "WARN",
                        "code": "row_mismatch",
                        "message": f"Найдены несовпадающие строки source/target ({mismatch_rows}).",
                        "value": mismatch_rows,
                    }
                )
            if missing_in_target > 0:
                reasons.append(
                    {
                        "level": "WARN",
                        "code": "missing_in_target",
                        "message": f"Часть ключей отсутствует в target ({missing_in_target}).",
                        "value": missing_in_target,
                    }
                )

        src_cols = get_columns_with_types(source or {}, table)
        target_conn = get_conn()
        try:
            tgt_cols = get_columns(target_conn, schema, name)
        finally:
            target_conn.close()
        src_map = {c["name"]: c["type"] for c in src_cols}
        tgt_map = {c["name"]: c["type"] for c in tgt_cols}
        added = [{"column": c, "source_type": src_map[c]} for c in src_map.keys() if c not in tgt_map]
        missing = [{"column": c, "target_type": tgt_map[c]} for c in tgt_map.keys() if c not in src_map]
        mismatched = []
        for col in src_map.keys():
            if col in tgt_map and str(src_map[col]).lower() != str(tgt_map[col]).lower():
                mismatched.append({"column": col, "source": src_map[col], "target": tgt_map[col]})
        schema_summary = {
            "added_columns": len(added),
            "missing_columns": len(missing),
            "type_mismatches": len(mismatched),
        }
        if added or missing or mismatched:
            reasons.append(
                {
                    "level": "WARN",
                    "code": "schema_diff",
                    "message": (
                        f"Schema diff: +{len(added)} / -{len(missing)} / "
                        f"type_mismatch={len(mismatched)}."
                    ),
                    "value": len(added) + len(missing) + len(mismatched),
                }
            )
        recs = build_recommendations(added, missing, mismatched)
    else:
        recs = []
        reasons.append(
            {
                "level": "WARN",
                "code": "target_table_missing",
                "message": "Не определена целевая таблица для полного gate-анализа.",
                "value": None,
            }
        )

    if file_id:
        run = RUNS.get(file_id)
        step2 = run.get("step2") if run else []
        unresolved = 0
        if step2:
            unresolved = sum(
                1
                for row in step2
                if str((row or {}).get("db_column") or "").startswith("(")
            )
        if unresolved > 0:
            reasons.append(
                {
                    "level": "WARN",
                    "code": "mapping_unresolved",
                    "message": f"Есть несопоставленные поля в mapping ({unresolved}).",
                    "value": unresolved,
                }
            )

    block_count = sum(1 for r in reasons if r.get("level") == "BLOCK")
    warn_count = sum(1 for r in reasons if r.get("level") == "WARN")
    if block_count:
        status = "BLOCK"
    elif warn_count:
        status = "WARN"
    else:
        status = "PASS"

    score = max(0, 100 - block_count * 45 - warn_count * 12)
    return {
        "status": status,
        "score": score,
        "source_type": source_type,
        "table": table,
        "target_table": resolved_target,
        "checks": {
            "validation": {
                "key_columns": pk_cols,
                "total_rows": total_rows,
                "null_key": null_key,
                "duplicate_rows": duplicate_rows,
                "null_top": null_top,
            },
            "conflicts": conflict_summary,
            "schema_diff": schema_summary,
            "recommendations_count": len(recs),
        },
        "reasons": reasons,
        "summary": (
            "Критические ошибки качества найдены. Миграция должна быть остановлена."
            if status == "BLOCK"
            else "Есть предупреждения. Рекомендуется исправить до запуска миграции."
            if status == "WARN"
            else "Проверки пройдены. Можно запускать миграцию."
        ),
    }


def get_table_counts(source: dict, table: str, key_cols):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    def build_null_where(cols, quote):
        return " OR ".join([f"{quote}{c}{quote} IS NULL" for c in cols])

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            cur.execute(f"SELECT COUNT(*) FROM {owner}.{table_name}")
            total = int(cur.fetchone()[0] or 0)
            nulls = 0
            if key_cols:
                where = build_null_where([c.upper() for c in key_cols], "")
                cur.execute(f"SELECT COUNT(*) FROM {owner}.{table_name} WHERE {where}")
                nulls = int(cur.fetchone()[0] or 0)
            duplicates = 0
            if key_cols:
                cols = ", ".join([c.upper() for c in key_cols])
                cur.execute(
                    f"SELECT SUM(cnt) - COUNT(*) FROM (SELECT COUNT(*) cnt FROM {owner}.{table_name} GROUP BY {cols})"
                )
                duplicates = int(cur.fetchone()[0] or 0)
        return total, nulls, duplicates

    if source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM `{name}`")
            total = int(cur.fetchone()[0] or 0)
            nulls = 0
            duplicates = 0
            if key_cols:
                where = build_null_where(key_cols, "`")
                cur.execute(f"SELECT COUNT(*) FROM `{name}` WHERE {where}")
                nulls = int(cur.fetchone()[0] or 0)
                cols = ", ".join([f"`{c}`" for c in key_cols])
                cur.execute(
                    f"SELECT SUM(cnt) - COUNT(*) FROM (SELECT COUNT(*) cnt FROM `{name}` GROUP BY {cols}) t"
                )
                duplicates = int(cur.fetchone()[0] or 0)
        finally:
            conn.close()
        return total, nulls, duplicates

    if source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            cur.execute(f"SELECT COUNT(*) FROM {schema_name}.{table_name}")
            total = int(cur.fetchone()[0] or 0)
            nulls = 0
            duplicates = 0
            if key_cols:
                where = build_null_where([c.upper() for c in key_cols], "")
                cur.execute(f"SELECT COUNT(*) FROM {schema_name}.{table_name} WHERE {where}")
                nulls = int(cur.fetchone()[0] or 0)
                cols = ", ".join([c.upper() for c in key_cols])
                cur.execute(
                    f"SELECT SUM(cnt) - COUNT(*) FROM (SELECT COUNT(*) cnt FROM {schema_name}.{table_name} GROUP BY {cols})"
                )
                duplicates = int(cur.fetchone()[0] or 0)
        finally:
            conn.close()
        return total, nulls, duplicates

    # postgres
    conn = get_postgres_conn(source)
    try:
        with conn.cursor() as cur:
            schema_name = schema or "public"
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier(schema_name),
                    sql.Identifier(name),
                )
            )
            total = int(cur.fetchone()[0] or 0)
            nulls = 0
            duplicates = 0
            if key_cols:
                where = sql.SQL(" OR ").join(
                    [sql.SQL("{} IS NULL").format(sql.Identifier(c)) for c in key_cols]
                )
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE ").format(
                        sql.Identifier(schema_name),
                        sql.Identifier(name),
                    ) + where
                )
                nulls = int(cur.fetchone()[0] or 0)
                group_cols = sql.SQL(", ").join([sql.Identifier(c) for c in key_cols])
                cur.execute(
                    sql.SQL(
                        "SELECT SUM(cnt) - COUNT(*) FROM (SELECT COUNT(*) cnt FROM {}.{} GROUP BY {}) t"
                    ).format(
                        sql.Identifier(schema_name),
                        sql.Identifier(name),
                        group_cols,
                    )
                )
                duplicates = int(cur.fetchone()[0] or 0)
        return total, nulls, duplicates
    finally:
        conn.close()


def get_null_top(source: dict, table: str, columns):
    source_type = (source.get("type") or "postgres").lower()
    host = source.get("host")
    port = source.get("port")
    db = source.get("db")
    user = source.get("user")
    password = source.get("password")
    schema, name = parse_table_name(table)

    cols = columns[:25]
    results = []

    if source_type == "oracle":
        import oracledb  # type: ignore
        dsn = oracledb.makedsn(host, port or 1521, service_name=db) if host else db
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            owner = (schema or user).upper()
            table_name = name.upper()
            for col in cols:
                col_name = col.upper()
                cur.execute(f"SELECT COUNT(*) FROM {owner}.{table_name} WHERE {col_name} IS NULL")
                results.append((col, int(cur.fetchone()[0] or 0)))

    elif source_type == "mysql":
        import pymysql  # type: ignore
        conn = pymysql.connect(
            host=host or "localhost",
            port=int(port or 3306),
            user=user,
            password=password,
            database=db,
            connect_timeout=5,
        )
        try:
            cur = conn.cursor()
            for col in cols:
                cur.execute(f"SELECT COUNT(*) FROM `{name}` WHERE `{col}` IS NULL")
                results.append((col, int(cur.fetchone()[0] or 0)))
        finally:
            conn.close()

    elif source_type == "db2":
        import ibm_db_dbi  # type: ignore
        conn_str = (
            f"DATABASE={db};HOSTNAME={host};PORT={port or 50000};"
            f"PROTOCOL=TCPIP;UID={user};PWD={password};"
        )
        conn = ibm_db_dbi.connect(conn_str, "", "")
        try:
            cur = conn.cursor()
            schema_name = (schema or user).upper()
            table_name = name.upper()
            for col in cols:
                col_name = col.upper()
                cur.execute(f"SELECT COUNT(*) FROM {schema_name}.{table_name} WHERE {col_name} IS NULL")
                results.append((col, int(cur.fetchone()[0] or 0)))
        finally:
            conn.close()

    else:
        conn = get_postgres_conn(source)
        try:
            with conn.cursor() as cur:
                schema_name = schema or "public"
                for col in cols:
                    cur.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {} IS NULL").format(
                            sql.Identifier(schema_name),
                            sql.Identifier(name),
                            sql.Identifier(col),
                        )
                    )
                    results.append((col, int(cur.fetchone()[0] or 0)))
        finally:
            conn.close()

    results.sort(key=lambda r: r[1], reverse=True)
    return [{"column": r[0], "nulls": r[1]} for r in results[:5]]


def fetch_source_preview(source: dict, table: str, limit: int = 100):
    columns, rows = get_external_preview(source, table, limit=limit)
    if not columns:
        return [], []
    return columns, rows


def fetch_target_rows(schema: str, table: str, key_col: str, keys, columns):
    if not keys:
        return {}
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            col_list = [key_col] + [c for c in columns if c != key_col]
            cur.execute(
                sql.SQL("SELECT {} FROM {}.{} WHERE {} = ANY(%s)").format(
                    sql.SQL(", ").join([sql.Identifier(c) for c in col_list]),
                    sql.Identifier(schema),
                    sql.Identifier(table),
                    sql.Identifier(key_col),
                ),
                (keys,),
            )
            rows = cur.fetchall()
            result = {}
            for row in rows:
                row_map = {col_list[i]: normalize_value(row[i]) for i in range(len(col_list))}
                result[row_map[key_col]] = row_map
            return result
    finally:
        conn.close()
def resolve_table(run):
    table = run.get("table")
    if not table:
        raise HTTPException(status_code=400, detail="Run step 1 to detect table")
    if "." in table:
        schema, name = table.split(".", 1)
    else:
        schema, name = "public", table
    return schema, name


def get_columns(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]


def pick_date_column(columns):
    candidates = [
        "dsdt_creation_date",
        "r_creation_date",
        "dsdt_reg_date",
        "dsdt_date",
        "dsdt_start",
        "dsdt_end",
    ]
    col_names = [c["name"] for c in columns]
    for name in candidates:
        if name in col_names:
            return name
    for col in columns:
        if col["type"] in DATE_TYPES:
            return col["name"]
    return None


def pick_category_column(columns, keywords):
    for col in columns:
        name = col["name"]
        if any(k in name for k in keywords):
            return name
    for col in columns:
        if col["type"] == "character varying" or col["type"] == "text":
            return col["name"]
    return None


def top_counts(conn, schema, table, column, limit=8, where_sql=None, params=None):
    if not column:
        return {"labels": [], "counts": []}
    where_sql = where_sql or sql.SQL("")
    params = params or []
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT {col}::text, COUNT(*) FROM {schema}.{table}").format(
                col=sql.Identifier(column),
                schema=sql.Identifier(schema),
                table=sql.Identifier(table),
            )
            + where_sql
            + sql.SQL(" GROUP BY {col} ORDER BY COUNT(*) DESC NULLS LAST LIMIT %s").format(
                col=sql.Identifier(column)
            ),
            params + [limit],
        )
        rows = cur.fetchall()
    return {"labels": [r[0] for r in rows], "counts": [r[1] for r in rows]}


def clean_dataframe(df):
    df = df.copy()
    df = df.replace(r"^\s*$", None, regex=True)
    df = df.replace(r"(?i)^null$", None, regex=True)
    return df


def pick_file_date_column(df):
    for col in df.columns:
        lower = str(col).lower()
        if "дата" in lower or "date" in lower or "time" in lower:
            return col
    best_col = None
    best_ratio = 0.0
    for col in df.columns:
        series = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
        non_null = df[col].notna().sum()
        if non_null == 0:
            continue
        ratio = series.notna().sum() / non_null
        if ratio > best_ratio:
            best_ratio = ratio
            best_col = col
    if best_ratio >= 0.4:
        return best_col
    return None


def pick_file_category_column(df, keywords):
    for col in df.columns:
        lower = str(col).lower()
        if any(k in lower for k in keywords):
            return col
    return None


def pick_file_id_column(df):
    priority = ["r_object_id", "object_id", "id", "идентификатор", "ид", "global", "глобал"]
    for col in df.columns:
        lower = str(col).lower()
        if lower in priority:
            return col
    for col in df.columns:
        lower = str(col).lower()
        if any(p in lower for p in priority):
            return col
    return None


def top_counts_df(df, column, limit=8):
    if not column or column not in df.columns:
        return {"labels": [], "counts": []}
    counts = df[column].fillna("(null)").astype(str).value_counts().head(limit)
    return {"labels": counts.index.tolist(), "counts": counts.values.tolist()}


def apply_file_filters(df, status_col, doc_type_col, dept_col, date_col, status, doc_type, dept, date_from, date_to):
    filtered = df
    if status and status_col in filtered.columns:
        filtered = filtered[filtered[status_col] == status]
    if doc_type and doc_type_col in filtered.columns:
        filtered = filtered[filtered[doc_type_col] == doc_type]
    if dept and dept_col in filtered.columns:
        filtered = filtered[filtered[dept_col] == dept]
    if date_col and date_col in filtered.columns and (date_from or date_to):
        series = pd.to_datetime(filtered[date_col], errors="coerce", dayfirst=True)
        if date_from:
            filtered = filtered[series >= pd.to_datetime(date_from, errors="coerce")]
        if date_to:
            filtered = filtered[series <= pd.to_datetime(date_to, errors="coerce")]
    return filtered


def serialize_sample_rows(rows):
    cleaned = []
    for row in rows:
        item = {}
        for k, v in row.items():
            if isinstance(v, (datetime, date)):
                item[k] = v.isoformat()
            else:
                item[k] = "" if v is None else str(v)
        cleaned.append(item)
    return cleaned
    where_sql = where_sql or sql.SQL("")
    params = params or []
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT {col}::text, COUNT(*) FROM {schema}.{table}"
            ).format(
                col=sql.Identifier(column),
                schema=sql.Identifier(schema),
                table=sql.Identifier(table),
            )
            + where_sql
            + sql.SQL(" GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT %s").format(
                col=sql.Identifier(column)
            ),
            params + [limit],
        )
        rows = cur.fetchall()
    return {
        "labels": [r[0] if r[0] is not None else "(null)" for r in rows],
        "counts": [r[1] for r in rows],
    }


def build_where(status_col, doc_type_col, dept_col, date_col, status, doc_type, dept, date_from, date_to):
    clauses = []
    params = []
    if status and status_col:
        clauses.append(sql.SQL("{} = %s").format(sql.Identifier(status_col)))
        params.append(status)
    if doc_type and doc_type_col:
        clauses.append(sql.SQL("{} = %s").format(sql.Identifier(doc_type_col)))
        params.append(doc_type)
    if dept and dept_col:
        clauses.append(sql.SQL("{} = %s").format(sql.Identifier(dept_col)))
        params.append(dept)
    if date_from and date_col:
        clauses.append(sql.SQL("{} >= %s").format(sql.Identifier(date_col)))
        params.append(date_from)
    if date_to and date_col:
        clauses.append(sql.SQL("{} <= %s").format(sql.Identifier(date_col)))
        params.append(date_to)
    if clauses:
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses), params
    return sql.SQL(""), params


def parse_match_count(raw):
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(int(raw), 0)
        except Exception:
            return 0
    txt = str(raw).strip()
    if not txt:
        return 0
    if "/" in txt:
        txt = txt.split("/", 1)[0].strip()
    try:
        return max(int(float(txt)), 0)
    except Exception:
        return 0


def normalize_step2_rows(rows):
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        excel_col = (
            row.get("excel_column")
            or row.get("excel")
            or row.get("source_column")
            or row.get("source")
            or ""
        )
        db_col = (
            row.get("db_column")
            or row.get("db")
            or row.get("target_column")
            or row.get("target")
            or ""
        )
        excel_col = str(excel_col or "").strip()
        db_col = str(db_col or "").strip()
        if not excel_col:
            continue
        match_count = parse_match_count(row.get("match_count", row.get("matches")))
        if db_col and not db_col.startswith("(") and match_count <= 0:
            match_count = 10
        out.append(
            {
                "excel_column": excel_col,
                "db_column": db_col,
                "match_count": match_count,
            }
        )
    return out


def trim_ident(value: str, limit: int = 63):
    text = str(value or "").strip("_")
    if not text:
        return ""
    return text[:limit].strip("_")


def guess_db_column_for_excel(excel_name: str):
    norm = to_snake_name(excel_name, default="value")

    def has_any(*parts):
        return any(part in norm for part in parts)

    if has_any("id_document", "id_dokument", "identifikator_dokument", "r_object_id", "id_documenta"):
        return "r_object_id", "Документный идентификатор", 0.97
    if "global_id" in norm:
        return "i_global_id", "Глобальный ID", 0.94
    if has_any("status", "sostoyanie"):
        return "dss_status", "Статус документа", 0.9
    if has_any("vremenny", "work_number", "temporary_number"):
        return "dss_work_number", "Временный номер", 0.89
    if has_any("reg_number", "registracionny_nomer", "registration_number") and not has_any("date", "data"):
        return "dss_reg_number", "Регистрационный номер", 0.87
    if has_any("description", "kratkoe_soderzhan", "comment"):
        return "dss_description", "Текстовое описание", 0.84
    if has_any("kolichestvo_list", "number_of_page", "page_count"):
        return "dis_number_of_page", "Количество листов", 0.9
    if has_any("kolichestvo_prilozhen", "number_of_appendix", "appendix_count"):
        return "dis_number_of_appendix", "Количество приложений", 0.9

    if has_any("date", "data", "datetime", "vremya", "time"):
        tail = re.sub(r"^(date|data|datetime|vremya|time)_?", "", norm).strip("_") or "value"
        return f"dsdt_{trim_ident(tail, 58)}", "Похоже на дату/время", 0.82

    if has_any("flag", "priznak", "is_", "srochn", "important", "urgent", "deleted", "udalenn"):
        tail = re.sub(r"^(flag|priznak|is)_?", "", norm).strip("_") or norm
        return f"dsb_{trim_ident(tail, 59)}", "Похоже на boolean/флаг", 0.84

    if norm.endswith("_id") or norm.startswith("id_"):
        tail = re.sub(r"^id_?", "", norm).strip("_") or "value"
        return f"dsid_{trim_ident(tail, 58)}", "Похоже на справочник/ID ссылку", 0.78

    return f"dss_{trim_ident(norm, 59)}", "Текстовое поле по умолчанию", 0.62


def compute_mapping_insights(rows, limit: int = 12):
    mapping = normalize_step2_rows(rows)
    total = len(mapping)
    mapped_rows = [r for r in mapping if r["db_column"] and not r["db_column"].startswith("(")]
    unresolved_rows = [r for r in mapping if not r["db_column"] or r["db_column"].startswith("(")]
    low_conf = [r for r in mapped_rows if 0 < int(r.get("match_count", 0)) < 7]

    duplicate_targets = {}
    for row in mapped_rows:
        key = row["db_column"].strip().lower()
        duplicate_targets.setdefault(key, []).append(row["excel_column"])
    duplicate_targets = {
        k: v for k, v in duplicate_targets.items() if len(v) > 1
    }

    suggestions = []
    for row in unresolved_rows:
        candidate, reason, confidence = guess_db_column_for_excel(row["excel_column"])
        suggestions.append(
            {
                "excel_column": row["excel_column"],
                "current_db_column": row["db_column"],
                "suggested_db_column": candidate,
                "reason": reason,
                "confidence": round(float(confidence), 2),
                "kind": "unresolved",
            }
        )

    for row in low_conf:
        candidate, reason, confidence = guess_db_column_for_excel(row["excel_column"])
        if candidate and candidate != row["db_column"]:
            suggestions.append(
                {
                    "excel_column": row["excel_column"],
                    "current_db_column": row["db_column"],
                    "suggested_db_column": candidate,
                    "reason": f"Низкий score: {row.get('match_count', 0)}/10. {reason}",
                    "confidence": round(float(confidence), 2),
                    "kind": "low_confidence",
                }
            )

    suggestions = sorted(
        suggestions,
        key=lambda item: (item.get("kind") != "unresolved", -float(item.get("confidence", 0.0))),
    )[: max(1, min(int(limit or 12), 60))]

    avg_match = 0.0
    if mapped_rows:
        avg_match = sum(int(r.get("match_count", 0)) for r in mapped_rows) / max(len(mapped_rows), 1)

    match_ratio = round((len(mapped_rows) / max(total, 1)) * 100, 1) if total else 0.0
    unresolved_cols = [r["excel_column"] for r in unresolved_rows]
    duplicate_items = [
        {"db_column": key, "excel_columns": cols}
        for key, cols in duplicate_targets.items()
    ]

    top_actions = []
    if unresolved_rows:
        top_actions.append(f"Закрыть unresolved колонки: {len(unresolved_rows)} шт.")
    if duplicate_items:
        top_actions.append(f"Проверить дубли target-полей: {len(duplicate_items)} шт.")
    if low_conf:
        top_actions.append(f"Проверить низкую уверенность сопоставления: {len(low_conf)} шт.")
    if not top_actions and total:
        top_actions.append("Маппинг выглядит стабильным. Можно переходить к precheck миграции.")
    if not total:
        top_actions.append("Сначала запусти Step 1, чтобы получить маппинг.")

    return {
        "summary": {
            "total_columns": total,
            "mapped_columns": len(mapped_rows),
            "unresolved_columns": len(unresolved_rows),
            "low_confidence_columns": len(low_conf),
            "duplicate_target_columns": len(duplicate_items),
            "match_ratio_pct": match_ratio,
            "avg_match_score": round(avg_match, 2),
        },
        "unresolved_columns": unresolved_cols,
        "duplicate_targets": duplicate_items,
        "suggestions": suggestions,
        "top_actions": top_actions,
    }


def build_ai_context(file_id: Optional[str], sheet_name: Optional[str], include_mapping: bool, include_migration: bool):
    ctx = {
        "generated_at": datetime.utcnow().isoformat(),
        "file_id": file_id or "",
    }
    if not file_id:
        return ctx

    run = RUNS.get(file_id)
    if not run:
        ctx["missing_run"] = True
        return ctx

    ctx["file_name"] = run.get("filename") or ""
    ctx["sheet_name"] = sheet_name or run.get("sheet") or ""
    ctx["best_table"] = run.get("table") or ""

    if include_mapping:
        step2_rows = normalize_step2_rows(run.get("step2") or [])
        ctx["mapping_insights"] = compute_mapping_insights(step2_rows, limit=10)

    if include_migration:
        progress = run.get("migration_progress") or default_migration_progress()
        gate = run.get("migration_gate") or {}
        ctx["migration"] = {
            "state": progress.get("state"),
            "stage": progress.get("stage"),
            "percent": progress.get("percent"),
            "processed_rows": progress.get("processed_rows"),
            "total_rows": progress.get("total_rows"),
            "processed_tables": progress.get("processed_tables"),
            "total_tables": progress.get("total_tables"),
            "gate_status": gate.get("status"),
            "gate_score": gate.get("score"),
            "gate_summary": gate.get("summary"),
        }

    excel_path = Path(run.get("path") or "")
    if excel_path.exists():
        try:
            chosen_sheet = sheet_name or run.get("sheet")
            if chosen_sheet:
                df = pd.read_excel(excel_path, sheet_name=chosen_sheet, dtype=str)
            else:
                df = pd.read_excel(excel_path, sheet_name=0, dtype=str)
            df = clean_dataframe(df)
            ctx["file_profile"] = {
                "rows": int(len(df)),
                "columns": int(len(df.columns)),
                "sheet": chosen_sheet or "",
                "column_names": [str(c) for c in list(df.columns)[:30]],
            }
        except Exception:
            ctx["file_profile"] = {"error": "read_failed"}

    return ctx


def parse_json_from_text(text: str):
    raw = str(text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```json\s*(\{.*?\})\s*```", raw, flags=re.S | re.I)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        fragment = raw[start : end + 1]
        try:
            return json.loads(fragment)
        except Exception:
            return None
    return None


def call_external_llm(messages):
    if not LLM_API_KEY:
        return None, "LLM API key not configured."

    payload = {
        "model": LLM_MODEL or "gpt-4o-mini",
        "temperature": 0.2,
        "messages": messages,
    }
    req = urllib.request.Request(
        LLM_BASE_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(5, LLM_TIMEOUT_SEC)) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            content = (
                (((data.get("choices") or [{}])[0].get("message") or {}).get("content"))
                or ""
            )
            if not content:
                return None, "LLM returned empty response."
            return str(content), None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = str(exc)
        return None, f"HTTP {exc.code}: {detail[:300]}"
    except Exception as exc:
        return None, str(exc)


def build_fallback_ai_response(question: str, context: dict):
    q = str(question or "").strip().lower()
    mapping = context.get("mapping_insights") or {}
    mapping_summary = mapping.get("summary") or {}
    migration = context.get("migration") or {}
    unresolved = int(mapping_summary.get("unresolved_columns") or 0)
    mapped = int(mapping_summary.get("mapped_columns") or 0)
    total = int(mapping_summary.get("total_columns") or 0)

    recommendations = list(mapping.get("top_actions") or [])
    if migration.get("gate_status") == "FAIL":
        recommendations.append("Перед миграцией закрой критичные пункты Quality Gate.")
    if unresolved and "шаг" in q:
        recommendations.append("Сначала закрой unresolved-поля вручную, затем запускай validation.")

    if any(word in q for word in ["миграц", "перенос", "run", "step 2"]):
        answer = (
            f"Текущий профиль маппинга: {mapped}/{max(total, 1)} сопоставлено. "
            f"Unresolved: {unresolved}. Рекомендую выполнить precheck (validate/conflicts/schema diff), "
            "после этого запускать миграцию."
        )
    elif any(word in q for word in ["мапп", "сопостав", "column", "колон"]):
        answer = (
            f"По маппингу сейчас {mapped}/{max(total, 1)} закрытых колонок. "
            "Для незакрытых полей используй suggestions из блока AI/BI и сохрани override."
        )
    elif any(word in q for word in ["помощ", "help", "как", "что делать"]):
        answer = (
            "Я могу помочь по платформе: запуск шага 1, разбор маппинга, precheck миграции, "
            "интерпретация schema diff и quality gate."
        )
    else:
        answer = (
            "Контекст получен. Я могу дать рекомендации по маппингу, качеству данных и готовности к миграции. "
            "Спроси, например: «что исправить перед миграцией?» или «какие поля маппинга самые рискованные?»."
        )

    actions = [
        {"id": "run_step1", "label": "Запустить Step 1"},
        {"id": "refresh_bi", "label": "Обновить BI insights"},
    ]
    return {
        "answer": answer,
        "recommendations": recommendations[:8],
        "actions": actions,
    }


@app.get("/api/profiles")
def api_list_profiles():
    rows = []
    for item in list_profiles():
        try:
            profile = load_profile(item.get("profile_id"))
        except Exception:
            profile = read_profile_file(item.get("profile_id"))
        rows.append(build_profile_summary(profile))
    return {"profiles": rows, "default_profile_id": DEFAULT_PROFILE_ID}


@app.get("/api/profiles/{profile_id}")
def api_get_profile(profile_id: str, resolved: bool = False):
    pid = sanitize_profile_id(profile_id)
    try:
        profile = load_profile(pid) if resolved else read_profile_file(pid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Profile '{pid}' not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "profile": profile,
        "summary": build_profile_summary(load_profile(pid)),
    }


@app.post("/api/profiles")
def api_save_profile(payload: CompanyProfileSaveRequest):
    profile_id = sanitize_profile_id(payload.profile_id or payload.company_name)
    try:
        saved = save_profile(
            {
                "profile_id": profile_id,
                "company_name": payload.company_name,
                "description": payload.description,
                "inherit_default": False if profile_id == DEFAULT_PROFILE_ID else bool(payload.inherit_default),
                "matching": payload.matching or {},
            }
        )
        resolved = load_profile(profile_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось сохранить профиль: {exc}")
    return {
        "status": "ok",
        "profile": saved,
        "resolved_profile": resolved,
        "summary": build_profile_summary(resolved),
    }


@app.get("/api/sheets")
def list_sheets(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    excel_path = Path(run["path"])
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="Uploaded file not found")
    try:
        import pandas as pd
        xls = pd.ExcelFile(excel_path)
        sheets = xls.sheet_names
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"sheets": sheets}


@app.post("/api/upload")
async def upload_excel(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx/.xls files supported")
    file_id = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{file_id}_{file.filename}"
    with dest.open("wb") as f:
        content = await file.read()
        f.write(content)
    RUNS[file_id] = {
        "path": str(dest),
        "json": None,
        "csv": None,
        "sheet": None,
        "profile_id": DEFAULT_PROFILE_ID,
        "filename": file.filename,
        "created_at": datetime.utcnow().isoformat(),
        "migration_progress": default_migration_progress(),
        "migration_summary": [],
        "migration_events": [],
        "migration_history": [],
        "migration_cancel_requested": False,
    }
    return {"file_id": file_id, "filename": file.filename}


@app.post("/api/run-step1")
def run_step1(payload: RunStep1Request):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    excel_path = Path(run["path"])
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="Uploaded file not found")

    profile_id = sanitize_profile_id(payload.profile_id or run.get("profile_id") or DEFAULT_PROFILE_ID)
    try:
        profile = load_profile(profile_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Профиль '{profile_id}' недоступен: {exc}")

    run["profile_id"] = profile_id
    run["profile_summary"] = build_profile_summary(profile)
    run["updated_at"] = datetime.utcnow().isoformat()

    env = build_script_pg_env(os.environ.copy())
    env["INCLUDE_SCHEMAS"] = "public"
    env["EXCEL_FILE"] = str(excel_path)
    env["MATCHING_PROFILE_ID"] = profile_id

    # Fast precheck avoids long waits and script prompts when Postgres is unavailable.
    pg_ok, pg_err = can_connect_script_pg(env)
    if not pg_ok:
        try:
            offline_result = build_offline_step1(run, payload.file_id, excel_path, payload.sheet_name)
            reason = (
                "Postgres недоступен — использован offline режим (сопоставление по структуре файла). "
                f"Причина: {friendly_backend_error(Exception(pg_err))}"
            )
            run["offline_reason"] = reason
            offline_result["warning"] = reason
            return offline_result
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Postgres precheck failed: {friendly_backend_error(Exception(pg_err))}. "
                f"Offline fallback error: {exc}",
            )

    step1_timeout = int(os.environ.get("DOCUMINO_STEP1_TIMEOUT", "300"))
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT1)],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=step1_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        detail = (
            f"Step 1 script timeout after {step1_timeout}s. "
            "Falling back to offline mode."
        )
        try:
            offline_result = build_offline_step1(run, payload.file_id, excel_path, payload.sheet_name)
            reason = (
                "Шаг 1 превысил лимит времени и был переключен в offline режим "
                f"({step1_timeout}s)."
            )
            run["offline_reason"] = reason
            offline_result["warning"] = reason
            return offline_result
        except Exception:
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            fallback_detail = summarize_script_error(stderr, stdout)
            raise HTTPException(status_code=500, detail=f"{detail} {fallback_detail}")
    if proc.returncode != 0:
        detail = summarize_script_error(proc.stderr, proc.stdout)
        try:
            offline_result = build_offline_step1(run, payload.file_id, excel_path, payload.sheet_name)
            reason = run.get("offline_reason") or "Использован offline режим."
            short_detail = (detail or "").strip()
            if short_detail and short_detail not in reason:
                reason = f"{reason} Причина: {short_detail}"
            run["offline_reason"] = reason
            offline_result["warning"] = reason
            return offline_result
        except Exception:
            raise HTTPException(status_code=500, detail=detail)

    json_path, csv_path = find_latest_mapping(excel_path)
    if not json_path:
        try:
            offline_result = build_offline_step1(run, payload.file_id, excel_path, payload.sheet_name)
            reason = "Mapping JSON not found after run. Использован offline режим."
            run["offline_reason"] = reason
            offline_result["warning"] = reason
            return offline_result
        except Exception:
            raise HTTPException(status_code=500, detail="Mapping JSON not found after run")

    sheet = load_sheet(json_path, payload.sheet_name)
    run["json"] = str(json_path)
    run["csv"] = str(csv_path) if csv_path else None
    run["sheet"] = sheet.get("sheet_name")
    run["updated_at"] = datetime.utcnow().isoformat()

    step2 = sheet.get("step2") or []
    step4 = sheet.get("step4") or {}
    table = step4.get("selection") or ""
    run["table"] = table
    run["step2"] = step2

    return {
        "sheet_name": sheet.get("sheet_name"),
        "db_name": env.get("PGDATABASE", ""),
        "table": table,
        "step2": step2,
        "profile_id": profile_id,
        "profile": build_profile_summary(profile),
        "csv_url": f"/api/download?file_id={payload.file_id}&kind=csv",
        "json_url": f"/api/download?file_id={payload.file_id}&kind=json",
    }


@app.get("/api/dashboard")
def dashboard(
    file_id: str,
    source: Optional[str] = "db",
    sheet_name: Optional[str] = None,
    status: Optional[str] = None,
    doc_type: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    if (source or "db").lower() == "file":
        excel_path = Path(run["path"])
        if not excel_path.exists():
            raise HTTPException(status_code=404, detail="Uploaded file not found")
        sheet = sheet_name or run.get("sheet")
        try:
            if sheet:
                df = pd.read_excel(excel_path, sheet_name=sheet, dtype=str)
            else:
                df = pd.read_excel(excel_path, sheet_name=0, dtype=str)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        df = clean_dataframe(df)

        date_col = pick_file_date_column(df)
        status_col = pick_file_category_column(df, ["статус", "status"])
        doc_type_col = pick_file_category_column(df, ["тип", "вид", "doctype", "doc_type", "doc"])
        dept_col = pick_file_category_column(
            df,
            ["подраздел", "департамент", "отдел", "филиал", "организац", "division", "department", "dept", "branch", "home"],
        )

        filtered = apply_file_filters(
            df, status_col, doc_type_col, dept_col, date_col, status, doc_type, department, date_from, date_to
        )
        total_rows = len(filtered)

        null_rates = []
        for col in filtered.columns:
            null_count = filtered[col].isna().sum()
            rate = (null_count / total_rows) if total_rows else 0
            null_rates.append({"column": str(col), "nulls": int(null_count), "rate": rate})
        null_rates.sort(key=lambda x: x["rate"], reverse=True)

        duplicates = {"column": "r_object_id", "count": 0}
        id_col = pick_file_id_column(filtered)
        if id_col:
            counts = filtered[id_col].dropna().astype(str).value_counts()
            duplicates = {"column": id_col, "count": int((counts > 1).sum())}

        avg_date = None
        if date_col and date_col in filtered.columns:
            series = pd.to_datetime(filtered[date_col], errors="coerce", dayfirst=True)
            series = series.dropna()
            if not series.empty:
                avg_ns = int(series.view("int64").mean())
                avg_date = pd.to_datetime(avg_ns)

        type_issues = []
        for col in filtered.columns:
            series = filtered[col]
            non_null = series.notna().sum()
            if not non_null:
                continue
            numeric_ratio = pd.to_numeric(series, errors="coerce").notna().sum() / non_null
            date_ratio = pd.to_datetime(series, errors="coerce", dayfirst=True).notna().sum() / non_null
            lower = str(col).lower()
            if ("дата" in lower or "date" in lower) and date_ratio < 0.4:
                type_issues.append({"column": str(col), "issue": "date_parse_low"})
            elif numeric_ratio > 0.9 and not any(k in lower for k in ["id", "ид", "номер", "num", "count"]):
                type_issues.append({"column": str(col), "issue": "numeric_like_text"})

        trend = {"labels": [], "counts": []}
        if date_col and date_col in filtered.columns:
            series = pd.to_datetime(filtered[date_col], errors="coerce", dayfirst=True)
            by_day = series.dropna().dt.date.value_counts().sort_index().head(30)
            trend = {
                "labels": [d.isoformat() for d in by_day.index],
                "counts": [int(v) for v in by_day.values],
            }

        sample_rows = serialize_sample_rows(filtered.head(50).to_dict(orient="records"))

        match_ratio = None
        json_path = run.get("json")
        if json_path and (run.get("sheet") or sheet_name):
            try:
                sheet = load_sheet(Path(json_path), run.get("sheet") or sheet_name)
                step2 = sheet.get("step2") or []
                if step2:
                    matched = [
                        r for r in step2
                        if r.get("db_column") and not str(r.get("db_column")).startswith("(")
                    ]
                    match_ratio = round(len(matched) / len(step2) * 100, 1)
            except Exception:
                match_ratio = None

        return {
            "source": "file",
            "table": run.get("filename", ""),
            "total_rows": total_rows,
            "avg_date": avg_date.isoformat() if hasattr(avg_date, "isoformat") and avg_date else None,
            "duplicates": duplicates,
            "null_rates": null_rates[:10],
            "type_issues": type_issues[:10],
            "trend": trend,
            "status": top_counts_df(filtered, status_col),
            "doc_types": top_counts_df(filtered, doc_type_col),
            "departments": top_counts_df(filtered, dept_col),
            "sample_rows": sample_rows,
            "match_ratio": match_ratio,
            "filters": {
                "status": {"column": status_col, "values": top_counts_df(filtered, status_col)},
                "doc_type": {"column": doc_type_col, "values": top_counts_df(filtered, doc_type_col)},
                "department": {"column": dept_col, "values": top_counts_df(filtered, dept_col)},
                "date": {"column": date_col},
            },
        }
    schema, table = resolve_table(run)
    try:
        conn = get_conn()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Postgres недоступен: {exc.__class__.__name__}. Проверь PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD.",
        ) from exc
    try:
        columns = get_columns(conn, schema, table)
        col_names = [c["name"] for c in columns]
        date_col = pick_date_column(columns)
        status_col = pick_category_column(columns, ["status"])
        doc_type_col = pick_category_column(columns, ["kind", "type", "doctype", "doc_type"])
        dept_col = pick_category_column(columns, ["dept", "org", "division", "branch", "department", "home"])

        where_sql, where_params = build_where(
            status_col, doc_type_col, dept_col, date_col, status, doc_type, department, date_from, date_to
        )

        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT COUNT(*) FROM {s}.{t}").format(
                    s=sql.Identifier(schema), t=sql.Identifier(table)
                )
                + where_sql,
                where_params,
            )
            total_rows = cur.fetchone()[0]

        # Null rate per column (top 10 worst)
        null_rates = []
        for col in col_names:
            with conn.cursor() as cur:
                base = sql.SQL("SELECT COUNT(*) FROM {s}.{t}").format(
                    s=sql.Identifier(schema), t=sql.Identifier(table)
                )
                if where_params:
                    query = base + where_sql + sql.SQL(" AND {c} IS NULL").format(c=sql.Identifier(col))
                    params = where_params
                else:
                    query = base + sql.SQL(" WHERE {c} IS NULL").format(c=sql.Identifier(col))
                    params = []
                cur.execute(query, params)
                null_count = cur.fetchone()[0]
            rate = (null_count / total_rows) if total_rows else 0
            null_rates.append({"column": col, "nulls": null_count, "rate": rate})
        null_rates.sort(key=lambda x: x["rate"], reverse=True)

        # Duplicates by r_object_id
        duplicates = {"column": "r_object_id", "count": 0}
        if "r_object_id" in col_names:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM (SELECT {c}, COUNT(*) cnt FROM {s}.{t}"
                    ).format(
                        c=sql.Identifier("r_object_id"),
                        s=sql.Identifier(schema),
                        t=sql.Identifier(table),
                    )
                    + where_sql
                    + sql.SQL(" GROUP BY {c} HAVING COUNT(*) > 1) sub").format(
                        c=sql.Identifier("r_object_id")
                    ),
                    where_params,
                )
                duplicates["count"] = cur.fetchone()[0]

        # Average date
        avg_date = None
        if date_col:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "SELECT to_timestamp(AVG(EXTRACT(EPOCH FROM {c}))) FROM {s}.{t}"
                    ).format(
                        c=sql.Identifier(date_col),
                        s=sql.Identifier(schema),
                        t=sql.Identifier(table),
                    )
                    + where_sql,
                    where_params,
                )
                avg_date = cur.fetchone()[0]

        # Type mismatch heuristic: text columns with mostly numeric values
        type_issues = []
        for col in columns:
            if col["type"] in ("character varying", "text"):
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "SELECT COUNT(*) FILTER (WHERE {c} ~ '^[0-9]+$'), COUNT(*) FILTER (WHERE {c} IS NOT NULL) FROM {s}.{t}"
                        ).format(
                            c=sql.Identifier(col["name"]),
                            s=sql.Identifier(schema),
                            t=sql.Identifier(table),
                        )
                        + where_sql,
                        where_params,
                    )
                    numeric_like, non_null = cur.fetchone()
                if non_null and numeric_like / non_null > 0.9:
                    type_issues.append({"column": col["name"], "issue": "text_looks_numeric"})

        # Trend series by date
        trend = {"labels": [], "counts": []}
        if date_col:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "SELECT DATE({c}) as d, COUNT(*) FROM {s}.{t}"
                    ).format(
                        c=sql.Identifier(date_col),
                        s=sql.Identifier(schema),
                        t=sql.Identifier(table),
                    )
                    + where_sql
                    + sql.SQL(" GROUP BY d ORDER BY d LIMIT 30"),
                    where_params,
                )
                rows = cur.fetchall()
            trend = {
                "labels": [r[0].isoformat() if r[0] else "(null)" for r in rows],
                "counts": [r[1] for r in rows],
            }

        # Sample table rows (limit 50)
        sample_rows = []
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT * FROM {s}.{t}").format(
                    s=sql.Identifier(schema), t=sql.Identifier(table)
                )
                + where_sql
                + sql.SQL(" LIMIT 50"),
                where_params,
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                sample_rows.append(dict(zip(cols, row)))

        match_ratio = None
        json_path = run.get("json")
        if json_path and run.get("sheet"):
            try:
                sheet = load_sheet(Path(json_path), run.get("sheet"))
                step2 = sheet.get("step2") or []
                if step2:
                    matched = [
                        r for r in step2
                        if r.get("db_column") and not str(r.get("db_column")).startswith("(")
                    ]
                    match_ratio = round(len(matched) / len(step2) * 100, 1)
            except Exception:
                match_ratio = None

        return {
            "table": f"{schema}.{table}",
            "total_rows": total_rows,
            "avg_date": avg_date.isoformat() if hasattr(avg_date, "isoformat") and avg_date else None,
            "duplicates": duplicates,
            "null_rates": null_rates[:10],
            "type_issues": type_issues[:10],
            "trend": trend,
            "status": top_counts(conn, schema, table, status_col, where_sql=where_sql, params=where_params),
            "doc_types": top_counts(conn, schema, table, doc_type_col, where_sql=where_sql, params=where_params),
            "departments": top_counts(conn, schema, table, dept_col, where_sql=where_sql, params=where_params),
            "sample_rows": sample_rows,
            "match_ratio": match_ratio,
            "filters": {
                "status": {"column": status_col, "values": top_counts(conn, schema, table, status_col)},
                "doc_type": {"column": doc_type_col, "values": top_counts(conn, schema, table, doc_type_col)},
                "department": {"column": dept_col, "values": top_counts(conn, schema, table, dept_col)},
                "date": {"column": date_col},
            },
        }
    finally:
        if conn:
            conn.close()


@app.get("/api/history")
def history():
    items = []
    for file_id, run in RUNS.items():
        items.append(
            {
                "file_id": file_id,
                "filename": run.get("filename", ""),
                "sheet": run.get("sheet", ""),
                "table": run.get("table", ""),
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
            }
        )
    items.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    return items


@app.get("/api/history/{file_id}")
def history_item(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    return {
        "file_id": file_id,
        "filename": run.get("filename", ""),
        "sheet_name": run.get("sheet", ""),
        "table": run.get("table", ""),
        "db_name": "offline" if run.get("offline_fallback") else resolve_default_pg_database(),
        "warning": run.get("offline_reason") if run.get("offline_fallback") else "",
        "step2": run.get("step2", []),
        "csv_url": f"/api/download?file_id={file_id}&kind=csv",
        "json_url": f"/api/download?file_id={file_id}&kind=json",
    }


@app.get("/api/schema/tables")
def schema_tables(limit: int = 200):
    try:
        conn = get_conn()
    except Exception as exc:
        offline_rows = collect_offline_schema_tables(limit=limit)
        if offline_rows:
            return offline_rows
        raise HTTPException(
            status_code=503,
            detail=f"Postgres недоступен: {exc.__class__.__name__}. Проверь параметры подключения.",
        ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT schemaname, relname, n_live_tup
                FROM pg_stat_user_tables
                ORDER BY n_live_tup DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {"schema": r[0], "table": r[1], "rows": int(r[2] or 0)}
            for r in rows
        ]
    finally:
        conn.close()


@app.get("/api/schema/table")
def schema_table(name: str, schema: str = "public"):
    try:
        conn = get_conn()
    except Exception as exc:
        card = build_offline_table_card(schema, name)
        if card:
            return card
        raise HTTPException(
            status_code=503,
            detail=f"Postgres недоступен: {exc.__class__.__name__}. Проверь параметры подключения.",
        ) from exc
    try:
        columns = get_columns(conn, schema, name)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tc.constraint_name,
                    kcu.column_name,
                    ccu.table_schema AS foreign_table_schema,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.constraint_schema = tc.constraint_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = %s
                  AND tc.table_name = %s
                """,
                (schema, name),
            )
            fks = [
                {
                    "constraint": r[0],
                    "column": r[1],
                    "ref_schema": r[2],
                    "ref_table": r[3],
                    "ref_column": r[4],
                }
                for r in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT
                    tc.constraint_name,
                    kcu.table_schema AS source_schema,
                    kcu.table_name AS source_table,
                    kcu.column_name AS source_column,
                    ccu.column_name AS target_column
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                JOIN information_schema.constraint_column_usage AS ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.constraint_schema = tc.constraint_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND ccu.table_schema = %s
                  AND ccu.table_name = %s
                """,
                (schema, name),
            )
            incoming_fks = [
                {
                    "constraint": r[0],
                    "source_schema": r[1],
                    "source_table": r[2],
                    "source_column": r[3],
                    "target_column": r[4],
                }
                for r in cur.fetchall()
            ]

        return {
            "schema": schema,
            "table": name,
            "columns": columns,
            "foreign_keys": fks,
            "incoming_foreign_keys": incoming_fks,
        }
    finally:
        conn.close()


@app.get("/api/compare")
def compare_mapping(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    schema, table = resolve_table(run)
    try:
        conn = get_conn()
    except Exception as exc:
        json_path = run.get("json")
        sheet_name = run.get("sheet")
        if not json_path or not sheet_name:
            raise HTTPException(
                status_code=503,
                detail=f"Postgres недоступен: {exc.__class__.__name__}. Проверь параметры подключения.",
            ) from exc
        sheet = load_sheet(Path(json_path), sheet_name)
        step2 = sheet.get("step2") or []
        mapped = [r for r in step2 if r.get("db_column") and not str(r.get("db_column")).startswith("(")]
        unresolved = [
            r.get("excel_column")
            for r in step2
            if not r.get("db_column") or str(r.get("db_column")).startswith("(")
        ]
        return {
            "table": f"{schema}.{table}",
            "mapped_count": len(mapped),
            "missing_in_table": [],
            "missing_in_mapping": [x for x in unresolved if x],
            "warning": "Postgres недоступен — сравнение выполнено по mapping JSON (offline).",
        }
    try:
        columns = get_columns(conn, schema, table)
        table_cols = {c["name"] for c in columns}
        json_path = run.get("json")
        sheet_name = run.get("sheet")
        if not json_path or not sheet_name:
            raise HTTPException(status_code=400, detail="Run step 1 first")
        sheet = load_sheet(Path(json_path), sheet_name)
        step2 = sheet.get("step2") or []
        mapped = [r for r in step2 if r.get("db_column") and not str(r.get("db_column")).startswith("(")]
        mapped_cols = {r["db_column"] for r in mapped}
        missing_in_table = [r for r in mapped if r["db_column"] not in table_cols]
        missing_in_mapping = sorted(table_cols - mapped_cols)
        return {
            "table": f"{schema}.{table}",
            "mapped_count": len(mapped),
            "missing_in_table": missing_in_table,
            "missing_in_mapping": missing_in_mapping,
        }
    finally:
        conn.close()


@app.get("/api/integrations")
def integrations(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    schema, table = resolve_table(run)
    return {
        "table": f"{schema}.{table}",
        "csv_url": f"/api/download?file_id={file_id}&kind=csv",
        "json_url": f"/api/download?file_id={file_id}&kind=json",
        "api_dashboard": f"/api/dashboard?file_id={file_id}",
    }


def _connector_ids():
    return {c["id"] for c in CONNECTORS}


def _append_integration_event(run: dict, connector_id: str, action: str, status: str, message: str):
    events = run.setdefault("integration_events", [])
    events.append(
        {
            "ts": datetime.utcnow().isoformat(),
            "connector_id": connector_id,
            "action": action,
            "status": status,
            "message": message,
        }
    )
    if len(events) > 300:
        del events[:-300]


@app.get("/api/integrations/connectors")
def integrations_connectors(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    states = run.get("integration_states", {})
    configs = run.get("integration_configs", {})
    tests = run.get("integration_tests", {})
    syncs = run.get("integration_syncs", {})
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "category": c["category"],
            "status": states.get(c["id"], "disconnected"),
            "config": configs.get(c["id"], {}),
            "last_test": tests.get(c["id"]),
            "last_sync": syncs.get(c["id"]),
        }
        for c in CONNECTORS
    ]


@app.post("/api/integrations/connect")
def integrations_connect(payload: IntegrationConnectRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    if payload.connector_id not in _connector_ids():
        raise HTTPException(status_code=400, detail="Unknown connector_id")
    states = run.setdefault("integration_states", {})
    configs = run.setdefault("integration_configs", {})
    cfg = dict(configs.get(payload.connector_id, {}))
    cfg.update(payload.config or {})
    endpoint = str(cfg.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status_code=400, detail="Укажи endpoint для подключения.")
    if not re.match(r"^https?://", endpoint):
        raise HTTPException(status_code=400, detail="Endpoint должен начинаться с http:// или https://")

    cfg["endpoint"] = endpoint
    cfg["connected_at"] = datetime.utcnow().isoformat()
    states[payload.connector_id] = "connected"
    configs[payload.connector_id] = cfg
    _append_integration_event(run, payload.connector_id, "connect", "ok", f"Подключение установлено: {endpoint}")
    return {"status": "ok", "connector_id": payload.connector_id, "config": cfg}


@app.post("/api/integrations/test")
def integrations_test(payload: IntegrationActionRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    if payload.connector_id not in _connector_ids():
        raise HTTPException(status_code=400, detail="Unknown connector_id")
    configs = run.setdefault("integration_configs", {})
    tests = run.setdefault("integration_tests", {})
    cfg = dict(configs.get(payload.connector_id, {}))
    cfg.update(payload.config or {})
    endpoint = str(cfg.get("endpoint") or "").strip()
    token = str(cfg.get("token") or "").strip()
    if not endpoint:
        tests[payload.connector_id] = {
            "ok": False,
            "message": "Укажи endpoint.",
            "checked_at": datetime.utcnow().isoformat(),
        }
        _append_integration_event(run, payload.connector_id, "test", "error", "Не указан endpoint.")
        return {"ok": False, "message": "Укажи endpoint."}
    if not re.match(r"^https?://", endpoint):
        tests[payload.connector_id] = {
            "ok": False,
            "message": "Endpoint должен начинаться с http:// или https://",
            "checked_at": datetime.utcnow().isoformat(),
        }
        _append_integration_event(run, payload.connector_id, "test", "error", "Некорректный формат endpoint.")
        return {"ok": False, "message": "Endpoint должен начинаться с http:// или https://"}
    if token and len(token) < 4:
        tests[payload.connector_id] = {
            "ok": False,
            "message": "Token слишком короткий.",
            "checked_at": datetime.utcnow().isoformat(),
        }
        _append_integration_event(run, payload.connector_id, "test", "error", "Token слишком короткий.")
        return {"ok": False, "message": "Token слишком короткий."}

    started = time.monotonic()
    request = urllib.request.Request(endpoint, method="HEAD")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=7) as resp:
            code = int(resp.getcode() or 0)
    except urllib.error.HTTPError as exc:
        code = int(exc.code or 0)
    except Exception as exc:
        tests[payload.connector_id] = {
            "ok": False,
            "message": f"Проверка не пройдена: {exc}",
            "checked_at": datetime.utcnow().isoformat(),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        _append_integration_event(run, payload.connector_id, "test", "error", f"Endpoint недоступен: {exc}")
        return {"ok": False, "message": f"Проверка не пройдена: {exc}"}

    latency_ms = int((time.monotonic() - started) * 1000)
    ok = (200 <= code < 400) or code in {401, 403}
    message = (
        f"Endpoint отвечает (HTTP {code}, {latency_ms} ms)."
        if ok
        else f"Endpoint недоступен (HTTP {code})."
    )
    tests[payload.connector_id] = {
        "ok": ok,
        "message": message,
        "checked_at": datetime.utcnow().isoformat(),
        "http_code": code,
        "latency_ms": latency_ms,
    }
    _append_integration_event(run, payload.connector_id, "test", "ok" if ok else "error", message)
    return {"ok": ok, "message": message, "http_code": code, "latency_ms": latency_ms}


@app.post("/api/integrations/disconnect")
def integrations_disconnect(payload: IntegrationActionRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    if payload.connector_id not in _connector_ids():
        raise HTTPException(status_code=400, detail="Unknown connector_id")
    states = run.setdefault("integration_states", {})
    configs = run.setdefault("integration_configs", {})
    states[payload.connector_id] = "disconnected"
    configs.pop(payload.connector_id, None)
    _append_integration_event(run, payload.connector_id, "disconnect", "ok", "Подключение отключено.")
    return {"status": "ok"}


@app.post("/api/integrations/sync")
def integrations_sync(payload: IntegrationSyncRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    if payload.connector_id not in _connector_ids():
        raise HTTPException(status_code=400, detail="Unknown connector_id")
    states = run.setdefault("integration_states", {})
    if states.get(payload.connector_id) != "connected":
        raise HTTPException(status_code=409, detail="Сначала подключи интеграцию (status=connected).")

    direction = str(payload.direction or "push").lower()
    if direction not in {"push", "pull", "bidirectional"}:
        raise HTTPException(status_code=400, detail="direction должен быть push/pull/bidirectional")

    step2 = run.get("step2") or []
    mapped = [r for r in step2 if r.get("db_column") and not str(r.get("db_column")).startswith("(")]
    unresolved = [r for r in step2 if not r.get("db_column") or str(r.get("db_column")).startswith("(")]

    estimated_rows = int(payload.options.get("rows") or 0) if isinstance(payload.options, dict) else 0
    if estimated_rows <= 0:
        # Conservative synthetic estimate from current mapping context.
        estimated_rows = max(1, len(mapped) * 50)

    syncs = run.setdefault("integration_syncs", {})
    sync_result = {
        "status": "ok",
        "direction": direction,
        "table": run.get("table") or "",
        "mapped_columns": len(mapped),
        "unresolved_columns": len(unresolved),
        "estimated_rows": estimated_rows,
        "finished_at": datetime.utcnow().isoformat(),
    }
    syncs[payload.connector_id] = sync_result
    _append_integration_event(
        run,
        payload.connector_id,
        "sync",
        "ok",
        f"Синхронизация завершена ({direction}, rows≈{estimated_rows}, mapped={len(mapped)}).",
    )
    return {"ok": True, "result": sync_result}


@app.get("/api/integrations/events")
def integrations_events(file_id: str, limit: int = 80):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    events = list(run.get("integration_events", []))
    if limit > 0:
        events = events[-limit:]
    return {"events": events}


@app.post("/api/migration/run")
def migration_run(payload: MigrationRunRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    source = payload.source or {}
    source_type = (source.get("type") or "postgres").lower()
    if source_type not in {"mysql", "oracle", "db2", "postgres"}:
        raise HTTPException(status_code=400, detail="Источник не поддерживается")
    if not payload.tables:
        raise HTTPException(status_code=400, detail="Не выбраны таблицы")
    start_stage = str(payload.start_stage or "schema").strip().lower()
    if start_stage not in {"schema", "data", "validation", "cutover"}:
        raise HTTPException(status_code=400, detail="start_stage должен быть одним из: schema/data/validation/cutover")
    current_progress = run.get("migration_progress") or {}
    if str(current_progress.get("state") or "").lower() == "running":
        raise HTTPException(status_code=409, detail="Миграция уже выполняется для этого файла.")

    # Strict gate enforcement before each run.
    gate_table = payload.tables[0] if payload.tables else (run.get("table") or "")
    try:
        gate = compute_quality_gate(
            source=source,
            table=gate_table,
            file_id=payload.file_id,
            target_table=run.get("table"),
        )
        run["migration_gate"] = gate
        if gate.get("status") == "BLOCK":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Migration blocked: Quality Gate = BLOCK.",
                    "gate": gate,
                },
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Не удалось выполнить Quality Gate: {friendly_backend_error(exc)}",
        ) from exc

    run["migration_output"] = "Запуск миграции..."
    run["migration_summary"] = []
    run["migration_events"] = []
    run["migration_stages"] = build_migration_stages()
    run["migration_cancel_requested"] = False
    run["migration_job_thread"] = None
    run_id = uuid.uuid4().hex[:12]
    run["active_migration_run_id"] = run_id
    run["active_migration_source_type"] = source_type
    run["active_migration_mode"] = payload.mode
    run["active_migration_tables"] = list(payload.tables)
    run["active_migration_start_stage"] = start_stage
    run["active_migration_retry_of"] = payload.retry_of
    update_migration_history(
        run,
        run_id,
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        state="running",
        stage="Prepare",
        percent=1,
        source_type=source_type,
        mode=payload.mode,
        start_stage=start_stage,
        retry_of=payload.retry_of,
        tables=list(payload.tables),
        mapping_snapshot=list(run.get("step2") or []),
        stages=list(run["migration_stages"]),
        summary=[],
        total_tables=len(payload.tables),
        processed_tables=0,
        processed_rows=0,
        total_rows=0,
        rows_per_sec=0.0,
        eta_sec=None,
        message=f"Инициализация миграции (start_stage={start_stage})...",
    )
    append_migration_event(run, "Запрос на запуск миграции получен.")
    set_migration_progress(
        run,
        state="running",
        stage="Prepare",
        percent=1,
        message=f"Инициализация миграции (start_stage={start_stage})...",
        current_table="",
        processed_tables=0,
        total_tables=len(payload.tables),
        processed_rows=0,
        total_rows=0,
        rows_per_sec=0.0,
        eta_sec=None,
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
    )

    worker_payload = payload.dict()
    worker_payload["_run_id"] = run_id
    thread = threading.Thread(target=execute_migration_job, args=(run, worker_payload), daemon=True)
    run["migration_job_thread"] = thread.name
    thread.start()

    return {
        "status": "started",
        "output": run["migration_output"],
        "stages": run["migration_stages"],
        "progress": run.get("migration_progress", default_migration_progress()),
        "history": latest_migration_history(run),
    }


@app.post("/api/migration/cancel")
def migration_cancel(payload: MigrationCancelRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    progress = run.get("migration_progress") or {}
    state = str(progress.get("state") or "").lower()
    if state != "running":
        return {"status": "noop", "message": "Активной миграции нет."}
    run["migration_cancel_requested"] = True
    append_migration_event(run, "Пользователь запросил отмену миграции.", level="warn")
    set_migration_progress(
        run,
        state="running",
        stage="Cancelling",
        message="Запрошена отмена миграции...",
        eta_sec=None,
    )
    return {"status": "cancelling", "message": "Отмена запрошена."}


@app.get("/api/migration/history")
def migration_history(file_id: str, limit: int = 120):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    return {"history": latest_migration_history(run, limit=limit)}


@app.get("/api/migration/history-item")
def migration_history_item(file_id: str, run_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    item = get_migration_history_item(run, run_id, include_events=True)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown run_id")

    # For active run details, expose current live output/progress as fallback.
    if str(run.get("active_migration_run_id") or "") == str(run_id):
        item["live_progress"] = run.get("migration_progress", default_migration_progress())
        item["live_output"] = run.get("migration_output", "")
        if (not item.get("events")) and isinstance(run.get("migration_events"), list):
            item["events"] = list(run.get("migration_events", []))
            item["event_count"] = len(item["events"])
    return {"item": item}


@app.get("/api/migration/history-diff")
def migration_history_diff(file_id: str, from_run_id: str, to_run_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    base = get_migration_history_item(run, from_run_id, include_events=True)
    target = get_migration_history_item(run, to_run_id, include_events=True)
    if not base:
        raise HTTPException(status_code=404, detail="from_run_id not found")
    if not target:
        raise HTTPException(status_code=404, detail="to_run_id not found")
    return migration_diff_items(base, target)


@app.post("/api/migration/retry")
def migration_retry(payload: MigrationRetryRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    item = get_migration_history_item(run, payload.run_id, include_events=False)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    source_type = str(item.get("source_type") or "postgres")
    mode = str(item.get("mode") or "upsert")
    tables = list(item.get("tables") or [])
    if not tables:
        raise HTTPException(status_code=400, detail="В выбранном запуске нет таблиц.")
    source = dict(payload.source or {})
    source.setdefault("type", source_type)
    if not source.get("host") and source_type == "postgres":
        source["host"] = os.environ.get("PGHOST", "localhost")
    if not source.get("port") and source_type == "postgres":
        source["port"] = int(os.environ.get("PGPORT", "5432"))
    if not source.get("db") and source_type == "postgres":
        source["db"] = resolve_default_pg_database(
            host=source.get("host"),
            port=source.get("port"),
            user=source.get("user"),
            password=source.get("password"),
        )
    if not source.get("user"):
        source["user"] = os.environ.get("PGUSER", "")
    if not source.get("password"):
        source["password"] = os.environ.get("PGPASSWORD", "")
    return migration_run(
        MigrationRunRequest(
            file_id=payload.file_id,
            source=source,
            tables=tables,
            mode=mode,
            start_stage=payload.start_stage or "schema",
            retry_of=payload.run_id,
        )
    )


@app.post("/api/mapping/override")
def mapping_override(payload: MappingOverrideRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    step2 = normalize_mapping_override_rows(payload.rows)
    run["step2"] = step2
    run["updated_at"] = datetime.utcnow().isoformat()

    json_path = Path(run["json"]) if run.get("json") else None
    sheet_name = payload.sheet_name or run.get("sheet")
    if json_path and json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            target_idx = 0
            if sheet_name:
                for i, item in enumerate(data):
                    if item.get("sheet_name") == sheet_name:
                        target_idx = i
                        break
            data[target_idx]["step2"] = step2
            if run.get("table"):
                data[target_idx].setdefault("step4", {})
                data[target_idx]["step4"]["selection"] = run.get("table")
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = Path(run["csv"]) if run.get("csv") else None
    if not csv_path:
        csv_path = UPLOAD_DIR / f"{payload.file_id}_mapping_override.csv"
        run["csv"] = str(csv_path)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Excel column", "DB column", "Matches"])
        for row in step2:
            writer.writerow([row["excel_column"], row["db_column"], f"{row['match_count']}/10" if row["match_count"] else ""])

    return {
        "status": "ok",
        "updated": len(step2),
        "step2": step2,
        "json_url": f"/api/download?file_id={payload.file_id}&kind=json" if run.get("json") else "",
        "csv_url": f"/api/download?file_id={payload.file_id}&kind=csv" if run.get("csv") else "",
    }


@app.post("/api/mapping/select-table")
def mapping_select_table(payload: MappingSelectTableRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    table = str(payload.table or "").strip()
    if not table:
        raise HTTPException(status_code=400, detail="Table is empty.")
    run["table"] = table
    run["updated_at"] = datetime.utcnow().isoformat()

    json_path = Path(run["json"]) if run.get("json") else None
    sheet_name = run.get("sheet")
    if json_path and json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            target_idx = 0
            if sheet_name:
                for i, item in enumerate(data):
                    if item.get("sheet_name") == sheet_name:
                        target_idx = i
                        break
            data[target_idx].setdefault("step4", {})
            data[target_idx]["step4"]["selection"] = table
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"file_id": payload.file_id, "table": table, "updated_at": run["updated_at"]}


@app.post("/api/run-step2")
def run_step2(payload: RunStep2Request):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    excel_path = Path(run["path"])
    json_path = run.get("json")
    if not json_path:
        raise HTTPException(status_code=400, detail="Run step 1 first")

    sheet_name = payload.sheet_name or run.get("sheet")
    target_table = payload.target_table or ""
    if not sheet_name:
        raise HTTPException(status_code=400, detail="Sheet name not found")

    env = build_script_pg_env(os.environ.copy())
    env["INCLUDE_SCHEMAS"] = "public"
    env["SKIP_SCRIPT1"] = "1"
    env["EXCEL_FILE"] = str(excel_path)
    env["MAPPING_JSON"] = str(json_path)
    env["SHEET_NAME"] = sheet_name
    if target_table:
        env["TARGET_TABLE"] = target_table

    proc = subprocess.run([sys.executable, str(SCRIPT2)], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = summarize_script_error(proc.stderr, proc.stdout)
        status_code = 503 if "Postgres connection failed" in detail else 500
        raise HTTPException(status_code=status_code, detail=detail)

    output = proc.stdout.strip()
    run["migration_output"] = output
    run["migration_stages"] = [
        {"name": "Schema", "status": "done"},
        {"name": "Data", "status": "done"},
        {"name": "Validation", "status": "done"},
        {"name": "Cutover", "status": "pending"},
    ]
    return {"status": "ok", "output": output}


@app.get("/api/migration/status")
def migration_status(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    return {
        "output": run.get("migration_output", ""),
        "summary": run.get("migration_summary", []),
        "events": run.get("migration_events", []),
        "history": latest_migration_history(run),
        "stages": run.get("migration_stages", []),
        "gate": run.get("migration_gate"),
        "progress": run.get("migration_progress", default_migration_progress()),
        "cancel_requested": bool(run.get("migration_cancel_requested")),
    }


@app.post("/api/migration/test-connection")
def migration_test_connection(payload: MigrationTestRequest):
    try:
        source_type = (payload.type or "postgres").lower()
        if source_type == "postgres":
            conn = psycopg2.connect(
                dbname=payload.db or resolve_default_pg_database(
                    host=payload.host,
                    port=payload.port,
                    user=payload.user,
                    password=payload.password,
                ),
                user=payload.user or os.environ.get("PGUSER"),
                password=payload.password or os.environ.get("PGPASSWORD"),
                host=payload.host or os.environ.get("PGHOST", "localhost"),
                port=payload.port or os.environ.get("PGPORT", 5432),
                connect_timeout=5,
            )
            conn.close()
            return {"ok": True, "message": "Подключение успешно"}
        if source_type == "oracle":
            import oracledb  # type: ignore
            dsn = oracledb.makedsn(payload.host, payload.port or 1521, service_name=payload.db)
            conn = oracledb.connect(user=payload.user, password=payload.password, dsn=dsn)
            conn.close()
            return {"ok": True, "message": "Подключение успешно"}
        if source_type == "mysql":
            import pymysql  # type: ignore
            conn = pymysql.connect(
                host=payload.host or "localhost",
                port=int(payload.port or 3306),
                user=payload.user,
                password=payload.password,
                database=payload.db,
                connect_timeout=5,
            )
            conn.close()
            return {"ok": True, "message": "Подключение успешно"}
        if source_type == "db2":
            import ibm_db  # type: ignore
            conn_str = (
                f"DATABASE={payload.db};HOSTNAME={payload.host};PORT={payload.port or 50000};"
                f"PROTOCOL=TCPIP;UID={payload.user};PWD={payload.password};"
            )
            conn = ibm_db.connect(conn_str, "", "")
            ibm_db.close(conn)
            return {"ok": True, "message": "Подключение успешно"}
        return {"ok": False, "message": "Источник не поддерживается"}
    except Exception as exc:
        return {"ok": False, "message": f"Ошибка подключения: {exc.__class__.__name__}"}


@app.post("/api/migration/plan")
def migration_plan(payload: MigrationPlanRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    source = payload.source or {}
    source_type = (source.get("type") or "postgres").lower()
    plan_error = ""
    if source_type == "postgres":
        try:
            conn = get_postgres_conn(source)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Postgres недоступен: {exc.__class__.__name__}. Проверь параметры подключения.",
            ) from exc
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), COALESCE(SUM(n_live_tup),0) FROM pg_stat_user_tables"
                )
                tables, rows = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(DISTINCT schemaname) FROM pg_stat_user_tables"
                )
                schema_count = cur.fetchone()[0]
                cur.execute(
                    """
                    SELECT relname, COALESCE(n_live_tup,0)
                    FROM pg_stat_user_tables
                    ORDER BY n_live_tup DESC
                    LIMIT 5
                    """
                )
                top_tables = [
                    {"name": r[0], "rows": int(r[1])}
                    for r in cur.fetchall()
                ]
                cur.execute(
                    "SELECT COALESCE(SUM(pg_total_relation_size(relid)),0) FROM pg_catalog.pg_statio_user_tables"
                )
                size_bytes = cur.fetchone()[0]
        finally:
            conn.close()
    else:
        try:
            stats = get_external_stats(source)
            tables = stats["tables"]
            rows = stats["rows"]
            size_bytes = stats["size_bytes"]
            schema_count = stats.get("schemas", 1)
            top_tables = stats.get("top_tables", [])
        except Exception as exc:
            tables = 0
            rows = 0
            size_bytes = 0
            schema_count = 0
            top_tables = []
            plan_error = str(exc)
            run["migration_plan_error"] = plan_error

    size_mb = round(size_bytes / (1024 * 1024), 1) if size_bytes else 0
    eta_sec = max(int(rows / 20000), 5)
    eta_min = max(int(eta_sec / 60), 1)
    stages = [
        {"name": "Schema", "status": "pending"},
        {"name": "Data", "status": "pending"},
        {"name": "Validation", "status": "pending"},
        {"name": "Cutover", "status": "pending"},
    ]
    plan = {
        "tables": int(tables),
        "rows": int(rows),
        "size": f"{size_mb} MB",
        "eta": f"~{eta_min} мин",
        "cdc": payload.cdc,
        "issues": 1 if plan_error else 0,
        "schemas": int(schema_count),
        "top_tables": top_tables,
        "mode": payload.mode,
        "log": (
            f"План миграции сформирован (с предупреждением: {friendly_backend_error(RuntimeError(plan_error))})"
            if plan_error
            else "План миграции сформирован"
        ),
        "stages": stages,
    }
    run["migration_plan"] = plan
    run["migration_stages"] = stages
    return plan


@app.post("/api/migration/tables")
def migration_tables(payload: MigrationTablesRequest):
    try:
        tables = get_external_tables(payload.source or {})
        return {"tables": tables}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/preview")
def migration_preview(payload: MigrationPreviewRequest):
    try:
        columns, rows = get_external_preview(payload.source or {}, payload.table, limit=100)
        data = [
            {col: normalize_value(val) for col, val in zip(columns, row)}
            for row in rows
        ]
        return {"columns": columns, "rows": data}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/validate")
def migration_validate(payload: MigrationValidateRequest):
    try:
        source = payload.source or {}
        columns = get_columns_for_source(source, payload.table)
        typed_columns = get_columns_with_types(source, payload.table)
        type_map = {c["name"]: c.get("type") for c in typed_columns}
        pk_cols = get_primary_key_columns(source, payload.table)
        if not pk_cols:
            fallback = next((c for c in columns if c.lower() == "r_object_id"), None)
            if fallback:
                pk_cols = [fallback]
            elif columns:
                pk_cols = [columns[0]]
        total, nulls, duplicates = get_table_counts(source, payload.table, pk_cols)
        null_top = get_null_top(source, payload.table, columns) if columns else []

        columns_preview, preview_rows = fetch_source_preview(source, payload.table, limit=1200)
        preview_maps = []
        if columns_preview and preview_rows:
            for row in preview_rows:
                preview_maps.append({columns_preview[i]: normalize_value(row[i]) for i in range(len(columns_preview))})

        required_fields = pk_cols[:]
        required_missing_rows = 0
        invalid_type_samples = []
        duplicate_key_samples = []
        if preview_maps:
            for row in preview_maps:
                if any(str(row.get(c) or "").strip() == "" for c in required_fields):
                    required_missing_rows += 1

            # Duplicate samples by primary key tuple
            if pk_cols:
                bucket = {}
                for row in preview_maps:
                    key = tuple(str(row.get(c) or "").strip() for c in pk_cols)
                    if not any(key):
                        continue
                    bucket[key] = bucket.get(key, 0) + 1
                for key, cnt in sorted(bucket.items(), key=lambda x: x[1], reverse=True):
                    if cnt <= 1:
                        continue
                    duplicate_key_samples.append({"key": list(key), "count": cnt})
                    if len(duplicate_key_samples) >= 10:
                        break

            for row in preview_maps:
                for col, val in row.items():
                    dtype = type_map.get(col, "")
                    if not looks_like_valid_for_type(val, dtype):
                        invalid_type_samples.append(
                            {
                                "column": col,
                                "value": str(val)[:120],
                                "expected_type": dtype or "unknown",
                            }
                        )
                        if len(invalid_type_samples) >= 20:
                            break
                if len(invalid_type_samples) >= 20:
                    break

        issues = 0
        if nulls:
            issues += 1
        if duplicates:
            issues += 1
        if any(n["nulls"] > 0 for n in null_top):
            issues += 1
        if required_missing_rows > 0:
            issues += 1
        if invalid_type_samples:
            issues += 1
        result = {
            "table": payload.table,
            "key_columns": pk_cols,
            "required_fields": required_fields,
            "total_rows": total,
            "null_key": nulls,
            "duplicate_rows": duplicates,
            "null_top": null_top,
            "required_missing_rows": required_missing_rows,
            "invalid_type_samples": invalid_type_samples,
            "duplicate_key_samples": duplicate_key_samples,
            "profile": {
                "sampled_rows": len(preview_maps),
                "typed_columns": len(typed_columns),
                "required_fields": len(required_fields),
            },
            "issues": issues,
        }
        if payload.file_id:
            run = RUNS.get(payload.file_id)
            if run is not None:
                history = run.setdefault("migration_validation", [])
                history.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "source": (source.get("type") or "postgres"),
                        "table": payload.table,
                        "result": result,
                    }
                )
        return result
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/conflicts")
def migration_conflicts(payload: MigrationConflictsRequest):
    try:
        source = payload.source or {}
        columns, rows = fetch_source_preview(source, payload.table, limit=100)
        if not columns:
            raise HTTPException(status_code=400, detail="Нет данных в источнике")
        pk_cols = get_primary_key_columns(source, payload.table)
        if not pk_cols:
            fallback = next((c for c in columns if c.lower() == "r_object_id"), None)
            if fallback:
                pk_cols = [fallback]
            else:
                pk_cols = [columns[0]]
        key_col = pk_cols[0]

        keys = []
        source_map = {}
        for row in rows:
            row_map = {columns[i]: normalize_value(row[i]) for i in range(len(columns))}
            key_val = row_map.get(key_col)
            if key_val is None:
                continue
            keys.append(key_val)
            source_map[key_val] = row_map

        if not keys:
            raise HTTPException(status_code=400, detail="Не найден ключ для сравнения")

        target_table = payload.target_table
        if not target_table and payload.file_id:
            run = RUNS.get(payload.file_id)
            if run and run.get("table"):
                target_table = run.get("table")
        if not target_table:
            raise HTTPException(status_code=400, detail="Не указана целевая таблица")
        schema, name = resolve_table({"table": target_table})
        target_conn = get_conn()
        try:
            target_columns = [c["name"] for c in get_columns(target_conn, schema, name)]
        finally:
            target_conn.close()
        overlap = [c for c in columns if c in target_columns]
        if key_col not in overlap:
            overlap.insert(0, key_col)

        target_rows = fetch_target_rows(schema, name, key_col, keys, overlap)
        missing_in_target = [k for k in keys if k not in target_rows]
        mismatches = []
        mismatch_count = 0

        for key in keys:
            if key not in target_rows:
                continue
            src_row = source_map.get(key, {})
            tgt_row = target_rows.get(key, {})
            diffs = {}
            for col in overlap:
                if col == key_col:
                    continue
                if src_row.get(col) != tgt_row.get(col):
                    diffs[col] = [src_row.get(col), tgt_row.get(col)]
            if diffs:
                mismatch_count += 1
                if len(mismatches) < 10:
                    mismatches.append({"key": key, "diffs": diffs})

        result = {
            "table": payload.table,
            "target_table": f"{schema}.{name}",
            "key_column": key_col,
            "sampled": len(keys),
            "missing_in_target": len(missing_in_target),
            "mismatch_rows": mismatch_count,
            "mismatch_samples": mismatches,
        }

        if payload.file_id:
            run = RUNS.get(payload.file_id)
            if run is not None:
                history = run.setdefault("migration_conflicts", [])
                history.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "source": (source.get("type") or "postgres"),
                        "table": payload.table,
                        "target_table": result["target_table"],
                        "result": result,
                    }
                )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/schema-diff")
def migration_schema_diff(payload: MigrationSchemaDiffRequest):
    try:
        source = payload.source or {}
        src_cols = get_columns_with_types(source, payload.table)
        target_table = payload.target_table
        if not target_table and payload.file_id:
            run = RUNS.get(payload.file_id)
            if run and run.get("table"):
                target_table = run.get("table")
        if not target_table:
            raise HTTPException(status_code=400, detail="Не указана целевая таблица")
        schema, name = resolve_table({"table": target_table})
        target_conn = get_conn()
        try:
            tgt_cols = get_columns(target_conn, schema, name)
        finally:
            target_conn.close()

        src_map = {c["name"]: c["type"] for c in src_cols}
        tgt_map = {c["name"]: c["type"] for c in tgt_cols}

        added = [{"column": c, "source_type": src_map[c]} for c in src_map.keys() if c not in tgt_map]
        missing = [{"column": c, "target_type": tgt_map[c]} for c in tgt_map.keys() if c not in src_map]
        mismatched = []
        for col in src_map.keys():
            if col in tgt_map and str(src_map[col]).lower() != str(tgt_map[col]).lower():
                mismatched.append({"column": col, "source": src_map[col], "target": tgt_map[col]})

        result = {
            "table": payload.table,
            "target_table": f"{schema}.{name}",
            "added_columns": added,
            "missing_columns": missing,
            "type_mismatches": mismatched,
        }

        if payload.file_id:
            run = RUNS.get(payload.file_id)
            if run is not None:
                history = run.setdefault("migration_schema_diff", [])
                history.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "source": (source.get("type") or "postgres"),
                        "table": payload.table,
                        "target_table": result["target_table"],
                        "result": result,
                    }
                )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/recommendations")
def migration_recommendations(payload: MigrationRecommendRequest):
    try:
        source = payload.source or {}
        src_cols = get_columns_with_types(source, payload.table)
        target_table = payload.target_table
        if not target_table and payload.file_id:
            run = RUNS.get(payload.file_id)
            if run and run.get("table"):
                target_table = run.get("table")
        if not target_table:
            raise HTTPException(status_code=400, detail="Не указана целевая таблица")
        schema, name = resolve_table({"table": target_table})
        target_conn = get_conn()
        try:
            tgt_cols = get_columns(target_conn, schema, name)
        finally:
            target_conn.close()

        src_map = {c["name"]: c["type"] for c in src_cols}
        tgt_map = {c["name"]: c["type"] for c in tgt_cols}

        added = [{"column": c, "source_type": src_map[c]} for c in src_map.keys() if c not in tgt_map]
        missing = [{"column": c, "target_type": tgt_map[c]} for c in tgt_map.keys() if c not in src_map]
        mismatched = []
        for col in src_map.keys():
            if col in tgt_map and str(src_map[col]).lower() != str(tgt_map[col]).lower():
                mismatched.append({"column": col, "source": src_map[col], "target": tgt_map[col]})

        recs = build_recommendations(added, missing, mismatched)
        actions = build_recommendation_actions(schema, name, added, missing, mismatched)
        result = {
            "table": payload.table,
            "target_table": f"{schema}.{name}",
            "recommendations": recs,
            "actions": actions,
            "summary": {
                "add": len(added),
                "missing": len(missing),
                "mismatched": len(mismatched),
            },
        }

        if payload.file_id:
            run = RUNS.get(payload.file_id)
            if run is not None:
                history = run.setdefault("migration_recommendations", [])
                history.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "source": (source.get("type") or "postgres"),
                        "table": payload.table,
                        "target_table": result["target_table"],
                        "result": result,
                    }
                )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.post("/api/migration/apply-recommendation")
def migration_apply_recommendation(payload: MigrationApplyRecommendationRequest):
    run = RUNS.get(payload.file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    action = payload.action or {}
    kind = str(action.get("kind") or "").lower()
    if kind not in {"add_column", "alter_type"}:
        raise HTTPException(status_code=400, detail="Эта рекомендация не поддерживает авто-применение.")
    target_table = payload.target_table or run.get("table")
    if not target_table:
        raise HTTPException(status_code=400, detail="Не определена целевая таблица.")
    schema, name = resolve_table({"table": target_table})
    column = str(action.get("column") or "").strip()
    if not column:
        raise HTTPException(status_code=400, detail="Не указана колонка для применения.")
    ensure_ident(column)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if kind == "add_column":
                target_type = str(action.get("target_type") or action.get("to_type") or "text")
                mapped_type = map_generic_type_to_pg(target_type)
                cur.execute(
                    sql.SQL('ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} {}').format(
                        sql.Identifier(schema),
                        sql.Identifier(name),
                        sql.Identifier(column),
                        sql.SQL(mapped_type),
                    )
                )
            elif kind == "alter_type":
                to_type = str(action.get("to_type") or action.get("target_type") or "text")
                mapped_type = map_generic_type_to_pg(to_type)
                cur.execute(
                    sql.SQL('ALTER TABLE {}.{} ALTER COLUMN {} TYPE {} USING {}::{}').format(
                        sql.Identifier(schema),
                        sql.Identifier(name),
                        sql.Identifier(column),
                        sql.SQL(mapped_type),
                        sql.Identifier(column),
                        sql.SQL(mapped_type),
                    )
                )
        conn.commit()
    finally:
        conn.close()

    if run is not None:
        applied = run.setdefault("migration_applied_recommendations", [])
        applied.append(
            {
                "ts": datetime.utcnow().isoformat(),
                "target_table": f"{schema}.{name}",
                "action": action,
                "status": "applied",
            }
        )
    return {"status": "ok", "message": "Рекомендация применена", "target_table": f"{schema}.{name}", "action": action}


@app.post("/api/migration/quality-gate")
def migration_quality_gate(payload: MigrationGateRequest):
    try:
        result = compute_quality_gate(
            source=payload.source or {},
            table=payload.table,
            file_id=payload.file_id,
            target_table=payload.target_table,
        )
        if payload.file_id:
            run = RUNS.get(payload.file_id)
            if run is not None:
                run["migration_gate"] = result
                history = run.setdefault("migration_gate_history", [])
                history.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "source": ((payload.source or {}).get("type") or "postgres"),
                        "table": payload.table,
                        "target_table": result.get("target_table"),
                        "result": result,
                    }
                )
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=friendly_backend_error(exc))


@app.get("/api/migration/cutover")
def migration_cutover(file_id: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    stages = run.get("migration_stages") or []
    if stages and len(stages) >= 4:
        stages[-1]["status"] = "done"
    run["migration_stages"] = stages
    return {"message": "Cutover выполнен"}


@app.get("/api/migration/report")
def migration_report(file_id: str, kind: str = "json"):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    payload = {
        "file_id": file_id,
        "created_at": datetime.utcnow().isoformat(),
        "table": run.get("table"),
        "plan": run.get("migration_plan"),
        "validation": run.get("migration_validation", []),
        "conflicts": run.get("migration_conflicts", []),
        "schema_diff": run.get("migration_schema_diff", []),
        "recommendations": run.get("migration_recommendations", []),
        "gate": run.get("migration_gate"),
        "gate_history": run.get("migration_gate_history", []),
        "output": run.get("migration_output"),
        "summary": run.get("migration_summary", []),
        "events": run.get("migration_events", []),
        "history": latest_migration_history(run),
        "progress": run.get("migration_progress", default_migration_progress()),
        "cancel_requested": bool(run.get("migration_cancel_requested")),
        "stages": run.get("migration_stages", []),
    }
    if kind == "json":
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content=data,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=migration_report.json"},
        )
    if kind == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["section", "key", "value"])
        writer.writerow(["meta", "file_id", file_id])
        writer.writerow(["meta", "table", payload.get("table") or ""])
        plan = payload.get("plan") or {}
        for key in ["tables", "rows", "size", "eta", "cdc", "issues", "schemas", "mode"]:
            if key in plan:
                writer.writerow(["plan", key, plan.get(key)])
        validation = payload.get("validation") or []
        for item in validation:
            result = item.get("result") or {}
            writer.writerow(["validation", "table", item.get("table")])
            writer.writerow(["validation", "key_columns", ",".join(result.get("key_columns") or [])])
            writer.writerow(["validation", "required_fields", ",".join(result.get("required_fields") or [])])
            writer.writerow(["validation", "total_rows", result.get("total_rows")])
            writer.writerow(["validation", "null_key", result.get("null_key")])
            writer.writerow(["validation", "duplicate_rows", result.get("duplicate_rows")])
            writer.writerow(["validation", "required_missing_rows", result.get("required_missing_rows")])
            profile = result.get("profile") or {}
            writer.writerow(["validation", "sampled_rows", profile.get("sampled_rows")])
            writer.writerow(["validation", "typed_columns", profile.get("typed_columns")])
            writer.writerow(["validation", "issues", result.get("issues")])
            for sample in (result.get("duplicate_key_samples") or [])[:20]:
                writer.writerow(["validation_duplicate_sample", json.dumps(sample, ensure_ascii=False), ""])
            for sample in (result.get("invalid_type_samples") or [])[:30]:
                writer.writerow(["validation_type_sample", json.dumps(sample, ensure_ascii=False), ""])
        gate = payload.get("gate") or {}
        if gate:
            writer.writerow(["gate", "status", gate.get("status")])
            writer.writerow(["gate", "score", gate.get("score")])
            writer.writerow(["gate", "summary", gate.get("summary")])
        progress = payload.get("progress") or {}
        if progress:
            for key in [
                "state",
                "stage",
                "percent",
                "current_table",
                "processed_tables",
                "total_tables",
                "processed_rows",
                "total_rows",
                "rows_per_sec",
                "eta_sec",
                "started_at",
                "finished_at",
            ]:
                writer.writerow(["progress", key, progress.get(key)])
        events = payload.get("events") or []
        for ev in events[-200:]:
            writer.writerow(
                [
                    "events",
                    f"{ev.get('ts')}|{ev.get('level')}",
                    ev.get("message") or "",
                ]
            )
        history = payload.get("history") or []
        for item in history:
            writer.writerow(
                [
                    "history",
                    item.get("id"),
                    json.dumps(item, ensure_ascii=False),
                ]
            )
        data = buf.getvalue().encode("utf-8")
        return Response(
            content=data,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=migration_report.csv"},
        )
    raise HTTPException(status_code=400, detail="Unknown report type")


@app.post("/api/ai/mapping-insights")
def ai_mapping_insights(payload: AIMappingInsightsRequest):
    rows = payload.rows or []
    if payload.file_id and not rows:
        run = RUNS.get(payload.file_id)
        if not run:
            raise HTTPException(status_code=404, detail="Unknown file_id")
        rows = run.get("step2") or []
    insights = compute_mapping_insights(rows, limit=payload.limit)
    return insights


@app.get("/api/bi/insights")
def bi_insights(file_id: str, sheet_name: Optional[str] = None):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    excel_path = Path(run.get("path") or "")
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="Uploaded file not found")

    try:
        selected_sheet = sheet_name or run.get("sheet")
        if selected_sheet:
            df = pd.read_excel(excel_path, sheet_name=selected_sheet, dtype=str)
        else:
            df = pd.read_excel(excel_path, sheet_name=0, dtype=str)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read Excel for BI: {exc}")

    df = clean_dataframe(df)
    total_rows = int(len(df))
    total_cols = int(len(df.columns))

    date_col = pick_file_date_column(df)
    status_col = pick_file_category_column(df, ["статус", "status"])
    doc_type_col = pick_file_category_column(df, ["тип", "вид", "doctype", "doc_type", "doc"])
    dept_col = pick_file_category_column(df, ["подраздел", "департамент", "отдел", "филиал", "organization", "home"])
    id_col = pick_file_id_column(df)

    null_rates = []
    for col in df.columns:
        null_count = int(df[col].isna().sum())
        rate = (null_count / total_rows) if total_rows else 0.0
        null_rates.append({"column": str(col), "nulls": null_count, "rate": rate})
    null_rates.sort(key=lambda item: item["rate"], reverse=True)
    null_avg_pct = round(
        (sum(item["rate"] for item in null_rates) / max(len(null_rates), 1)) * 100, 1
    ) if null_rates else 0.0

    duplicate_count = 0
    if id_col and id_col in df.columns:
        counts = df[id_col].dropna().astype(str).value_counts()
        duplicate_count = int((counts > 1).sum())

    timeline = {"labels": [], "counts": []}
    if date_col and date_col in df.columns:
        series = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True).dropna()
        if not series.empty:
            month_counts = series.dt.strftime("%Y-%m").value_counts().sort_index()
            timeline = {
                "labels": [str(k) for k in month_counts.index.tolist()],
                "counts": [int(v) for v in month_counts.tolist()],
            }

    status_counts = top_counts_df(df, status_col, limit=8) if status_col else {"labels": [], "counts": []}
    doc_counts = top_counts_df(df, doc_type_col, limit=8) if doc_type_col else {"labels": [], "counts": []}
    dept_counts = top_counts_df(df, dept_col, limit=8) if dept_col else {"labels": [], "counts": []}

    mapping_insights = compute_mapping_insights(run.get("step2") or [], limit=12)
    mapping_summary = mapping_insights.get("summary") or {}
    unresolved = int(mapping_summary.get("unresolved_columns") or 0)
    match_ratio = float(mapping_summary.get("match_ratio_pct") or 0.0)

    recommendations = []
    if unresolved:
        recommendations.append(f"Закрыть unresolved маппинг: {unresolved} колонок.")
    if duplicate_count > 0:
        recommendations.append(f"Проверить дубликаты по ключу {id_col or 'ID'}: {duplicate_count}.")
    if null_avg_pct > 20:
        recommendations.append(f"Высокий средний % пустых значений: {null_avg_pct}%.")
    if not recommendations:
        recommendations.append("Критичных проблем качества не найдено. Можно переходить к precheck миграции.")
    recommendations.extend(mapping_insights.get("top_actions") or [])

    kpis = [
        {"id": "rows", "label": "Записи", "value": str(total_rows)},
        {"id": "columns", "label": "Колонки", "value": str(total_cols)},
        {"id": "match_ratio", "label": "Match ratio", "value": f"{match_ratio}%"},
        {"id": "unresolved", "label": "Unresolved", "value": str(unresolved)},
        {"id": "duplicates", "label": "Дубликаты", "value": str(duplicate_count)},
        {"id": "null_avg", "label": "Пустые (ср.)", "value": f"{null_avg_pct}%"},
    ]

    return {
        "file_id": file_id,
        "sheet_name": selected_sheet or "",
        "best_table": run.get("table") or "",
        "kpis": kpis,
        "segments": {
            "status": [
                {"name": str(label), "value": int(value)}
                for label, value in zip(status_counts.get("labels", []), status_counts.get("counts", []))
            ],
            "doc_type": [
                {"name": str(label), "value": int(value)}
                for label, value in zip(doc_counts.get("labels", []), doc_counts.get("counts", []))
            ],
            "department": [
                {"name": str(label), "value": int(value)}
                for label, value in zip(dept_counts.get("labels", []), dept_counts.get("counts", []))
            ],
        },
        "timeline": timeline,
        "null_top": null_rates[:12],
        "mapping": mapping_summary,
        "recommendations": recommendations[:14],
    }


@app.post("/api/ai/assistant")
def ai_assistant(payload: AIAssistantRequest):
    question = str(payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is empty.")

    context = build_ai_context(
        payload.file_id,
        payload.sheet_name,
        include_mapping=bool(payload.include_mapping),
        include_migration=bool(payload.include_migration),
    )

    answer = ""
    recommendations = []
    actions = []
    used_llm = False
    llm_error = ""

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — DataBrigde Copilot для платформы миграции данных. "
                "Отвечай на русском, практично и по шагам. "
                "Верни JSON-объект вида: "
                "{\"answer\":\"...\",\"recommendations\":[\"...\"],\"actions\":[{\"id\":\"...\",\"label\":\"...\"}]}."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Вопрос пользователя: {question}\n\n"
                f"Контекст платформы:\n{json.dumps(context, ensure_ascii=False)}\n\n"
                "Если контекст неполный — дай безопасные рекомендации без выдумывания фактов."
            ),
        },
    ]

    llm_content, llm_error_msg = call_external_llm(messages)
    if llm_content and not llm_error_msg:
        used_llm = True
        parsed = parse_json_from_text(llm_content)
        if isinstance(parsed, dict):
            answer = str(parsed.get("answer") or "").strip()
            raw_recs = parsed.get("recommendations") or []
            if isinstance(raw_recs, list):
                recommendations = [str(item).strip() for item in raw_recs if str(item).strip()]
            raw_actions = parsed.get("actions") or []
            if isinstance(raw_actions, list):
                actions = [item for item in raw_actions if isinstance(item, dict)]
        if not answer:
            answer = str(llm_content).strip()
    else:
        llm_error = llm_error_msg or ""

    if not answer:
        fallback = build_fallback_ai_response(question, context)
        answer = fallback.get("answer") or "Не удалось сформировать ответ."
        recommendations = fallback.get("recommendations") or []
        actions = fallback.get("actions") or []

    mapping_summary = (context.get("mapping_insights") or {}).get("summary") or {}
    return {
        "answer": answer,
        "recommendations": recommendations[:10],
        "actions": actions[:8],
        "used_llm": used_llm,
        "model": LLM_MODEL if used_llm else "fallback",
        "llm_error": llm_error,
        "context_summary": {
            "file_name": context.get("file_name") or "",
            "sheet_name": context.get("sheet_name") or "",
            "best_table": context.get("best_table") or "",
            "mapped_columns": mapping_summary.get("mapped_columns", 0),
            "unresolved_columns": mapping_summary.get("unresolved_columns", 0),
        },
    }


@app.get("/api/download")
def download(file_id: str, kind: str):
    run = RUNS.get(file_id)
    if not run:
        raise HTTPException(status_code=404, detail="Unknown file_id")
    path = run.get(kind)
    if not path:
        raise HTTPException(status_code=404, detail="File not ready")
    return FileResponse(path, filename=Path(path).name)


@app.get("/")
def root():
    index_path = None
    if UI_DIR and UI_DIR.exists():
        candidate = UI_DIR / "index.html"
        if candidate.exists():
            index_path = candidate
    if index_path:
        return FileResponse(index_path)
    return {"service": "databrigde-backend", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


# Serve the UI from the same backend so no manual backend URL is needed.
if UI_DIR and UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
