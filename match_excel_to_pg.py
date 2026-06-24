#!/usr/bin/env python3
import os
import sys
import json
import csv
import re
import getpass
import traceback
import unicodedata
import warnings
from decimal import Decimal, InvalidOperation
from datetime import datetime
from collections import defaultdict

import pandas as pd
import psycopg2
from psycopg2 import sql

from company_profiles import DEFAULT_PROFILE_ID, load_profile

# Keep backend/API errors readable: this warning is noisy and non-fatal for our matching flow.
warnings.filterwarnings(
    "ignore",
    message=r"Parsing dates in %Y-%m-%d %H:%M:%S.*dayfirst=True.*",
    category=UserWarning,
)


EXCLUDE_DIRS = {
    ".git",
    ".Trash",
    ".DS_Store",
    "Library",
    "node_modules",
    "__pycache__",
}

DATE_TYPES = {
    "date",
    "timestamp without time zone",
    "timestamp with time zone",
}

BOOL_TYPES = {"boolean"}
NUMERIC_TYPES = {
    "smallint",
    "integer",
    "bigint",
    "numeric",
    "decimal",
    "real",
    "double precision",
}

FK_COLUMNS = set()
HISTORY_MAP = {}
PROFILE_ID = DEFAULT_PROFILE_ID
PROFILE_META = {}
PROFILE_MATCHING = {}
MANUAL_MAPPINGS = {}
NAME_KEYWORDS = {}
TABLE_NAME_KEYWORDS = {}
INTENT_KEYWORDS = {}
REQUIRED_KEYWORDS = []
BOOL_TRUE_VALUES = {"t", "true", "1", "yes", "y", "да"}
BOOL_FALSE_VALUES = {"f", "false", "0", "no", "n", "нет"}
DATE_INTENT_KEYWORDS = {}
FLAG_NAME_MAP = {}
COLUMN_BONUS_RULES = []
TABLE_CHOICE_RULES = []
TABLE_BIAS_RULES = []
CUSTOM_TRANSLITERATION_MAP = {}


def apply_runtime_profile(profile_id=None):
    global PROFILE_ID, PROFILE_META, PROFILE_MATCHING
    global MANUAL_MAPPINGS, NAME_KEYWORDS, TABLE_NAME_KEYWORDS, INTENT_KEYWORDS
    global REQUIRED_KEYWORDS, BOOL_TRUE_VALUES, BOOL_FALSE_VALUES
    global DATE_INTENT_KEYWORDS, FLAG_NAME_MAP, COLUMN_BONUS_RULES
    global TABLE_CHOICE_RULES, TABLE_BIAS_RULES, CUSTOM_TRANSLITERATION_MAP

    profile = load_profile(profile_id or DEFAULT_PROFILE_ID)
    matching = profile.get("matching") or {}

    PROFILE_ID = str(profile.get("profile_id") or DEFAULT_PROFILE_ID)
    PROFILE_META = {
        "profile_id": PROFILE_ID,
        "company_name": str(profile.get("company_name") or PROFILE_ID),
        "description": str(profile.get("description") or ""),
        "inherit_default": bool(profile.get("inherit_default")),
    }
    PROFILE_MATCHING = matching
    MANUAL_MAPPINGS = dict(matching.get("manual_mappings") or {})
    NAME_KEYWORDS = {str(k).lower(): list(v or []) for k, v in (matching.get("name_keywords") or {}).items()}
    TABLE_NAME_KEYWORDS = {
        str(k).lower(): list(v or []) for k, v in (matching.get("table_name_keywords") or {}).items()
    }
    INTENT_KEYWORDS = {
        str(k).lower(): [str(x).lower() for x in (v or [])]
        for k, v in (matching.get("intent_keywords") or {}).items()
    }
    REQUIRED_KEYWORDS = [str(x).lower() for x in (matching.get("required_keywords") or [])]
    BOOL_TRUE_VALUES = {str(x).strip().lower() for x in (matching.get("bool_true_values") or []) if str(x).strip()}
    BOOL_FALSE_VALUES = {str(x).strip().lower() for x in (matching.get("bool_false_values") or []) if str(x).strip()}
    DATE_INTENT_KEYWORDS = {
        str(k).lower(): [str(x).lower() for x in (v or [])]
        for k, v in (matching.get("date_intent_keywords") or {}).items()
    }
    FLAG_NAME_MAP = {
        str(k).lower(): str(v).strip()
        for k, v in (matching.get("flag_name_map") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    COLUMN_BONUS_RULES = list(matching.get("column_bonus_rules") or [])
    TABLE_CHOICE_RULES = list(matching.get("table_choice_rules") or [])
    TABLE_BIAS_RULES = list(matching.get("table_bias_rules") or [])
    CUSTOM_TRANSLITERATION_MAP = {
        str(k).lower(): str(v)
        for k, v in (matching.get("transliteration_map") or {}).items()
        if str(k)
    }


apply_runtime_profile(DEFAULT_PROFILE_ID)


def find_excel_files(root, max_files=200):
    matches = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in filenames:
            lower = name.lower()
            if lower.endswith(".xlsx") or lower.endswith(".xls"):
                matches.append(os.path.join(dirpath, name))
                if len(matches) >= max_files:
                    return matches
    return matches


def load_mapping_history(root, excel_path):
    history = defaultdict(lambda: defaultdict(float))
    if not excel_path:
        return history
    excel_base = os.path.basename(excel_path)
    for dirpath, _, filenames in os.walk(root):
        if not dirpath.endswith("_mapping_json"):
            continue
        for name in filenames:
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            results = data if isinstance(data, list) else data.get("results") or data.get("sheets") or []
            if isinstance(results, dict):
                results = list(results.values())
            for sheet in results:
                sheet_excel = sheet.get("excel_path") or ""
                if os.path.basename(sheet_excel) != excel_base:
                    continue
                for row in sheet.get("step2", []):
                    excel_col = row.get("excel_column")
                    db_col = row.get("db_column")
                    if not excel_col or not db_col:
                        continue
                    if db_col.startswith("(") and db_col.endswith(")"):
                        continue
                    history[excel_col][db_col] += 0.2
    return history


def choose_excel_file(root):
    preselected = os.getenv("EXCEL_FILE")
    if preselected and os.path.isfile(preselected):
        print("Using preselected Excel file:", preselected)
        return preselected

    files = find_excel_files(root)
    if not files:
        print("No .xls/.xlsx files found under", root)
        sys.exit(1)

    print("Found Excel files (please select a file each run):")
    for i, path in enumerate(files, 1):
        print(f"  {i}) {path}")

    while True:
        choice = input("Select file number: ").strip()
        if not choice.isdigit():
            print("Enter a number.")
            continue
        idx = int(choice)
        if 1 <= idx <= len(files):
            return files[idx - 1]
        print("Out of range.")


def prompt_default(label, default):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def is_yes(value):
    v = value.strip().lower()
    return v in ("y", "yes", "да", "д", "у")


def connect_db():
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    dbname = os.getenv("PGDATABASE", "sedo")
    user = os.getenv("PGUSER", getpass.getuser())
    password = os.getenv("PGPASSWORD", "")

    if os.getenv("PG_NO_PROMPT") == "1":
        print("Using Postgres defaults (PG_NO_PROMPT=1).")
    else:
        print("Postgres connection params (press Enter to accept defaults)")
        host = prompt_default("Host", host)
        port = prompt_default("Port", port)
        dbname = prompt_default("Database", dbname)
        user = prompt_default("User", user)

    try:
        conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
        return conn
    except Exception as exc:
        if os.getenv("PG_NO_PROMPT") == "1":
            raise RuntimeError(
                f"Postgres connection failed to {host}:{port}/{dbname}. "
                "Start Postgres or set PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD."
            ) from exc
        if not password:
            print("Initial connection failed, trying with password.")
            password = getpass.getpass("Password: ")
            conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)
            return conn
        raise


def list_text_columns(conn, include_schemas=None, exclude_schemas=None):
    include_schemas = [s for s in (include_schemas or []) if s]
    exclude_schemas = set(exclude_schemas or [])
    exclude_schemas.update({"information_schema", "pg_catalog"})

    params = []
    where_parts = [
        "table_schema NOT IN %s",
        "data_type IN ('character varying', 'text', 'character', 'char',"
        " 'date', 'timestamp without time zone', 'timestamp with time zone', 'boolean',"
        " 'smallint', 'integer', 'bigint', 'numeric', 'decimal', 'real', 'double precision')",
    ]
    params.append(tuple(exclude_schemas))

    if include_schemas:
        where_parts.append("table_schema IN %s")
        params.append(tuple(include_schemas))

    query = f"""
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE {' AND '.join(where_parts)}
        ORDER BY table_schema, table_name, column_name
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def list_name_columns(conn, include_schemas=None, exclude_schemas=None):
    include_schemas = [s for s in (include_schemas or []) if s]
    exclude_schemas = set(exclude_schemas or [])
    exclude_schemas.update({"information_schema", "pg_catalog"})

    params = []
    where_parts = ["table_schema NOT IN %s"]
    params.append(tuple(exclude_schemas))

    if include_schemas:
        where_parts.append("table_schema IN %s")
        params.append(tuple(include_schemas))

    query = f"""
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE {' AND '.join(where_parts)}
        ORDER BY table_schema, table_name, column_name
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def load_fk_columns(conn, include_schemas=None, exclude_schemas=None):
    include_schemas = [s for s in (include_schemas or []) if s]
    exclude_schemas = set(exclude_schemas or [])
    exclude_schemas.update({"information_schema", "pg_catalog"})

    params = [tuple(exclude_schemas)]
    where_parts = ["tc.table_schema NOT IN %s"]
    if include_schemas:
        where_parts.append("tc.table_schema IN %s")
        params.append(tuple(include_schemas))

    query = f"""
        SELECT tc.table_schema, tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND {' AND '.join(where_parts)}
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        return set(cur.fetchall())


def normalize_date_values(values):
    date_values = []
    for v in values:
        try:
            dt = pd.to_datetime(v, errors="coerce", dayfirst=True)
        except Exception:
            dt = pd.NaT
        if pd.isna(dt):
            continue
        date_values.append(dt.date().isoformat())
    # Keep unique order
    seen = set()
    unique = []
    for v in date_values:
        if v in seen:
            continue
        seen.add(v)
        unique.append(v)
    return unique


def normalize_bool_values(values):
    bool_values = []
    for v in values:
        lower = normalize_text_value(v)
        if lower in BOOL_TRUE_VALUES:
            bool_values.append("t")
        elif lower in BOOL_FALSE_VALUES:
            bool_values.append("f")
    seen = set()
    unique = []
    for v in bool_values:
        if v in seen:
            continue
        seen.add(v)
        unique.append(v)
    return unique


def extract_excel_samples(path, max_values=None):
    xls = pd.ExcelFile(path)
    sheets = {}

    for sheet in xls.sheet_names:
        try:
            df = pd.read_excel(path, sheet_name=sheet, dtype=str, keep_default_na=False)
        except Exception as exc:
            print(f"Skip sheet '{sheet}' due to read error: {exc}")
            continue

        col_samples = {}
        for col in df.columns:
            values = []
            numeric_values = []
            seen = set()
            seen_numeric = set()
            total_rows = len(df[col].tolist())
            non_empty_count = 0
            for raw in df[col].tolist():
                if raw is None:
                    continue
                val = normalize_text_value(raw)
                if not val:
                    continue
                if val.lower() == "null":
                    continue
                non_empty_count += 1
                if val in seen:
                    continue
                seen.add(val)
                values.append(val)
                numeric_val = normalize_numeric_value(raw)
                if numeric_val and numeric_val not in seen_numeric:
                    seen_numeric.add(numeric_val)
                    numeric_values.append(numeric_val)
                if max_values and len(values) >= max_values:
                    break
            date_values = normalize_date_values(values) if values else []
            bool_values = normalize_bool_values(values) if values else []
            numeric_values = numeric_values[:max_values] if max_values else numeric_values
            lengths = [len(v) for v in values if v is not None]
            min_len = min(lengths) if lengths else None
            max_len = max(lengths) if lengths else None
            avg_len = round(sum(lengths) / len(lengths), 2) if lengths else None
            uniq_ratio = round(len(values) / non_empty_count, 3) if non_empty_count else 0.0
            null_ratio = round(
                1 - (non_empty_count / total_rows), 3
            ) if total_rows else 0.0
            col_samples[str(col)] = {
                "values": values,
                "date_values": date_values,
                "bool_values": bool_values,
                "numeric_values": numeric_values,
                "total_rows": total_rows,
                "non_empty_count": non_empty_count,
                "unique_count": len(values),
                "uniq_ratio": uniq_ratio,
                "min_len": min_len,
                "max_len": max_len,
                "avg_len": avg_len,
                "null_ratio": null_ratio,
            }

        if col_samples:
            sheets[sheet] = col_samples

    return sheets


def scan_matches_for_sheet(conn, sheet_name, col_samples, text_columns, name_columns):
    no_values = True
    for payload in col_samples.values():
        if (
            payload.get("values")
            or payload.get("date_values")
            or payload.get("bool_values")
            or payload.get("numeric_values")
        ):
            no_values = False
            break
    if no_values:
        matches = defaultdict(list)
        for excel_col in col_samples.keys():
            manual_col = MANUAL_MAPPINGS.get(excel_col)
            norm_excel = normalize_col_name(excel_col)
            for schema, table, column, data_type in name_columns:
                norm_col = normalize_col_name(column)
                name_score = (
                    name_similarity(excel_col, column)
                    + keyword_bonus(excel_col, column)
                    + HISTORY_MAP.get(excel_col, {}).get(column, 0.0)
                )
                if manual_col and column == manual_col:
                    name_score += 1.0
                if (
                    name_score >= 0.2
                    or (manual_col and column == manual_col)
                    or (norm_col and norm_col == norm_excel)
                ):
                    matches[excel_col].append(
                        {
                            "schema": schema,
                            "table": table,
                            "column": column,
                            "data_type": data_type,
                            "match_count": 0,
                            "match_ratio": 0.0,
                            "example_values": [],
                        }
                    )
        for excel_col, items in matches.items():
            items.sort(key=lambda x: score_item(excel_col, x), reverse=True)
        return matches
    # Build a value -> excel column map for this sheet
    value_to_cols = defaultdict(set)
    date_value_to_cols = defaultdict(set)
    bool_value_to_cols = defaultdict(set)
    numeric_value_to_cols = defaultdict(set)
    for col, payload in col_samples.items():
        for v in payload["values"]:
            value_to_cols[v].add(col)
        for dv in payload["date_values"]:
            date_value_to_cols[dv].add(col)
        for bv in payload.get("bool_values", []):
            bool_value_to_cols[bv].add(col)
        for nv in payload.get("numeric_values", []):
            numeric_value_to_cols[nv].add(col)

    all_values = list(value_to_cols.keys())
    all_date_values = list(date_value_to_cols.keys())
    all_bool_values = list(bool_value_to_cols.keys())
    all_numeric_values = list(numeric_value_to_cols.keys())
    if not all_values and not all_date_values and not all_bool_values and not all_numeric_values:
        return {}

    matches = {col: [] for col in col_samples}

    total_cols = len(text_columns)
    print(f"Scanning {total_cols} DB columns for sheet '{sheet_name}'...")

    with conn.cursor() as cur:
        for idx, (schema, table, column, data_type) in enumerate(text_columns, 1):
            if idx % 100 == 0 or idx == total_cols:
                print(f"  {idx}/{total_cols}")

            if data_type in DATE_TYPES:
                if not all_date_values:
                    continue
                values_list = all_date_values
                value_map = date_value_to_cols
                query = sql.SQL(
                    """
                    WITH vals AS (SELECT unnest(%s::text[]) AS value)
                    SELECT vals.value
                    FROM vals
                    JOIN {schema}.{table} t ON t.{column}::date = vals.value::date
                    GROUP BY vals.value
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                )
            elif data_type in BOOL_TYPES:
                if not all_bool_values:
                    continue
                values_list = all_bool_values
                value_map = bool_value_to_cols
                query = sql.SQL(
                    """
                    WITH vals AS (SELECT unnest(%s::text[]) AS value)
                    SELECT vals.value
                    FROM vals
                    JOIN {schema}.{table} t ON t.{column}::text = vals.value
                    GROUP BY vals.value
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                )
            elif data_type in NUMERIC_TYPES:
                if not all_numeric_values:
                    continue
                values_list = all_numeric_values
                value_map = numeric_value_to_cols
                query = sql.SQL(
                    """
                    WITH vals AS (SELECT unnest(%s::text[]) AS value)
                    SELECT vals.value
                    FROM vals
                    JOIN {schema}.{table} t ON t.{column}::numeric = vals.value::numeric
                    GROUP BY vals.value
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                )
            else:
                if not all_values:
                    continue
                values_list = all_values
                value_map = value_to_cols
                query = sql.SQL(
                    """
                    WITH vals AS (SELECT unnest(%s::text[]) AS value)
                    SELECT vals.value
                    FROM vals
                    JOIN {schema}.{table} t
                      ON regexp_replace(lower(btrim(t.{column}::text)), '\\s+', ' ', 'g') = vals.value
                    GROUP BY vals.value
                    """
                ).format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                )

            try:
                cur.execute(query, (values_list,))
                rows = cur.fetchall()
            except Exception:
                # Skip problematic columns to keep run going.
                conn.rollback()
                continue

            if not rows:
                continue

            matched_values = [value for (value,) in rows]
            per_col_counts = defaultdict(int)
            per_col_values = defaultdict(list)
            for value in matched_values:
                for excel_col in value_map.get(value, []):
                    per_col_counts[excel_col] += 1
                    if len(per_col_values[excel_col]) < 3:
                        per_col_values[excel_col].append(value)

            for excel_col, count in per_col_counts.items():
                if data_type in DATE_TYPES and col_samples[excel_col]["date_values"]:
                    total_values = len(col_samples[excel_col]["date_values"])
                elif data_type in BOOL_TYPES and col_samples[excel_col]["bool_values"]:
                    total_values = len(col_samples[excel_col]["bool_values"])
                elif data_type in NUMERIC_TYPES and col_samples[excel_col]["numeric_values"]:
                    total_values = len(col_samples[excel_col]["numeric_values"])
                else:
                    total_values = len(col_samples[excel_col]["values"])
                ratio = count / total_values if total_values else 0
                matches[excel_col].append(
                    {
                        "schema": schema,
                        "table": table,
                        "column": column,
                        "data_type": data_type,
                        "match_count": count,
                        "match_ratio": ratio,
                        "example_values": per_col_values.get(excel_col, []),
                    }
                )

    # Sort matches and compute table scores using best match per excel column
    for excel_col, items in matches.items():
        items.sort(key=lambda x: (x["match_count"], x["match_ratio"]), reverse=True)

    # Fallback: if no value-based matches for a column, try name-based candidates.
    for excel_col in col_samples.keys():
        if matches.get(excel_col):
            continue
        manual_col = MANUAL_MAPPINGS.get(excel_col)
        norm_excel = normalize_col_name(excel_col)
        for schema, table, column, data_type in name_columns:
            norm_col = normalize_col_name(column)
            name_score = (
                name_similarity(excel_col, column)
                + keyword_bonus(excel_col, column)
                + HISTORY_MAP.get(excel_col, {}).get(column, 0.0)
            )
            if manual_col and column == manual_col:
                name_score += 1.0
            if (
                name_score >= 0.2
                or (manual_col and column == manual_col)
                or (norm_col and norm_col == norm_excel)
            ):
                matches[excel_col].append(
                    {
                        "schema": schema,
                        "table": table,
                        "column": column,
                        "data_type": data_type,
                        "match_count": 0,
                        "match_ratio": 0.0,
                        "example_values": [],
                    }
                )
        if matches.get(excel_col):
            matches[excel_col].sort(key=lambda x: score_item(excel_col, x), reverse=True)

    return matches


def contains_any(text, needles):
    low_text = str(text or "").lower()
    return any(str(needle or "").lower() in low_text for needle in (needles or []))


def transliterate_cyrillic(text):
    mapping = {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "i",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
    mapping.update(CUSTOM_TRANSLITERATION_MAP)
    return "".join(mapping.get(ch, ch) for ch in str(text or "").lower())


def ascii_fold_text(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return normalized.encode("ascii", "ignore").decode("ascii").lower()


def normalize_col_name(name):
    lower = str(name or "").strip().lower()
    base = transliterate_cyrillic(lower)
    if base == lower:
        base = ascii_fold_text(lower) or lower
    return "".join(ch for ch in base if ch.isalnum())


def normalized_text_tokens(text):
    lower = str(text or "").strip().lower()
    parts = set()
    for candidate in {lower, transliterate_cyrillic(lower), ascii_fold_text(lower)}:
        for part in re.findall(r"[a-z0-9]+", candidate):
            parts.add(part)
    return parts


def normalize_text_value(value):
    text = str(value or "").replace("\u00a0", " ").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_numeric_value(value):
    text = str(value or "").strip()
    if not text:
        return None
    compact = text.replace("\u00a0", "").replace(" ", "")
    if "," in compact and "." in compact:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif compact.count(",") == 1 and compact.count(".") == 0:
        compact = compact.replace(",", ".")
    try:
        number = Decimal(compact)
    except (InvalidOperation, ValueError):
        return None
    normalized = format(number.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def excel_name_tokens(name):
    lower = str(name or "").strip().lower()
    tokens = set()
    for key, mapped in NAME_KEYWORDS.items():
        if key in lower:
            tokens.update(mapped)
    tokens.update(normalized_text_tokens(lower))
    return tokens


def name_similarity(excel_col, db_col):
    excel_tokens = excel_name_tokens(excel_col)
    if not excel_tokens:
        return 0.0
    db_tokens = set(db_col.lower().split("_"))
    overlap = len(excel_tokens & db_tokens)
    return overlap / max(len(excel_tokens), 1)


def is_flag_excel(excel_col):
    return contains_any(excel_col, INTENT_KEYWORDS.get("flag", []))


def is_id_excel(excel_col):
    return contains_any(excel_col, INTENT_KEYWORDS.get("id", []))


def is_date_excel(excel_col):
    return contains_any(excel_col, INTENT_KEYWORDS.get("date", []))


def is_date_candidate(db_col):
    col = db_col.lower()
    if "workday" in col or "lunch" in col:
        return False
    if col.startswith("dsdt_"):
        return True
    return any(
        key in col
        for key in (
            "date",
            "time",
            "create",
            "creation",
            "reg",
            "registration",
            "control",
            "due",
            "deadline",
            "crsp",
            "corr",
            "correspond",
        )
    )


def attribute_prefix_type(db_col):
    col = db_col.lower()
    if col.startswith("dsdt_"):
        return "date"
    if col.startswith("dsb_"):
        return "bool"
    if col.startswith("dsid_") or col == "r_object_id":
        return "id"
    if col.startswith("dsi_"):
        return "int"
    if col.startswith("dss_"):
        return "string"
    return None


def confirm_sample_limit(excel_col, data_type=None):
    lower = excel_col.lower()
    if is_flag_excel(excel_col):
        return 50
    if "статус" in lower or "status" in lower or "состояние" in lower:
        return 50
    if is_date_excel(excel_col) or data_type in DATE_TYPES:
        return 50
    return 10


def date_intent_keywords(excel_col):
    lower = excel_col.lower()
    out = set()
    for key, mapped in DATE_INTENT_KEYWORDS.items():
        if key in lower:
            out.update(mapped)
    return out


def custom_flag_name(excel_col):
    lower = excel_col.lower()
    for key, value in FLAG_NAME_MAP.items():
        if key in lower:
            return value
    return "flag_custom"


def profile_column_bonus(excel_col, db_col):
    lower = excel_col.lower()
    col = db_col.lower()
    bonus = 0.0
    for rule in COLUMN_BONUS_RULES:
        if not isinstance(rule, dict):
            continue
        excel_contains = [str(x).lower() for x in (rule.get("excel_contains") or [])]
        db_contains = [str(x).lower() for x in (rule.get("db_contains") or [])]
        if excel_contains and not contains_any(lower, excel_contains):
            continue
        if db_contains and not contains_any(col, db_contains):
            continue
        try:
            bonus += float(rule.get("bonus") or 0.0)
        except Exception:
            continue
    return bonus


def keyword_bonus(excel_col, db_col):
    lower = excel_col.lower()
    col = db_col.lower()
    bonus = 0.0
    prefix_type = attribute_prefix_type(db_col)
    if is_date_excel(excel_col):
        if prefix_type == "date":
            bonus += 0.6
        intent_tokens = date_intent_keywords(excel_col)
        if intent_tokens and any(token in col for token in intent_tokens):
            bonus += 0.6
        if "workday" in col or "lunch" in col:
            bonus -= 0.6
    if is_flag_excel(excel_col):
        if prefix_type == "bool":
            bonus += 0.6
        if "is_" in col or col.startswith("i_") or col.startswith("dsb_"):
            bonus += 0.2
        if "name" in col:
            bonus -= 0.6
    if is_id_excel(excel_col):
        if prefix_type == "id":
            bonus += 0.6
        if col.endswith("_id") or col.startswith("dsid_") or col == "r_object_id":
            bonus += 0.3
        if col == "i_global_id" or col == "dsid_global_id":
            bonus += 0.3
    return bonus + profile_column_bonus(excel_col, db_col)


def score_item(excel_col, item):
    fk_bonus = 0.0
    if is_id_excel(excel_col):
        if (item["schema"], item["table"], item["column"]) in FK_COLUMNS:
            fk_bonus += 0.3
    history_bonus = HISTORY_MAP.get(excel_col, {}).get(item["column"], 0.0)
    return (
        item["match_count"],
        item["match_ratio"],
        name_similarity(excel_col, item["column"])
        + keyword_bonus(excel_col, item["column"])
        + fk_bonus
        + history_bonus,
    )


def filter_flag_candidates(items):
    filtered = []
    for item in items:
        col = item["column"].lower()
        if item.get("data_type") in BOOL_TYPES:
            filtered.append(item)
            continue
        if (
            "flag" in col
            or "deleted" in col
            or "urgent" in col
            or "important" in col
            or col.startswith("dsb_")
            or col.startswith("i_")
        ):
            filtered.append(item)
    return filtered


def table_boolean_columns(conn, schema, table):
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND (
              data_type = 'boolean'
              OR column_name LIKE 'dsb_%%'
              OR column_name LIKE 'i_is_%%'
          )
        ORDER BY column_name
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return [row[0] for row in cur.fetchall()]


def table_columns_with_types(conn, schema, table):
    query = """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return [{"name": row[0], "data_type": row[1]} for row in cur.fetchall()]


def flag_candidates_from_table(schema, table, bool_columns):
    return [
        {
            "schema": schema,
            "table": table,
            "column": col,
            "data_type": "boolean",
            "match_count": 0,
            "match_ratio": 0.0,
        }
        for col in bool_columns
    ]


def is_ambiguous_match(excel_col, items):
    if len(items) < 2:
        return False, None, None
    ordered = sorted(items, key=lambda x: score_item(excel_col, x), reverse=True)
    best = ordered[0]
    second = ordered[1]
    count_diff = abs(best["match_count"] - second["match_count"])
    ratio_diff = abs(best["match_ratio"] - second["match_ratio"])
    name_diff = abs(
        name_similarity(excel_col, best["column"])
        - name_similarity(excel_col, second["column"])
    )
    if count_diff <= 2 and ratio_diff <= 0.02 and name_diff <= 0.1:
        return True, best, ordered[:3]
    return False, best, None


def db_column_stats(conn, schema, table, column):
    query = sql.SQL(
        """
        SELECT
            COUNT(*)::int,
            COUNT(DISTINCT col)::int,
            COALESCE(AVG(LENGTH(col::text)), 0)::float,
            MIN(LENGTH(col::text))::int,
            MAX(LENGTH(col::text))::int,
            SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END)::int
        FROM (
            SELECT {column} AS col
            FROM {schema}.{table}
            LIMIT 10000
        ) s
        """
    ).format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        column=sql.Identifier(column),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        total, distinct, avg_len, min_len, max_len, nulls = cur.fetchone()
    uniq_ratio = distinct / total if total else 0.0
    null_ratio = nulls / total if total else 0.0
    return {
        "sample_total": total,
        "sample_distinct": distinct,
        "avg_length": round(avg_len, 2),
        "min_length": min_len,
        "max_length": max_len,
        "null_ratio": round(null_ratio, 3),
        "uniq_ratio": round(uniq_ratio, 3),
    }


def is_foreign_key(conn, schema, table, column):
    query = """
        SELECT 1
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %s
          AND tc.table_name = %s
          AND kcu.column_name = %s
        LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table, column))
        return cur.fetchone() is not None


def pick_column_mappings(matches, min_match_count=1, min_match_ratio=0.05):
    mapping = {}
    used_columns = set()
    for excel_col, items in matches.items():
        if not items:
            continue
        if is_flag_excel(excel_col):
            flag_items = filter_flag_candidates(items)
            if flag_items:
                items = flag_items
            else:
                # No safe flag candidates; skip mapping to avoid incorrect columns.
                continue
        if is_date_excel(excel_col):
            intent = date_intent_keywords(excel_col)
            if intent:
                date_items = [
                    i for i in items if any(k in i["column"].lower() for k in intent)
                ]
            else:
                date_items = [i for i in items if is_date_candidate(i["column"])]
            if date_items:
                items = date_items
            else:
                continue
        norm_excel = normalize_col_name(excel_col)
        manual_col = MANUAL_MAPPINGS.get(excel_col)
        if manual_col:
            manual_matches = [i for i in items if i["column"] == manual_col]
            if manual_matches:
                ordered = sorted(manual_matches, key=lambda x: score_item(excel_col, x), reverse=True)
            else:
                ordered = sorted(items, key=lambda x: score_item(excel_col, x), reverse=True)
        else:
            name_matches = [i for i in items if normalize_col_name(i["column"]) == norm_excel]
            if name_matches:
                ordered = sorted(name_matches, key=lambda x: score_item(excel_col, x), reverse=True)
            else:
                ordered = sorted(items, key=lambda x: score_item(excel_col, x), reverse=True)
        best = None
        for cand in ordered:
            if cand["column"] in used_columns:
                continue
            best = cand
            break
        if not best and is_flag_excel(excel_col):
            continue
        if not best:
            best = ordered[0]
        if best["match_count"] < min_match_count:
            continue
        if best["match_ratio"] < min_match_ratio:
            continue
        mapping[excel_col] = best
        used_columns.add(best["column"])
    return mapping


def validate_mapping_for_table(conn, col_samples, excel_cols, matches, schema, table):
    mapping = {}
    used_columns = set()
    db_stats_cache = {}
    bool_columns = None
    table_cols = None

    def name_candidates():
        nonlocal table_cols
        if table_cols is None:
            table_cols = table_columns_with_types(conn, schema, table)
        return [
            {
                "schema": schema,
                "table": table,
                "column": col["name"],
                "data_type": col["data_type"],
                "match_count": 0,
                "match_ratio": 0.0,
            }
            for col in table_cols
        ]

    for excel_col in excel_cols:
        items = [
            i for i in matches.get(excel_col, [])
            if i["schema"] == schema and i["table"] == table
        ]
        name_only = False
        if not items:
            items = name_candidates()
            name_only = True
        manual_col = MANUAL_MAPPINGS.get(excel_col)
        if manual_col:
            if table_cols is None:
                table_cols = table_columns_with_types(conn, schema, table)
            manual_info = next(
                (c for c in table_cols if c["name"] == manual_col),
                None,
            )
            if manual_info and not any(i["column"] == manual_col for i in items):
                items = [
                    {
                        "schema": schema,
                        "table": table,
                        "column": manual_info["name"],
                        "data_type": manual_info["data_type"],
                        "match_count": 0,
                        "match_ratio": 0.0,
                    }
                ] + items
        if is_flag_excel(excel_col):
            if not name_only:
                if bool_columns is None:
                    bool_columns = table_boolean_columns(conn, schema, table)
                if bool_columns:
                    items = items + flag_candidates_from_table(schema, table, bool_columns)
        if is_flag_excel(excel_col):
            flag_items = filter_flag_candidates(items)
            if flag_items:
                items = flag_items
            else:
                if not name_only:
                    items = name_candidates()
                    name_only = True
                    flag_items = filter_flag_candidates(items)
                    if flag_items:
                        items = flag_items
                    else:
                        mapping[excel_col] = None
                        continue
                else:
                    mapping[excel_col] = None
                    continue
        if is_date_excel(excel_col):
            intent = date_intent_keywords(excel_col)
            if intent:
                date_items = [
                    i for i in items if any(k in i["column"].lower() for k in intent)
                ]
            else:
                date_items = [
                    i
                    for i in items
                    if is_date_candidate(i["column"]) or i.get("data_type") in DATE_TYPES
                ]
            if date_items:
                items = date_items
            else:
                if not name_only:
                    items = name_candidates()
                    name_only = True
                    date_items = [
                        i
                        for i in items
                        if is_date_candidate(i["column"]) or i.get("data_type") in DATE_TYPES
                    ]
                    if date_items:
                        items = date_items
                    else:
                        mapping[excel_col] = None
                        continue
                else:
                    mapping[excel_col] = None
                    continue
        norm_excel = normalize_col_name(excel_col)
        if manual_col:
            manual_matches = [i for i in items if i["column"] == manual_col]
            ordered = sorted(
                manual_matches or items,
                key=lambda x: score_item(excel_col, x),
                reverse=True,
            )
        else:
            name_matches = [i for i in items if normalize_col_name(i["column"]) == norm_excel]
            ordered = sorted(
                name_matches or items,
                key=lambda x: score_item(excel_col, x),
                reverse=True,
            )
        candidates = ordered[:8]
        best = None
        excel_profile = excel_column_profile(col_samples, excel_col)
        if is_flag_excel(excel_col):
            best_score = (-1.0, -1.0, -1, -1.0)
            for cand in candidates:
                if cand["column"] in used_columns:
                    continue
                limit = confirm_sample_limit(excel_col, cand.get("data_type"))
                confirm_values = excel_values_for_type(
                    col_samples, excel_col, cand.get("data_type")
                )[:limit]
                if confirm_values:
                    confirm_count, confirm_ratio = confirm_mapping(
                        conn,
                        schema,
                        table,
                        cand["column"],
                        confirm_values,
                        data_type=cand.get("data_type"),
                    )
                else:
                    confirm_count, confirm_ratio = 0, 0.0
                name_score = (
                    name_similarity(excel_col, cand["column"])
                    + keyword_bonus(excel_col, cand["column"])
                    + HISTORY_MAP.get(excel_col, {}).get(cand["column"], 0.0)
                )
                score = (name_score, confirm_ratio, confirm_count, cand["match_ratio"])
                if score > best_score:
                    best_score = score
                    best = {
                        **cand,
                        "confirm_count": confirm_count,
                        "confirm_ratio": confirm_ratio,
                        "adjusted_confirm_ratio": confirm_ratio,
                    }
            if not best or best_score[0] < 0.2:
                mapping[excel_col] = None
                continue
        else:
            best_score = (-1.0, -1, -1, -1.0)
            for cand in candidates:
                if cand["column"] in used_columns:
                    continue
                limit = confirm_sample_limit(excel_col, cand.get("data_type"))
                confirm_values = excel_values_for_type(
                    col_samples, excel_col, cand.get("data_type")
                )[:limit]
                if name_only:
                    name_score = (
                        name_similarity(excel_col, cand["column"])
                        + keyword_bonus(excel_col, cand["column"])
                        + HISTORY_MAP.get(excel_col, {}).get(cand["column"], 0.0)
                    )
                    score = (name_score, cand["match_ratio"], cand["match_count"], 0.0)
                    if score > best_score:
                        best_score = score
                        best = {
                            **cand,
                            "confirm_count": 0,
                            "confirm_ratio": 0.0,
                            "adjusted_confirm_ratio": 0.0,
                        }
                    continue
                if not confirm_values:
                    continue
                confirm_count, confirm_ratio = confirm_mapping(
                    conn, schema, table, cand["column"], confirm_values, data_type=cand.get("data_type")
                )
                cache_key = (schema, table, cand["column"])
                if cache_key not in db_stats_cache:
                    db_stats_cache[cache_key] = db_column_stats(conn, schema, table, cand["column"])
                db_profile = db_stats_cache[cache_key]
                adj_ratio, _ = adjusted_confirm_ratio(
                    excel_profile, db_profile, excel_col, cand["column"], confirm_ratio
                )
                score = (
                    adj_ratio,
                    confirm_count,
                    cand["match_count"],
                    cand["match_ratio"],
                )
                if score > best_score:
                    best_score = score
                    best = {
                        **cand,
                        "confirm_count": confirm_count,
                        "confirm_ratio": confirm_ratio,
                        "adjusted_confirm_ratio": adj_ratio,
                    }
            if name_only:
                if not best or best_score[0] < 0.2:
                    mapping[excel_col] = None
                    continue
            elif not best or best["adjusted_confirm_ratio"] < 0.3:
                mapping[excel_col] = None
                continue
        if not best:
            mapping[excel_col] = None
            continue
        mapping[excel_col] = best
        used_columns.add(best["column"])
    return mapping


def confirm_mapping(conn, schema, table, column, sample_values, data_type=None):
    if not sample_values:
        return 0, 0.0
    if data_type in DATE_TYPES:
        query = sql.SQL(
            """
            WITH vals AS (SELECT unnest(%s::text[]) AS value)
            SELECT COUNT(*) FROM (
                SELECT vals.value
                FROM vals
                JOIN {schema}.{table} t ON t.{column}::date = vals.value::date
                GROUP BY vals.value
            ) s
            """
        ).format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            column=sql.Identifier(column),
        )
    elif data_type in NUMERIC_TYPES:
        query = sql.SQL(
            """
            WITH vals AS (SELECT unnest(%s::text[]) AS value)
            SELECT COUNT(*) FROM (
                SELECT vals.value
                FROM vals
                JOIN {schema}.{table} t ON t.{column}::numeric = vals.value::numeric
                GROUP BY vals.value
            ) s
            """
        ).format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            column=sql.Identifier(column),
        )
    else:
        query = sql.SQL(
            """
            WITH vals AS (SELECT unnest(%s::text[]) AS value)
            SELECT COUNT(*) FROM (
                SELECT vals.value
                FROM vals
                JOIN {schema}.{table} t
                  ON regexp_replace(lower(btrim(t.{column}::text)), '\\s+', ' ', 'g') = vals.value
                GROUP BY vals.value
            ) s
            """
        ).format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
            column=sql.Identifier(column),
        )
    with conn.cursor() as cur:
        try:
            cur.execute(query, (sample_values,))
            count = cur.fetchone()[0]
        except psycopg2.Error:
            conn.rollback()
            return 0, 0.0
    ratio = count / len(sample_values) if sample_values else 0.0
    return count, ratio


def find_best_tables_by_columns(conn, mapped_db_columns, include_schemas=None, exclude_schemas=None):
    include_schemas = [s for s in (include_schemas or []) if s]
    exclude_schemas = set(exclude_schemas or [])
    exclude_schemas.update({"information_schema", "pg_catalog"})

    if not mapped_db_columns:
        return []

    params = [tuple(exclude_schemas), tuple(mapped_db_columns)]
    where_parts = ["table_schema NOT IN %s", "column_name IN %s"]
    if include_schemas:
        where_parts.append("table_schema IN %s")
        params.append(tuple(include_schemas))

    query = f"""
        SELECT
            table_schema,
            table_name,
            COUNT(*) as matched_columns,
            STRING_AGG(column_name || ' (' || data_type || ')', ', ') as columns_found,
            ARRAY_AGG(column_name ORDER BY ordinal_position) as ordered_columns,
            ARRAY_AGG(data_type ORDER BY ordinal_position) as ordered_types
        FROM information_schema.columns
        WHERE {' AND '.join(where_parts)}
        GROUP BY table_schema, table_name
        ORDER BY matched_columns DESC, table_schema, table_name
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def ordered_match_score(excel_columns, table_columns):
    # LCS length to score preserved column order.
    if not excel_columns or not table_columns:
        return 0
    dp = [0] * (len(table_columns) + 1)
    for col in excel_columns:
        prev = 0
        for j, tcol in enumerate(table_columns, 1):
            temp = dp[j]
            if col == tcol:
                dp[j] = prev + 1
            else:
                dp[j] = max(dp[j], dp[j - 1])
            prev = temp
    return dp[-1]


def best_candidate_for_column(matches, column_name):
    best = None
    for item in matches:
        if item["column"] != column_name:
            continue
        if not best or (item["match_count"], item["match_ratio"]) > (
            best["match_count"],
            best["match_ratio"],
        ):
            best = item
    return best


def excel_values_for_type(col_samples, excel_col, data_type):
    payload = col_samples.get(excel_col, {})
    if data_type in DATE_TYPES and payload.get("date_values"):
        return payload["date_values"]
    if data_type in BOOL_TYPES and payload.get("bool_values"):
        return payload["bool_values"]
    if data_type in NUMERIC_TYPES and payload.get("numeric_values"):
        return payload["numeric_values"]
    return payload.get("values", [])


def excel_column_profile(col_samples, excel_col):
    payload = col_samples.get(excel_col, {})
    return {
        "uniq_ratio": payload.get("uniq_ratio", 0.0),
        "null_ratio": payload.get("null_ratio", 0.0),
        "min_len": payload.get("min_len"),
        "max_len": payload.get("max_len"),
    }


def adjusted_confirm_ratio(excel_profile, db_profile, excel_col, db_col, confirm_ratio):
    penalty = 0.0
    if excel_profile["uniq_ratio"] >= 0.85 and db_profile["uniq_ratio"] <= 0.2:
        penalty += 0.3
    if excel_profile["null_ratio"] <= 0.05 and db_profile["null_ratio"] >= 0.8:
        penalty += 0.2
    if (
        excel_profile["min_len"] is not None
        and excel_profile["max_len"] is not None
        and db_profile["min_length"] is not None
        and db_profile["max_length"] is not None
    ):
        excel_fixed = excel_profile["min_len"] == excel_profile["max_len"]
        db_fixed = db_profile["min_length"] == db_profile["max_length"]
        if excel_fixed and db_fixed and excel_profile["min_len"] != db_profile["min_length"]:
            penalty += 0.4
    if is_id_excel(excel_col) or attribute_prefix_type(db_col) == "id":
        if excel_profile["min_len"] == 16 and excel_profile["max_len"] == 16:
            if db_profile["min_length"] and db_profile["max_length"]:
                if db_profile["min_length"] != 16 or db_profile["max_length"] != 16:
                    penalty += 0.4
    return max(0.0, confirm_ratio - penalty), penalty


def refine_mapping_for_table(
    conn,
    excel_cols,
    col_samples,
    matches,
    schema,
    table,
    ordered_columns,
    ordered_types,
    min_confirm_ratio=0.5,
):
    if not isinstance(ordered_columns, (list, tuple)):
        ordered_columns = []
    if not isinstance(ordered_types, (list, tuple)):
        ordered_types = []
    table_col_index = {col: idx for idx, col in enumerate(ordered_columns)}
    table_col_type = {
        col: ordered_types[idx] if idx < len(ordered_types) else None
        for idx, col in enumerate(ordered_columns)
    }

    mapping = {}
    for excel_col in excel_cols:
        candidates = [
            m for m in matches.get(excel_col, [])
            if m["schema"] == schema and m["table"] == table
        ]
        if not candidates:
            mapping[excel_col] = None
            continue
        best = max(candidates, key=lambda x: (x["match_count"], x["match_ratio"]))
        mapping[excel_col] = best

    # Recheck gaps using table column order.
    for idx, excel_col in enumerate(excel_cols):
        if mapping[excel_col] is not None:
            continue
        prev_col = None
        next_col = None
        for j in range(idx - 1, -1, -1):
            if mapping[excel_cols[j]] is not None:
                prev_col = excel_cols[j]
                break
        for j in range(idx + 1, len(excel_cols)):
            if mapping[excel_cols[j]] is not None:
                next_col = excel_cols[j]
                break
        if not prev_col or not next_col:
            continue
        prev_db_col = mapping[prev_col]["column"]
        next_db_col = mapping[next_col]["column"]
        if prev_db_col not in table_col_index or next_db_col not in table_col_index:
            continue
        prev_idx = table_col_index[prev_db_col]
        next_idx = table_col_index[next_db_col]
        if next_idx - prev_idx <= 1:
            continue
        candidate_cols = ordered_columns[prev_idx + 1:next_idx]
        best_candidate = None
        for cand in candidate_cols:
            cand_type = table_col_type.get(cand)
            limit = min(confirm_sample_limit(excel_col, cand_type), 30)
            values = excel_values_for_type(col_samples, excel_col, cand_type)[:limit]
            if not values:
                continue
            confirm_count, confirm_ratio = confirm_mapping(
                conn, schema, table, cand, values, data_type=cand_type
            )
            if confirm_ratio >= min_confirm_ratio:
                if not best_candidate or confirm_ratio > best_candidate["confirm_ratio"]:
                    best_candidate = {
                        "schema": schema,
                        "table": table,
                        "column": cand,
                        "data_type": cand_type,
                        "match_count": confirm_count,
                        "match_ratio": confirm_ratio,
                    }
        if best_candidate:
            mapping[excel_col] = best_candidate

    return mapping


def choose_best_table_by_meaning(top_list, sheet_name, excel_path):
    if not top_list:
        return None
    base_name = os.path.basename(excel_path) if excel_path else ""
    lower = f"{sheet_name} {base_name}".lower()
    for rule in TABLE_CHOICE_RULES:
        if not isinstance(rule, dict):
            continue
        context_contains = [str(x).lower() for x in (rule.get("context_contains") or [])]
        if context_contains and not contains_any(lower, context_contains):
            continue
        prefer_suffix = str(rule.get("prefer_suffix") or "").lower()
        prefer_contains = str(rule.get("prefer_contains") or "").lower()
        for row in top_list:
            table_name = str(row[1] or "").lower()
            if prefer_suffix and table_name.endswith(prefer_suffix):
                return row
            if prefer_contains and prefer_contains in table_name:
                return row
    return top_list[0]


def is_copy_table(table_name):
    return table_name.lower().endswith("_copy")


def table_name_tokens(text):
    if not text:
        return set()
    lower = str(text).lower()
    tokens = set()
    for key, mapped in TABLE_NAME_KEYWORDS.items():
        if key in lower:
            tokens.update(mapped)
    tokens.update(normalized_text_tokens(lower))
    return tokens


def table_name_similarity(table_name, sheet_name, excel_path):
    base_name = os.path.splitext(os.path.basename(excel_path or ""))[0]
    context = f"{sheet_name} {base_name}"
    ctx_tokens = table_name_tokens(context)
    if not ctx_tokens:
        return 0.0
    name_tokens = set(table_name.lower().split("_"))
    overlap = len(ctx_tokens & name_tokens)
    return overlap / max(len(ctx_tokens), 1)


def table_name_bias(table_name, sheet_name, excel_path):
    base_name = os.path.basename(excel_path) if excel_path else ""
    lower = f"{sheet_name} {base_name}".lower()
    name = table_name.lower()
    bias = 0.0
    ctx_tokens = table_name_tokens(lower)
    table_tokens = set(name.split("_"))
    for rule in TABLE_BIAS_RULES:
        if not isinstance(rule, dict):
            continue
        if rule.get("context_contains") and not contains_any(lower, rule.get("context_contains")):
            continue
        tokens_any = {str(x).lower() for x in (rule.get("context_tokens_any") or [])}
        if tokens_any and not (ctx_tokens & tokens_any):
            continue
        tokens_all = {str(x).lower() for x in (rule.get("context_tokens_all") or [])}
        if tokens_all and not tokens_all.issubset(ctx_tokens):
            continue

        prefer_suffixes = [str(x).lower() for x in (rule.get("prefer_suffixes") or [])]
        avoid_suffixes = [str(x).lower() for x in (rule.get("avoid_suffixes") or [])]
        prefer_contains = [str(x).lower() for x in (rule.get("prefer_contains") or [])]
        prefer_tokens = {str(x).lower() for x in (rule.get("prefer_tokens") or [])}
        table_tokens_any = {str(x).lower() for x in (rule.get("table_tokens_any") or [])}

        if prefer_suffixes and any(name.endswith(suffix) for suffix in prefer_suffixes):
            bias += float(rule.get("prefer_bonus") or 0.0)
        if avoid_suffixes and any(name.endswith(suffix) for suffix in avoid_suffixes):
            bias -= float(rule.get("avoid_penalty") or 0.0)
        if prefer_contains and any(token in name for token in prefer_contains):
            bias += float(rule.get("prefer_bonus") or 0.0)
        if prefer_tokens and (table_tokens & prefer_tokens):
            bias += float(rule.get("prefer_bonus") or 0.0)
        if table_tokens_any and (table_tokens & table_tokens_any):
            bias -= float(rule.get("avoid_penalty") or 0.0)
    return bias


def required_excel_columns(col_samples):
    required = []
    for excel_col, profile in col_samples.items():
        lower = excel_col.lower()
        uniq_ratio = profile.get("uniq_ratio", 0.0)
        if is_id_excel(excel_col) and uniq_ratio >= 0.7:
            required.append(excel_col)
            continue
        if REQUIRED_KEYWORDS and contains_any(lower, REQUIRED_KEYWORDS) and uniq_ratio >= 0.7:
            required.append(excel_col)
    return required


def score_table_candidate(conn, schema, table, col_samples, matches, sheet_name, excel_path):
    excel_cols = list(col_samples.keys())
    mapping = validate_mapping_for_table(conn, col_samples, excel_cols, matches, schema, table)
    mapped = [m for m in mapping.values() if m]
    if not mapped:
        return {
            "score": -1.0,
            "mapped_count": 0,
            "required_missing": len(required_excel_columns(col_samples)),
            "type_bonus": 0.0,
            "name_bias": 0.0,
        }
    quality = 0.0
    type_bonus = 0.0
    for excel_col, match in mapping.items():
        if not match:
            continue
        quality += match.get("adjusted_confirm_ratio", match.get("confirm_ratio", 0.0))
        prefix_type = attribute_prefix_type(match["column"])
        if is_date_excel(excel_col) and prefix_type == "date":
            type_bonus += 0.3
        if is_flag_excel(excel_col) and prefix_type == "bool":
            type_bonus += 0.3
        if is_id_excel(excel_col) and prefix_type == "id":
            type_bonus += 0.3
    required = required_excel_columns(col_samples)
    required_missing = sum(1 for col in required if mapping.get(col) is None)
    has_flag = any(is_flag_excel(c) for c in excel_cols)
    bool_cols = table_boolean_columns(conn, schema, table)
    no_bool_penalty = 1.5 if has_flag and not bool_cols else 0.0
    name_bias = table_name_bias(table, sheet_name, excel_path)
    name_similarity = table_name_similarity(table, sheet_name, excel_path)
    score = (
        quality
        + type_bonus
        + name_bias
        + name_similarity
        + (len(mapped) * 0.2)
        - (required_missing * 1.0)
        - no_bool_penalty
    )
    return {
        "score": round(score, 3),
        "mapped_count": len(mapped),
        "required_missing": required_missing,
        "type_bonus": round(type_bonus, 3),
        "name_bias": round(name_bias, 3),
        "name_similarity": round(name_similarity, 3),
    }


def print_results(conn, sheet_name, col_samples, matches, mapping, table_candidates, excel_path, top_n=3, top_tables=5):
    print("\n=== Sheet:", sheet_name, "===")

    result = {
        "sheet_name": sheet_name,
        "excel_path": excel_path,
        "step1": [],
        "step2": [],
        "step3": {"skipped": False, "message": "", "candidates": []},
        "step4": {"selection": None, "choice": None},
        "suggestions": [],
    }

    print("Step 1: column name mapping by value matches")
    if mapping:
        for excel_col, match in mapping.items():
            if isinstance(match, dict) and match.get("ambiguous"):
                print(f"  {excel_col} => (ambiguous)")
                result["step1"].append(
                    {
                        "excel_column": excel_col,
                        "db_column": None,
                        "db_table": None,
                        "match_count": 0,
                        "match_ratio": 0.0,
                        "examples": [],
                        "ambiguous": True,
                    }
                )
                continue
            examples = ", ".join(match.get("example_values", []))
            print(
                f"  {excel_col} => {match['column']}"
                f"  matches={match['match_count']} ratio={match['match_ratio']:.2f}"
            )
            if examples:
                print(f"    example values: {examples}")
            result["step1"].append(
                {
                    "excel_column": excel_col,
                    "db_column": match["column"],
                    "db_table": f"{match['schema']}.{match['table']}",
                    "match_count": match["match_count"],
                    "match_ratio": match["match_ratio"],
                    "examples": match.get("example_values", []),
                }
            )
    else:
        print("  No confident column mappings found.")

    print("\nStep 2: Excel -> DB column mapping")
    for idx, excel_col in enumerate(col_samples.keys(), 1):
        match = mapping.get(excel_col)
        if not match:
            if is_flag_excel(excel_col):
                custom_name = f"({custom_flag_name(excel_col)})"
            else:
                custom_name = f"({excel_col})"
            print(f"  {idx}. {excel_col} => {custom_name}")
            result["step2"].append(
                {
                    "index": idx,
                    "excel_column": excel_col,
                    "db_table": None,
                    "db_column": custom_name,
                    "match_count": 0,
                    "confirm_count": 0,
                    "confirm_total": 0,
                    "confirm_ratio": 0.0,
                    "examples": [],
                    "notes": "",
                }
            )
            continue
        if isinstance(match, dict) and match.get("ambiguous"):
            notes = []
            print(f"  {idx}. {excel_col} => (ambiguous; requires review)")
            candidates = match.get("candidates", [])
            for cand in candidates:
                schema = cand["schema"]
                table = cand["table"]
                column = cand["column"]
                stats = db_column_stats(conn, schema, table, column)
                fk = is_foreign_key(conn, schema, table, column)
                note = (
                    f"{schema}.{table}.{column}"
                    f" type={cand.get('data_type')}"
                    f" matches={cand.get('match_count')}"
                    f" ratio={cand.get('match_ratio'):.2f}"
                    f" avg_len={stats['avg_length']}"
                    f" uniq={stats['uniq_ratio']}"
                    f" fk={fk}"
                )
                notes.append(note)
                print(f"    candidate: {note}")
            result["step2"].append(
                {
                    "index": idx,
                    "excel_column": excel_col,
                    "db_table": None,
                    "db_column": None,
                    "match_count": 0,
                    "confirm_count": 0,
                    "confirm_total": 0,
                    "confirm_ratio": 0.0,
                    "examples": [],
                    "notes": " | ".join(notes),
                    "ambiguous": True,
                }
            )
            continue
        examples = ", ".join(match.get("example_values", [])[:5])
        limit = confirm_sample_limit(excel_col, match.get("data_type"))
        confirm_values = excel_values_for_type(
            col_samples, excel_col, match.get("data_type")
        )[:limit]
        confirm_count, confirm_ratio = confirm_mapping(
            conn,
            match["schema"],
            match["table"],
            match["column"],
            confirm_values,
            data_type=match.get("data_type"),
        )
        print(
            f"  {idx}. {excel_col} => {match['column']}"
            f"  matches={match['match_count']}"
            f"  confirm={confirm_count}/{len(confirm_values)}"
        )
        if examples:
            print(f"    examples: {examples}")
        print(f"    confirm_ratio: {confirm_ratio:.2f}")
        result["step2"].append(
            {
                "index": idx,
                "excel_column": excel_col,
                "db_table": f"{match['schema']}.{match['table']}",
                "db_column": match["column"],
                "match_count": match["match_count"],
                "confirm_count": confirm_count,
                "confirm_total": len(confirm_values),
                "confirm_ratio": confirm_ratio,
                "examples": match.get("example_values", []),
            }
        )

    print("\nProceeding to Step 3 automatically.")

    print("\nStep 3: best table candidates by mapped DB columns")
    if table_candidates:
        table_candidates = [
            row for row in table_candidates if not is_copy_table(row[1])
        ]
        excel_cols = list(col_samples.keys())
        ordered_matches = []
        scored_tables = {}
        for schema, table, matched_count, columns_found, ordered_columns, ordered_types in table_candidates:
            refined = refine_mapping_for_table(
                conn,
                excel_cols,
                col_samples,
                matches,
                schema,
                table,
                ordered_columns or [],
                ordered_types or [],
            )
            mapped_cols = [m["column"] for m in refined.values() if m]
            order_score = ordered_match_score(mapped_cols, ordered_columns or [])
            ordered_matches.append(
                (schema, table, matched_count, columns_found, order_score)
            )

        exact_order = [row for row in ordered_matches if row[4] == row[2]]
        max_matched = max((row[2] for row in ordered_matches), default=0)
        def should_score_candidate(idx, schema, table, matched_count):
            if idx < top_tables:
                return True
            if matched_count == max_matched:
                return True
            return table_name_bias(table, sheet_name, excel_path) != 0.0

        if exact_order:
            top_list = exact_order
            if max_matched:
                top_list = list(
                    {
                        (schema, table, matched_count, columns_found, order_score)
                        for schema, table, matched_count, columns_found, order_score in top_list
                    }
                    | {
                        (schema, table, matched_count, columns_found, order_score)
                        for schema, table, matched_count, columns_found, order_score in ordered_matches
                        if matched_count == max_matched
                    }
                )
            for idx, (schema, table, matched_count, columns_found, order_score) in enumerate(top_list):
                if not should_score_candidate(idx, schema, table, matched_count):
                    continue
                quality = score_table_candidate(
                    conn,
                    schema,
                    table,
                    col_samples,
                    matches,
                    sheet_name,
                    excel_path,
                )
                scored_tables[(schema, table)] = quality
                print(
                    f"  {schema}.{table}  matched_columns={matched_count}  order_score={order_score}  quality={quality['score']}"
                )
                print(f"    {columns_found}")
                result["step3"]["candidates"].append(
                    {
                        "table": f"{schema}.{table}",
                        "matched_columns": matched_count,
                        "order_score": order_score,
                        "quality_score": quality["score"],
                        "required_missing": quality["required_missing"],
                        "columns_found": columns_found,
                    }
                )
        else:
            message = (
                "Таблиц с одинаковой последовательностью не найдено, "
                "вывожу лучшие результаты по количеству совпадений колонок"
            )
            print(message)
            result["step3"]["message"] = message
            top_list = ordered_matches
            for idx, (schema, table, matched_count, columns_found, order_score) in enumerate(top_list):
                if not should_score_candidate(idx, schema, table, matched_count):
                    continue
                quality = score_table_candidate(
                    conn,
                    schema,
                    table,
                    col_samples,
                    matches,
                    sheet_name,
                    excel_path,
                )
                scored_tables[(schema, table)] = quality
                print(
                    f"  {schema}.{table}  matched_columns={matched_count}  order_score={order_score}  quality={quality['score']}"
                )
                print(f"    {columns_found}")
                result["step3"]["candidates"].append(
                    {
                        "table": f"{schema}.{table}",
                        "matched_columns": matched_count,
                        "order_score": order_score,
                        "quality_score": quality["score"],
                        "required_missing": quality["required_missing"],
                        "columns_found": columns_found,
                    }
                )

        # Step 4: report the best existing table (no choice prompt).
        best = None
        if top_list:
            ranked = []
            for schema, table, matched_count, _, order_score in top_list:
                quality = scored_tables.get((schema, table))
                quality_score = quality["score"] if quality else 0.0
                name_bias = table_name_bias(table, sheet_name, excel_path)
                name_sim = table_name_similarity(table, sheet_name, excel_path)
                ranked.append(
                    (matched_count, name_bias, name_sim, quality_score, order_score, schema, table)
                )
            ranked.sort(reverse=True)
            top_matched = ranked[0][0]
            top_candidates = [
                (schema, table, matched_count, order_score)
                for matched_count, name_bias, name_sim, quality_score, order_score, schema, table in ranked
                if matched_count == top_matched
            ]
            meaning_pick = choose_best_table_by_meaning(
                [(s, t, m, "", o) for s, t, m, o in top_candidates],
                sheet_name,
                excel_path,
            )
            if meaning_pick:
                best = meaning_pick
        print("\nStep 4: best existing table")
        if best:
            schema, table, matched_count, _, order_score = best
            print(
                f"  Best existing table: {schema}.{table}"
                f"  matched_columns={matched_count}  order_score={order_score}"
            )
            result["step4"]["choice"] = "use_existing"
            result["step4"]["selection"] = f"{schema}.{table}"
            validated_mapping = validate_mapping_for_table(
                conn,
                col_samples,
                list(col_samples.keys()),
                matches,
                schema,
                table,
            )
            if validated_mapping:
                print(f"\nStep 2 (validated against {schema}.{table})")
                result["step2"] = []
                for idx, excel_col in enumerate(col_samples.keys(), 1):
                    match = validated_mapping.get(excel_col)
                    if not match:
                        if is_flag_excel(excel_col):
                            custom_name = f"({custom_flag_name(excel_col)})"
                        elif MANUAL_MAPPINGS.get(excel_col):
                            custom_name = f"({MANUAL_MAPPINGS[excel_col]})"
                        else:
                            custom_name = f"({excel_col})"
                        print(f"  {idx}. {excel_col} => {custom_name}")
                        result["step2"].append(
                            {
                                "index": idx,
                                "excel_column": excel_col,
                                "db_table": None,
                                "db_column": custom_name,
                                "match_count": 0,
                                "confirm_count": 0,
                                "confirm_total": 0,
                                "confirm_ratio": 0.0,
                                "examples": [],
                                "notes": "",
                            }
                        )
                        continue
                    limit = confirm_sample_limit(excel_col, match.get("data_type"))
                    confirm_values = excel_values_for_type(
                        col_samples, excel_col, match.get("data_type")
                    )[:limit]
                    confirm_count, confirm_ratio = confirm_mapping(
                        conn,
                        match["schema"],
                        match["table"],
                        match["column"],
                        confirm_values,
                        data_type=match.get("data_type"),
                    )
                    print(
                        f"  {idx}. {excel_col} => {match['column']}"
                        f"  matches={match['match_count']}"
                        f"  confirm={confirm_count}/{len(confirm_values)}"
                    )
                    if match.get("example_values"):
                        examples = ", ".join(match.get("example_values", [])[:5])
                        print(f"    examples: {examples}")
                    print(f"    confirm_ratio: {confirm_ratio:.2f}")
                    result["step2"].append(
                        {
                            "index": idx,
                            "excel_column": excel_col,
                            "db_table": f"{match['schema']}.{match['table']}",
                            "db_column": match["column"],
                            "match_count": match["match_count"],
                            "confirm_count": confirm_count,
                            "confirm_total": len(confirm_values),
                            "confirm_ratio": confirm_ratio,
                            "examples": match.get("example_values", []),
                        }
                    )
        else:
            print("No suitable existing table found.")
    else:
        print("  No table candidates found.")
        result["step3"]["message"] = "No table candidates found."

    print("\nColumn mapping suggestions:")
    for excel_col, values in col_samples.items():
        print(f"  Excel column: {excel_col} (samples={len(values['values'])})")
        items = matches.get(excel_col, [])[:top_n]
        if not items:
            print("    No matches")
            continue
        for item in items:
            schema = item["schema"]
            table = item["table"]
            column = item["column"]
            count = item["match_count"]
            ratio = item["match_ratio"]
            print(f"    {schema}.{table}.{column}  matches={count}  ratio={ratio:.2f}")
            result["suggestions"].append(
                {
                    "excel_column": excel_col,
                    "db_table": f"{schema}.{table}",
                    "db_column": column,
                    "match_count": count,
                    "match_ratio": ratio,
                }
            )
    return result


def write_outputs(results, excel_path):
    base = os.path.splitext(os.path.basename(excel_path))[0]
    output_dir = os.path.join(os.path.dirname(excel_path), f"{base}_mapping_json")
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"{base}_mapping_{ts}.json")
    csv_path = os.path.join(output_dir, f"{base}_mapping_{ts}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        for sheet in results:
            sheet_name = sheet.get("sheet_name") or ""
            selected_table = sheet.get("step4", {}).get("selection") or ""

            writer.writerow(["Наименование", "Значение"])
            writer.writerow(["Таблица АСУД", os.path.basename(excel_path)])
            writer.writerow(["Правило выбора", ""])
            writer.writerow(["Таблица СЭДО", selected_table])
            writer.writerow([])

            writer.writerow(
                [
                    "Наименование",
                    "Поле АСУД",
                    "Отображение АСУД",
                    "Поле СЭДО",
                    "Отображение СЭДО",
                    "Значение по умолчанию",
                    "Совпадений",
                    "Подтверждение",
                ]
            )
            for row in sheet.get("step2", []):
                excel_col = row.get("excel_column") or ""
                db_col = row.get("db_column") or ""
                match_count = row.get("match_count") or ""
                confirm_count = row.get("confirm_count") or 0
                confirm_total = row.get("confirm_total") or 0
                confirm_ratio = row.get("confirm_ratio")
                notes = row.get("notes") or ""
                if confirm_total:
                    confirm_text = f"{confirm_count}/{confirm_total}"
                    if confirm_ratio is not None:
                        confirm_text = f"{confirm_text} ({confirm_ratio:.2f})"
                else:
                    confirm_text = ""

                writer.writerow(
                    [
                        excel_col,
                        excel_col,
                        "",
                        db_col,
                        notes,
                        "",
                        match_count,
                        confirm_text,
                    ]
                )
            writer.writerow([])

    print(f"\nSaved JSON: {json_path}")
    print(f"Saved CSV: {csv_path}")


def main():
    profile_id = os.getenv("MATCHING_PROFILE_ID", DEFAULT_PROFILE_ID).strip() or DEFAULT_PROFILE_ID
    apply_runtime_profile(profile_id)
    root = os.getenv("MAPPING_ROOT", "/Users/evgeniy/Desktop/ID2")
    print(f"Using matching profile: {PROFILE_META.get('company_name')} ({PROFILE_ID})")
    excel_path = choose_excel_file(root)
    print("Selected:", excel_path)
    global HISTORY_MAP
    HISTORY_MAP = load_mapping_history(root, excel_path)

    print("Reading Excel...")
    sheets = extract_excel_samples(excel_path, max_values=None)
    if not sheets:
        print("No non-empty columns found in the Excel file.")
        return

    print("Connecting to Postgres...")
    conn = connect_db()

    include_env = os.getenv("INCLUDE_SCHEMAS", "").strip()
    include_schemas = [s.strip() for s in include_env.split(",") if s.strip()] if include_env else ["public"]
    exclude_schemas = []

    global FK_COLUMNS
    print("Loading foreign key columns...")
    FK_COLUMNS = load_fk_columns(conn, include_schemas=include_schemas, exclude_schemas=exclude_schemas)
    print(f"Found {len(FK_COLUMNS)} foreign key columns.")

    print("Loading text columns list...")
    text_columns = list_text_columns(conn, include_schemas=include_schemas, exclude_schemas=exclude_schemas)
    print(f"Found {len(text_columns)} text columns.")
    print("Loading name columns list...")
    name_columns = list_name_columns(conn, include_schemas=include_schemas, exclude_schemas=exclude_schemas)
    print(f"Found {len(name_columns)} name columns.")

    try:
        results = []
        for sheet_name, col_samples in sheets.items():
            sheet_no_values = True
            for payload in col_samples.values():
                if payload.get("values") or payload.get("date_values") or payload.get("bool_values"):
                    sheet_no_values = False
                    break
            matches = scan_matches_for_sheet(conn, sheet_name, col_samples, text_columns, name_columns)
            has_value_matches = False
            for items in matches.values():
                for item in items:
                    if item.get("match_count", 0) > 0:
                        has_value_matches = True
                        break
                if has_value_matches:
                    break
            # Use best match for every Excel column that has any hit.
            min_count = 0 if (sheet_no_values or not has_value_matches) else 1
            mapping = pick_column_mappings(matches, min_match_count=min_count, min_match_ratio=0.0)
            mapped_db_cols = sorted(
                {
                    m["column"]
                    for m in mapping.values()
                    if isinstance(m, dict) and m.get("column")
                }
            )
            table_candidates = find_best_tables_by_columns(
                conn,
                mapped_db_cols,
                include_schemas=include_schemas,
                exclude_schemas=exclude_schemas,
            )
            results.append(
                print_results(conn, sheet_name, col_samples, matches, mapping, table_candidates, excel_path)
            )
        write_outputs(results, excel_path)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
