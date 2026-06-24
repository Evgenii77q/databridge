#!/usr/bin/env python3
import os
import sys
import json
import getpass
import subprocess
import uuid
import re
import hashlib
import math
from datetime import datetime
from collections import OrderedDict

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values


EXCLUDE_DIRS = {
    ".git",
    ".Trash",
    ".DS_Store",
    "Library",
    "node_modules",
    "__pycache__",
}


def find_files(root, exts, max_files=200):
    matches = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in filenames:
            lower = name.lower()
            if any(lower.endswith(ext) for ext in exts):
                matches.append(os.path.join(dirpath, name))
                if len(matches) >= max_files:
                    return matches
    return matches


def choose_file(files, label):
    if not files:
        print(f"No files found for {label}.")
        sys.exit(1)
    print(f"Found {label} files:")
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


def latest_mapping_json(root):
    json_files = []
    for dirpath, _, filenames in os.walk(root):
        if not dirpath.endswith("_mapping_json"):
            continue
        for name in filenames:
            if name.lower().endswith(".json"):
                path = os.path.join(dirpath, name)
                json_files.append(path)
    if not json_files:
        return None
    return max(json_files, key=lambda p: os.path.getmtime(p))


def prompt_default(label, default):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def connect_db():
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    dbname = os.getenv("PGDATABASE", "sedo")
    user = os.getenv("PGUSER", getpass.getuser())
    password = os.getenv("PGPASSWORD", "")

    if os.getenv("PG_NO_PROMPT") == "1":
        print("Using Postgres defaults (PG_NO_PROMPT=1).")
        return psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
        )

    print("Postgres connection params (press Enter to accept defaults)")
    host = prompt_default("Host", host)
    port = prompt_default("Port", port)
    dbname = prompt_default("Database", dbname)
    user = prompt_default("User", user)
    if not password:
        password = getpass.getpass("Password (blank to skip): ")

    return psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)


def load_mapping(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("Mapping JSON format is unexpected (expected a list).")
        sys.exit(1)
    return data


def choose_sheet(mapping_data):
    if len(mapping_data) == 1:
        return mapping_data[0]
    print("Sheets in mapping:")
    for i, sheet in enumerate(mapping_data, 1):
        print(f"  {i}) {sheet.get('sheet_name')}")
    while True:
        choice = input("Select sheet number: ").strip()
        if not choice.isdigit():
            print("Enter a number.")
            continue
        idx = int(choice)
        if 1 <= idx <= len(mapping_data):
            return mapping_data[idx - 1]
        print("Out of range.")


def normalize_mapping_rows(step2_rows):
    rows = []
    for row in step2_rows:
        excel_col = row.get("excel_column")
        db_col = row.get("db_column") or ""
        if not excel_col:
            continue
        if not db_col:
            db_col = f"({excel_col})"
        rows.append(
            {
                "excel_column": excel_col,
                "db_column": db_col,
            }
        )
    return rows


def parse_target_table(sheet):
    step4 = sheet.get("step4") or {}
    selection = step4.get("selection")
    if not selection:
        selection = input("Enter target table (schema.table): ").strip()
    if "." not in selection:
        print("Expected schema.table format.")
        sys.exit(1)
    schema, table = selection.split(".", 1)
    return schema, table


def drop_and_create_copy(conn, schema, table, copy_table):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {schema}.{table}").format(
                schema=sql.Identifier(schema),
                table=sql.Identifier(copy_table),
            )
        )


def existing_columns(conn, schema, table):
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return {row[0] for row in cur.fetchall()}


def table_columns(conn, schema, table):
    query = """
        SELECT
            column_name,
            data_type,
            udt_name,
            character_maximum_length,
            numeric_precision,
            numeric_scale,
            datetime_precision,
            column_default,
            is_nullable,
            ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return [
            {
                "name": row[0],
                "data_type": row[1],
                "udt_name": row[2],
                "char_len": row[3],
                "num_precision": row[4],
                "num_scale": row[5],
                "dt_precision": row[6],
                "default": row[7],
                "nullable": row[8],
                "ordinal": row[9],
            }
            for row in cur.fetchall()
        ]


def column_type_sql(col):
    dtype = col["data_type"]
    udt = col["udt_name"]
    if dtype == "character varying" and col["char_len"]:
        return f"varchar({col['char_len']})"
    if dtype == "character" and col["char_len"]:
        return f"char({col['char_len']})"
    if dtype == "numeric" and col["num_precision"]:
        scale = col["num_scale"] or 0
        return f"numeric({col['num_precision']},{scale})"
    if dtype in ("timestamp without time zone", "timestamp with time zone"):
        return dtype
    if udt in ("int2", "int4", "int8"):
        return {"int2": "smallint", "int4": "integer", "int8": "bigint"}[udt]
    if udt == "float8":
        return "double precision"
    if udt == "float4":
        return "real"
    return dtype


def shorten_identifier(name, max_len=63):
    if len(name) <= max_len:
        return name
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    prefix = name[: max_len - 7]
    return f"{prefix}_{digest}"


def infer_column_type(excel_series, excel_col, db_col):
    lower = excel_col.lower()
    col = db_col.lower()
    if "флаг" in lower or col.startswith("dsb_") or col.startswith("i_is_"):
        return "boolean"
    if col.startswith("dsdt_") or "дата" in lower or "date" in lower or "time" in lower:
        return "timestamp without time zone"
    if col.startswith("dsid_") or col == "r_object_id":
        lengths = [len(str(v)) for v in excel_series if v not in (None, "")]
        if lengths and min(lengths) == max(lengths) and min(lengths) <= 64:
            return f"varchar({min(lengths)})"
        return "text"
    samples = []
    for v in excel_series:
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        samples.append(s)
    if samples and all(s.lower() in ("t", "f", "true", "false", "0", "1", "да", "нет") for s in samples):
        return "boolean"
    date_like = [s for s in samples if re.match(r"^\\d{1,4}[-./]\\d{1,2}[-./]\\d{1,4}", s)]
    if date_like and len(date_like) >= max(1, len(samples) // 2):
        return "timestamp without time zone"
    if samples and all(s.replace("-", "").isdigit() for s in samples):
        return "bigint"
    return "text"


def build_desired_order(base_columns, mapping_rows):
    desired = []
    base_set = {c["name"] for c in base_columns}
    for row in mapping_rows:
        db_col = row["db_column"]
        if db_col.startswith("(") and db_col.endswith(")"):
            db_col = db_col[1:-1].strip()
        db_col = db_col.strip()
        if not db_col:
            continue
        if db_col not in desired:
            desired.append(db_col)
    for col in base_columns:
        if col["name"] not in desired:
            desired.append(col["name"])
    return desired, base_set


def create_copy_with_order(conn, schema, table, copy_table, mapping_rows, df):
    base_columns = table_columns(conn, schema, table)
    desired_order, base_set = build_desired_order(base_columns, mapping_rows)
    base_by_name = {c["name"]: c for c in base_columns}

    drop_and_create_copy(conn, schema, table, copy_table)

    column_defs = []
    for name in desired_order:
        if name in base_set:
            col = base_by_name[name]
            col_type = column_type_sql(col)
            parts = [sql.Identifier(name).as_string(conn), col_type]
            if col["default"]:
                parts.append(f"DEFAULT {col['default']}")
            if col["nullable"] == "NO":
                parts.append("NOT NULL")
            column_defs.append(" ".join(parts))
        else:
            excel_col = next(
                (r["excel_column"] for r in mapping_rows if r["db_column"].strip("()") == name),
                None,
            )
            excel_series = df[excel_col] if excel_col in df.columns else []
            col_type = infer_column_type(excel_series, excel_col or name, name)
            column_defs.append(
                f"{sql.Identifier(name).as_string(conn)} {col_type}"
            )

    create_sql = sql.SQL(
        "CREATE TABLE {schema}.{table} ({cols})"
    ).format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(copy_table),
        cols=sql.SQL(", ").join(sql.SQL(c) for c in column_defs),
    )
    with conn.cursor() as cur:
        cur.execute(create_sql)

    pk_info = fetch_primary_key_info(conn, schema, table)
    if pk_info:
        pk_cols = [sql.Identifier(c["name"]) for c in pk_info]
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("ALTER TABLE {schema}.{table} ADD PRIMARY KEY ({cols})").format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(copy_table),
                    cols=sql.SQL(", ").join(pk_cols),
                )
            )
    conn.commit()
    copy_constraints_and_indexes(conn, schema, table, copy_table)
    return desired_order


def fetch_constraints(conn, schema, table):
    query = """
        SELECT conname, contype, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = %s
          AND t.relname = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return cur.fetchall()


def fetch_nonconstraint_indexes(conn, schema, table):
    query = """
        SELECT
            i.indexrelid::regclass::text AS indexname,
            pg_get_indexdef(i.indexrelid) AS indexdef,
            (con.oid IS NOT NULL) AS is_constraint
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid
        WHERE n.nspname = %s
          AND t.relname = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return [row for row in cur.fetchall() if not row[2]]


def replace_indexdef_table(indexdef, schema, table, copy_table):
    patterns = [
        f" ON {schema}.{table} ",
        f' ON "{schema}"."{table}" ',
        f" ON {table} ",
        f' ON "{table}" ',
    ]
    replacement = f" ON {schema}.{copy_table} "
    for pat in patterns:
        if pat in indexdef:
            return indexdef.replace(pat, replacement)
    return indexdef


def copy_constraints_and_indexes(conn, schema, table, copy_table):
    constraints = fetch_constraints(conn, schema, table)
    for conname, contype, condef in constraints:
        # Skip primary keys (added separately) and foreign keys.
        # FK constraints on the staging copy table cause ForeignKeyViolation
        # when copy_base_data runs, because the copy table's FK references
        # point to production tables whose rows may differ from what we insert.
        if contype in ("p", "f"):
            continue
        new_name = shorten_identifier(f"{conname}_copy")
        stmt = sql.SQL("ALTER TABLE {schema}.{table} ADD CONSTRAINT {cname} {cdef}").format(
            schema=sql.Identifier(schema),
            table=sql.Identifier(copy_table),
            cname=sql.Identifier(new_name),
            cdef=sql.SQL(condef),
        )
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT sp_constraint")
                cur.execute(stmt)
            except psycopg2.Error:
                cur.execute("ROLLBACK TO SAVEPOINT sp_constraint")
            finally:
                cur.execute("RELEASE SAVEPOINT sp_constraint")

    indexes = fetch_nonconstraint_indexes(conn, schema, table)
    for indexname, indexdef, _ in indexes:
        new_name = shorten_identifier(f"{indexname}_copy")
        new_def = re.sub(
            r"^CREATE( UNIQUE)? INDEX \\S+ ON",
            f"CREATE\\1 INDEX {new_name} ON",
            indexdef,
        )
        new_def = replace_indexdef_table(new_def, schema, table, copy_table)
        with conn.cursor() as cur:
            try:
                cur.execute("SAVEPOINT sp_index")
                cur.execute(new_def)
            except psycopg2.Error:
                cur.execute("ROLLBACK TO SAVEPOINT sp_index")
            finally:
                cur.execute("RELEASE SAVEPOINT sp_index")


def choose_excel_file_for_mapping(json_path):
    mapping_dir = os.path.dirname(json_path)
    parent = os.path.dirname(mapping_dir)
    files = find_files(parent, [".xlsx", ".xls"])
    return choose_file(files, "Excel")


def load_excel_sheet(excel_path, sheet_name):
    df = pd.read_excel(excel_path, sheet_name=sheet_name, dtype=str, keep_default_na=False)
    return df


def clean_cell(val):
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if s.lower() == "null":
        return None
    return s


def normalize_bool_value(val):
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("t", "true", "1", "yes", "y", "да"):
        return "t"
    if s in ("f", "false", "0", "no", "n", "нет"):
        return "f"
    return None


DATE_TYPES = {
    "date",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
}

INT_TYPES = {"smallint", "integer", "bigint"}
NUM_TYPES = {"real", "double precision", "numeric", "decimal"}


def normalize_numeric_value(val, integer=False):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                return None
        return int(val) if integer else float(val)
    s = str(val).strip()
    if not s:
        return None
    s = s.replace(" ", "").replace(",", ".")
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", s) is None:
        return None
    try:
        num = float(s)
    except Exception:
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return int(num) if integer else num


def normalize_datetime_value(val, date_only=False):
    if val is None:
        return None
    # Native datetime / timestamp from pandas/excel.
    if isinstance(val, datetime):
        return val.date() if date_only else val

    s = str(val).strip()
    if not s:
        return None

    dt = None
    # Avoid turning arbitrary short numbers (like "333") into timestamps.
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        try:
            num = float(s.replace(",", "."))
            # Excel serial date window.
            if 20_000 <= num <= 80_000:
                dt = pd.to_datetime(num, errors="coerce", unit="D", origin="1899-12-30")
        except Exception:
            dt = None
    else:
        dt = pd.to_datetime(s, errors="coerce", dayfirst=True)

    if dt is None or pd.isna(dt):
        return None
    py_dt = dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt
    return py_dt.date() if date_only else py_dt


def normalize_for_column_type(value, col_type):
    t = (col_type or "").lower().strip()
    if t == "boolean":
        return normalize_bool_value(value)
    if t in DATE_TYPES:
        return normalize_datetime_value(value, date_only=(t == "date"))
    if t in INT_TYPES:
        return normalize_numeric_value(value, integer=True)
    if t in NUM_TYPES:
        return normalize_numeric_value(value, integer=False)
    return value


def build_insert_data(df, mapping_rows, column_types):
    columns = []
    for row in mapping_rows:
        db_col = row["db_column"]
        if db_col.startswith("(") and db_col.endswith(")"):
            db_col = db_col[1:-1].strip()
        columns.append(db_col)

    columns = list(OrderedDict.fromkeys(columns))
    data = []
    for _, rec in df.iterrows():
        row_values = []
        for col_name in columns:
            excel_col = None
            for row in mapping_rows:
                target = row["db_column"]
                if target.startswith("(") and target.endswith(")"):
                    target = target[1:-1].strip()
                if target == col_name:
                    excel_col = row["excel_column"]
                    break
            if excel_col is None or excel_col not in df.columns:
                row_values.append(None)
                continue
            value = clean_cell(rec.get(excel_col))
            col_type = column_types.get(col_name, "").lower()
            value = normalize_for_column_type(value, col_type)
            row_values.append(value)
        data.append(row_values)
    return columns, data


def insert_rows(conn, schema, table, columns, rows, page_size=1000):
    if not columns or not rows:
        return
    cols_sql = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    query = sql.SQL("INSERT INTO {schema}.{table} ({cols}) VALUES %s").format(
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        cols=cols_sql,
    )
    with conn.cursor() as cur:
        execute_values(cur, query.as_string(conn), rows, page_size=page_size)


def fetch_primary_key_info(conn, schema, table):
    query = """
        SELECT kcu.column_name, c.data_type, c.column_default
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.columns c
          ON c.table_schema = kcu.table_schema
         AND c.table_name = kcu.table_name
         AND c.column_name = kcu.column_name
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
          AND tc.table_name = %s
        ORDER BY kcu.ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        return [
            {"name": row[0], "data_type": row[1], "default": row[2]}
            for row in cur.fetchall()
        ]


def choose_upsert_key(pk_info, mapped_columns):
    if not pk_info:
        return None
    if len(pk_info) == 1:
        pk_name = pk_info[0]["name"]
        if pk_name in mapped_columns:
            return pk_name
        return pk_name
    mapped = [pk["name"] for pk in pk_info if pk["name"] in mapped_columns]
    if len(mapped) == 1:
        return mapped[0]
    return None


def ensure_primary_key_values(columns, rows, pk_info):
    if not pk_info:
        return columns, rows
    col_index = {name: idx for idx, name in enumerate(columns)}
    for pk in pk_info:
        name = pk["name"]
        if name in col_index:
            continue
        if pk["default"]:
            continue
        dtype = (pk["data_type"] or "").lower()
        if dtype in ("integer", "bigint", "smallint"):
            values = [1_000_000 + i for i in range(len(rows))]
        elif dtype == "uuid":
            values = [str(uuid.uuid4()) for _ in range(len(rows))]
        else:
            values = [uuid.uuid4().hex for _ in range(len(rows))]
        columns.append(name)
        for i, row in enumerate(rows):
            row.append(values[i])
        col_index[name] = len(columns) - 1
    return columns, rows


def relax_not_null_constraints(conn, schema, table, pk_columns):
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND is_nullable = 'NO'
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        required = {row[0] for row in cur.fetchall()}
    to_relax = sorted(c for c in required if c not in pk_columns)
    if not to_relax:
        return
    with conn.cursor() as cur:
        for col_name in to_relax:
            cur.execute(
                sql.SQL("ALTER TABLE {schema}.{table} ALTER COLUMN {col} DROP NOT NULL").format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    col=sql.Identifier(col_name),
                )
            )


def widen_varchar_columns(conn, schema, table, columns):
    query = """
        SELECT column_name, data_type, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table, list(columns)))
        rows = cur.fetchall()
    to_widen = [
        name
        for name, dtype, max_len in rows
        if dtype == "character varying" and max_len is not None
    ]
    if not to_widen:
        return
    with conn.cursor() as cur:
        for col_name in to_widen:
            cur.execute(
                sql.SQL("ALTER TABLE {schema}.{table} ALTER COLUMN {col} TYPE TEXT").format(
                    schema=sql.Identifier(schema),
                    table=sql.Identifier(table),
                    col=sql.Identifier(col_name),
                )
            )


def column_types_map(conn, schema, table, columns):
    query = """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table, list(columns)))
        return {row[0]: row[1] for row in cur.fetchall()}


def copy_base_data(conn, schema, table, copy_table, copy_columns, base_columns):
    base_set = {c["name"] for c in base_columns}
    select_parts = []
    for col in copy_columns:
        if col in base_set:
            select_parts.append(sql.Identifier(col))
        else:
            select_parts.append(sql.SQL("NULL"))
    query = sql.SQL(
        "INSERT INTO {schema}.{copy} ({cols}) SELECT {selects} FROM {schema}.{base}"
    ).format(
        schema=sql.Identifier(schema),
        copy=sql.Identifier(copy_table),
        base=sql.Identifier(table),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in copy_columns),
        selects=sql.SQL(", ").join(select_parts),
    )
    with conn.cursor() as cur:
        cur.execute(query)


def create_temp_table(conn, temp_name, column_types):
    cols = []
    for name, col_type in column_types.items():
        cols.append(
            sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(col_type))
        )
    drop_query = sql.SQL("DROP TABLE IF EXISTS {temp}").format(
        temp=sql.Identifier(temp_name)
    )
    query = sql.SQL("CREATE TEMP TABLE {temp} ({cols})").format(
        temp=sql.Identifier(temp_name),
        cols=sql.SQL(", ").join(cols),
    )
    with conn.cursor() as cur:
        cur.execute(drop_query)
        cur.execute(query)


def upsert_from_temp(conn, schema, copy_table, temp_name, key_col, columns):
    set_parts = []
    for col in columns:
        if col == key_col:
            continue
        set_parts.append(
            sql.SQL("{col} = COALESCE(src.{col}, tgt.{col})").format(
                col=sql.Identifier(col)
            )
        )
    update_query = sql.SQL(
        """
        UPDATE {schema}.{copy} AS tgt
        SET {sets}
        FROM {temp} AS src
        WHERE tgt.{key} = src.{key}
        """
    ).format(
        schema=sql.Identifier(schema),
        copy=sql.Identifier(copy_table),
        temp=sql.Identifier(temp_name),
        key=sql.Identifier(key_col),
        sets=sql.SQL(", ").join(set_parts),
    )
    insert_cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    insert_query = sql.SQL(
        """
        INSERT INTO {schema}.{copy} ({cols})
        SELECT {cols} FROM {temp} src
        WHERE src.{key} IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM {schema}.{copy} tgt WHERE tgt.{key} = src.{key}
          )
        """
    ).format(
        schema=sql.Identifier(schema),
        copy=sql.Identifier(copy_table),
        temp=sql.Identifier(temp_name),
        key=sql.Identifier(key_col),
        cols=insert_cols,
    )
    with conn.cursor() as cur:
        cur.execute(update_query)
        cur.execute(insert_query)


def main():
    root = "/Users/evgeniy/Desktop/ID2"
    script1 = os.path.join(os.path.dirname(__file__), "match_excel_to_pg.py")
    excel_override = os.getenv("EXCEL_FILE", "").strip()
    mapping_override = os.getenv("MAPPING_JSON", "").strip()
    table_override = os.getenv("TARGET_TABLE", "").strip()
    sheet_override = os.getenv("SHEET_NAME", "").strip()
    skip_script1 = os.getenv("SKIP_SCRIPT1", "").strip() == "1"
    env = os.environ.copy()
    env["PG_NO_PROMPT"] = "1"
    if not skip_script1:
        if excel_override:
            env["EXCEL_FILE"] = excel_override
        subprocess.run([sys.executable, script1], check=True, env=env)
    excel_path = None
    latest = mapping_override or latest_mapping_json(root)
    if not latest:
        print("No mapping JSON found after running script 1.")
        return
    json_path = latest
    print("Using latest JSON:", json_path)
    print("Selected JSON:", json_path)

    mapping_data = load_mapping(json_path)
    if sheet_override:
        sheet = next((s for s in mapping_data if s.get("sheet_name") == sheet_override), None)
        if not sheet:
            print(f"Sheet '{sheet_override}' not found in mapping JSON.")
            return
    else:
        sheet = mapping_data[0] if mapping_data else None
        if not sheet:
            print("No sheets available in mapping JSON.")
            return
    excel_path = sheet.get("excel_path")
    sheet_name = sheet.get("sheet_name")
    step2 = sheet.get("step2") or []
    mapping_rows = normalize_mapping_rows(step2)

    print("\nMapping preview (Excel -> DB):")
    for row in mapping_rows:
        print(f"  {row['excel_column']} -> {row['db_column']}")
    print("Confirm mapping and continue? [auto-yes]")

    if table_override:
        if "." not in table_override:
            print("TARGET_TABLE must be schema.table")
            return
        schema, table = table_override.split(".", 1)
    else:
        schema, table = parse_target_table(sheet)
    copy_table = f"{table}_copy"
    print(f"\nTarget table: {schema}.{table}")
    print(f"Copy table: {schema}.{copy_table}")

    if excel_override:
        excel_path = excel_override
    if not excel_path:
        excel_files = find_files(root, [".xlsx", ".xls"])
        excel_path = choose_file(excel_files, "Excel for script 2")
    print("Selected Excel:", excel_path)
    print("Loading Excel sheet:", sheet_name)
    df = load_excel_sheet(excel_path, sheet_name)

    print("Connecting to Postgres...")
    conn = connect_db()
    try:
        print("Creating copy table (drop/recreate with Excel order)...")
        copy_columns = create_copy_with_order(conn, schema, table, copy_table, mapping_rows, df)
        base_columns = table_columns(conn, schema, table)
        print("Copying base table data into copy...")
        copy_base_data(conn, schema, table, copy_table, copy_columns, base_columns)
        pk_info = fetch_primary_key_info(conn, schema, copy_table)

        key_col = None
        mapped_columns = []
        for row in mapping_rows:
            db_col = row["db_column"]
            if db_col.startswith("(") and db_col.endswith(")"):
                db_col = db_col[1:-1].strip()
            db_col = db_col.strip()
            if db_col and db_col not in mapped_columns:
                mapped_columns.append(db_col)
        key_col = choose_upsert_key(pk_info, mapped_columns)
        if not key_col:
            if "r_object_id" in mapped_columns:
                key_col = "r_object_id"
                print("Using fallback key: r_object_id")
            else:
                print("No suitable primary key found for upsert; aborting.")
                return
        print(f"Using key column for upsert: {key_col}")

        print("Preparing rows...")
        column_types = column_types_map(conn, schema, copy_table, mapped_columns)
        columns, rows = build_insert_data(df, mapping_rows, column_types)
        pk_columns = {pk["name"] for pk in pk_info}
        print("Relaxing NOT NULL constraints (except primary key columns)...")
        relax_not_null_constraints(conn, schema, copy_table, pk_columns)
        print("Widening VARCHAR columns used for insert...")
        widen_varchar_columns(conn, schema, copy_table, set(columns))

        temp_name = "tmp_excel_load"
        print("Creating temp table for upsert...")
        temp_types = {col: column_types.get(col, "text") for col in columns}
        create_temp_table(conn, temp_name, temp_types)
        print(f"Loading {len(rows)} rows into temp table...")
        insert_rows(conn, "pg_temp", temp_name, columns, rows)
        print("Upserting into copy table...")
        upsert_from_temp(conn, schema, copy_table, temp_name, key_col, columns)
        conn.commit()
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
