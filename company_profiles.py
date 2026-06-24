from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
COMPANY_PROFILES_DIR = PROJECT_ROOT / "company_profiles"
DEFAULT_PROFILE_ID = "default"
PROFILE_FILENAME = "profile.json"


def default_profile_payload() -> dict[str, Any]:
    return {
        "profile_id": DEFAULT_PROFILE_ID,
        "company_name": "Default Library",
        "description": "Built-in mapping library extracted from the legacy in-code dictionary.",
        "inherit_default": False,
        "matching": {
            "manual_mappings": {
                "ID документа": "r_object_id",
                "Идентификатор документа": "r_object_id",
                "Автор создания документ на площадке": "r_creator_name",
                "Автор создания документа на площадке": "r_creator_name",
                "Дата создания на площадке": "r_creation_date",
                "Автор последних изменений": "r_modifier",
                "Дата последних изменений": "r_modify_date",
                "дата последних изменеий": "r_modify_date",
                "дата последних изменеий ": "r_modify_date",
                "Перемещен в корзину (да/нет)": "i_is_deleted",
                "Временный номер": "dss_work_number",
                "Статус": "dss_status",
                "Дата создания": "dsdt_creation_date",
                "Гриф": "dsi_classificator",
                "Вид доверенности": "dss_proxy_card_type",
                "Тип доверенности (Внутренняя/Внешняя)": "dss_agent_user_name",
                "Передоверие": "dss_retrust",
                "Подписан на бланке": "dsb_hardcopy_sgn",
                "Регистрационный номер": "dss_reg_number",
                "Дата регистрации": "dsdt_reg_date",
                "Важный документ": "dsb_immediate_examination",
                "Краткое содержание": "dss_description",
                "Примечание": "dss_comment_text",
                "Классификация": "r_object_type",
                "Нотариальное заверение (да/нет)": "dss_notarization_required",
                "Номер нотариального заверения": "dss_attest_number",
                "Дата нотариального заверения": "dsdt_attest_date",
                "Дата начала действия доверенности": "dsdt_actual_start_date",
                "Дата окончания действия доверенности": "dsdt_actual_end_date",
                "Состояние": "dss_state",
                "Глобал ID": "dsid_global_id",
                "Аннулирована": "dsb_annulled",
                "Дата прекращения действия доверенности": "dsdt_cancel_date",
                "Основная доверенность": "parent_id",
                "Кол-во листов": "dsi_number_of_page",
                "Количество листов": "dsi_number_of_page",
                "Кол-во приложений": "dsi_number_of_appendix",
                "Количество приложений": "dsi_number_of_appendix",
                "dsi_funding_year": "dsi_funding_year",
                "Флаг Удаленный": "dsb_deleted",
                "Флаг Важный": "dsb_important",
                "Флаг Срочный": "dsb_urgency",
            },
            "name_keywords": {
                "идентификатор": ["id", "object"],
                "документ": ["document", "doc"],
                "номер": ["number", "num", "no"],
                "временный": ["work"],
                "статус": ["status", "state"],
                "состояние": ["state", "status"],
                "регистрац": ["reg", "registration"],
                "краткое": ["description"],
                "описание": ["description"],
                "примечание": ["comment", "note"],
                "дата": ["date"],
                "создан": ["create", "creation"],
                "последн": ["modify", "modifier"],
                "автор": ["creator", "modifier", "user"],
                "корреспондент": ["crsp"],
                "контрольн": ["control", "due"],
                "срочн": ["urgent", "urgency", "important", "priority"],
                "важн": ["important", "priority"],
                "удален": ["deleted"],
                "корзин": ["deleted"],
                "классиф": ["class", "classification", "classificator"],
                "гриф": ["stamp", "classificator"],
                "маршрут": ["policy", "route"],
                "глобал": ["global"],
                "нотариал": ["notarization", "attest"],
                "аннулир": ["annulled"],
                "передовер": ["retrust"],
                "доверенност": ["proxy", "card", "trust"],
                "подписан": ["hardcopy", "sgn", "sign"],
                "кол-во": ["number", "num", "count", "qty", "number_of", "numberof"],
                "количество": ["number", "num", "count", "qty", "number_of", "numberof"],
                "лист": ["page", "sheet"],
                "прилож": ["appendix", "attachment"],
            },
            "table_name_keywords": {
                "финанс": ["finance", "financial", "financing"],
                "проект": ["project"],
            },
            "intent_keywords": {
                "flag": ["флаг", "flag"],
                "id": ["id", "ид", "идентификатор", "глобал"],
                "date": ["дата", "date", "time"],
            },
            "required_keywords": ["номер", "регист", "reg"],
            "bool_true_values": ["t", "true", "1", "yes", "y", "да"],
            "bool_false_values": ["f", "false", "0", "no", "n", "нет"],
            "date_intent_keywords": {
                "корреспондент": ["crsp", "corr", "correspond"],
                "корресп": ["crsp", "corr", "correspond"],
                "создан": ["create", "creation"],
                "регистр": ["reg", "registration"],
                "контроль": ["control", "due", "deadline"],
            },
            "flag_name_map": {
                "срочн": "urgent_flag",
                "важн": "important_flag",
                "удален": "deleted_flag",
            },
            "column_bonus_rules": [
                {
                    "excel_contains": ["срочн"],
                    "db_contains": ["urgent", "priority", "important"],
                    "bonus": 0.6,
                },
                {
                    "excel_contains": ["важн"],
                    "db_contains": ["important", "priority"],
                    "bonus": 0.6,
                },
                {
                    "excel_contains": ["удален"],
                    "db_contains": ["deleted", "is_deleted"],
                    "bonus": 0.6,
                },
            ],
            "table_choice_rules": [
                {
                    "context_contains": ["входящ"],
                    "prefer_suffix": "incoming_type_doc",
                },
                {
                    "context_contains": ["исходящ"],
                    "prefer_suffix": "outcoming_type_doc",
                },
                {
                    "context_contains": ["орд"],
                    "prefer_contains": "ord",
                },
            ],
            "table_bias_rules": [
                {
                    "context_contains": ["исходящ"],
                    "prefer_suffixes": ["outcoming_type_doc"],
                    "avoid_suffixes": ["incoming_type_doc"],
                    "prefer_bonus": 3.0,
                    "avoid_penalty": 2.0,
                },
                {
                    "context_contains": ["входящ"],
                    "prefer_suffixes": ["incoming_type_doc"],
                    "avoid_suffixes": ["outcoming_type_doc"],
                    "prefer_bonus": 3.0,
                    "avoid_penalty": 2.0,
                },
                {
                    "context_contains": ["орд"],
                    "prefer_contains": ["ord"],
                    "prefer_bonus": 1.5,
                },
                {
                    "context_tokens_any": ["finance", "financial", "financing"],
                    "context_tokens_all": ["project"],
                    "prefer_tokens": ["project", "financing", "finance"],
                    "prefer_bonus": 2.0,
                },
                {
                    "table_tokens_any": ["bck", "backup"],
                    "avoid_penalty": 2.0,
                },
            ],
            "transliteration_map": {},
        },
    }


def ensure_profiles_dir() -> Path:
    COMPANY_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return COMPANY_PROFILES_DIR


def sanitize_profile_id(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_PROFILE_ID
    text = re.sub(r"[^\w-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return text or DEFAULT_PROFILE_ID


def profile_dir(profile_id: str | None) -> Path:
    pid = sanitize_profile_id(profile_id)
    return ensure_profiles_dir() / pid


def profile_file(profile_id: str | None) -> Path:
    return profile_dir(profile_id) / PROFILE_FILENAME


def _normalize_profile_payload(payload: dict[str, Any], fallback_id: str | None = None) -> dict[str, Any]:
    base = copy.deepcopy(payload or {})
    pid = sanitize_profile_id(base.get("profile_id") or fallback_id or base.get("company_name"))
    base["profile_id"] = pid
    base["company_name"] = str(base.get("company_name") or pid).strip() or pid
    base["description"] = str(base.get("description") or "").strip()
    base["inherit_default"] = bool(base.get("inherit_default", pid != DEFAULT_PROFILE_ID))
    matching = base.get("matching")
    if not isinstance(matching, dict):
        matching = {}
    base["matching"] = matching
    return base


def ensure_default_profile() -> Path:
    path = profile_file(DEFAULT_PROFILE_ID)
    if path.exists():
        return path
    save_profile(default_profile_payload())
    return path


def read_profile_file(profile_id: str | None) -> dict[str, Any]:
    ensure_default_profile()
    pid = sanitize_profile_id(profile_id)
    path = profile_file(pid)
    if not path.exists():
        raise FileNotFoundError(f"Profile '{pid}' not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid profile format for '{pid}'")
    return _normalize_profile_payload(data, fallback_id=pid)


def _merge_matching(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            section = dict(merged.get(key) or {})
            section.update(value)
            merged[key] = section
            continue
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = list(merged.get(key) or []) + list(value or [])
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def load_profile(profile_id: str | None) -> dict[str, Any]:
    ensure_default_profile()
    pid = sanitize_profile_id(profile_id)
    raw = read_profile_file(pid)
    if pid == DEFAULT_PROFILE_ID or not raw.get("inherit_default"):
        return raw
    default_raw = read_profile_file(DEFAULT_PROFILE_ID)
    merged = copy.deepcopy(default_raw)
    merged.update(
        {
            "profile_id": raw["profile_id"],
            "company_name": raw["company_name"],
            "description": raw.get("description", ""),
            "inherit_default": True,
        }
    )
    merged["matching"] = _merge_matching(default_raw.get("matching", {}), raw.get("matching", {}))
    return merged


def save_profile(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_profile_payload(payload, fallback_id=payload.get("profile_id"))
    path = profile_file(normalized["profile_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return normalized


def list_profiles() -> list[dict[str, Any]]:
    ensure_default_profile()
    items: list[dict[str, Any]] = []
    for child in sorted(ensure_profiles_dir().iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        path = child / PROFILE_FILENAME
        if not path.exists():
            continue
        try:
            payload = read_profile_file(child.name)
        except Exception:
            continue
        items.append(
            {
                "profile_id": payload["profile_id"],
                "company_name": payload.get("company_name") or payload["profile_id"],
                "description": payload.get("description") or "",
                "inherit_default": bool(payload.get("inherit_default")),
                "path": str(path.relative_to(PROJECT_ROOT)),
            }
        )
    return items
