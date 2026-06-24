import hashlib
import hmac
import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request


PERMISSIONS = ("read", "write", "delete", "admin")


class AccessDenied(Exception):
    pass


class NotFound(Exception):
    pass


class ECMCore:
    """
    Minimal ECM/EDM core with versioning, metadata, ACL, audit, search, workflow.
    SQLite-backed to keep deployment simple.
    """

    def __init__(self, db_path: str, storage_dir: Optional[str] = None) -> None:
        self.db_path = db_path
        self.storage_dir = storage_dir
        if self.storage_dir:
            os.makedirs(self.storage_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS doc_types (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS doc_type_fields (
                    doc_type TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    field_type TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    allowed_values TEXT,
                    FOREIGN KEY(doc_type) REFERENCES doc_types(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS doc_type_fields_unique
                    ON doc_type_fields(doc_type, field_name);
                CREATE TABLE IF NOT EXISTS doc_type_acls (
                    doc_type TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    FOREIGN KEY(doc_type) REFERENCES doc_types(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS doc_type_acls_unique
                    ON doc_type_acls(doc_type, principal, permission);
                CREATE TABLE IF NOT EXISTS groups (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_members (
                    group_name TEXT NOT NULL,
                    member TEXT NOT NULL,
                    FOREIGN KEY(group_name) REFERENCES groups(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS group_members_unique
                    ON group_members(group_name, member);
                CREATE TABLE IF NOT EXISTS roles (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS role_members (
                    role_name TEXT NOT NULL,
                    member TEXT NOT NULL,
                    FOREIGN KEY(role_name) REFERENCES roles(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS role_members_unique
                    ON role_members(role_name, member);
                CREATE TABLE IF NOT EXISTS role_permissions (
                    role_name TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    FOREIGN KEY(role_name) REFERENCES roles(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS role_permissions_unique
                    ON role_permissions(role_name, permission);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    doc_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    current_version_id TEXT NOT NULL,
                    deleted_at REAL,
                    retention_until REAL,
                    legal_hold INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    content_path TEXT,
                    content_size INTEGER,
                    checksum TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS versions_unique
                    ON versions(document_id, version_number);
                CREATE TABLE IF NOT EXISTS metadata (
                    document_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS metadata_doc_key
                    ON metadata(document_id, key);
                CREATE TABLE IF NOT EXISTS acls (
                    document_id TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS acls_unique
                    ON acls(document_id, principal, permission);
                CREATE TABLE IF NOT EXISTS audits (
                    id TEXT PRIMARY KEY,
                    document_id TEXT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    document_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    assigned_to TEXT,
                    template_name TEXT,
                    due_at REAL,
                    started_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_template_states (
                    template_name TEXT NOT NULL,
                    state_name TEXT NOT NULL,
                    next_states TEXT NOT NULL,
                    sla_hours REAL,
                    assignee_role TEXT,
                    FOREIGN KEY(template_name) REFERENCES workflow_templates(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS workflow_template_states_unique
                    ON workflow_template_states(template_name, state_name);
                CREATE TABLE IF NOT EXISTS signatures (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    signer TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    algorithm TEXT NOT NULL,
                    note TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id)
                );
                CREATE TABLE IF NOT EXISTS webhooks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    events TEXT NOT NULL,
                    secret TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_status INTEGER,
                    last_error TEXT,
                    last_attempt_at REAL,
                    last_success_at REAL,
                    created_at REAL NOT NULL
                );
                """
            )
            self._ensure_schema_version(conn)
            self._ensure_documents_columns(conn)
            self._ensure_versions_columns(conn)
            self._ensure_workflows_columns(conn)
            self._ensure_doc_fts(conn)

    def _ensure_schema_version(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()
        if row["version"] is None:
            conn.execute(
                "INSERT INTO schema_versions (version, applied_at) VALUES (?, ?)",
                (1, time.time()),
            )
        self._bump_schema_version(conn, 2)

    def _bump_schema_version(self, conn: sqlite3.Connection, target_version: int) -> None:
        row = conn.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()
        current = row["version"] or 1
        if current >= target_version:
            return
        conn.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (?, ?)",
            (target_version, time.time()),
        )

    def _ensure_documents_columns(self, conn: sqlite3.Connection) -> None:
        cols = conn.execute("PRAGMA table_info(documents)").fetchall()
        col_names = {row["name"] for row in cols}
        if "deleted_at" not in col_names:
            conn.execute("ALTER TABLE documents ADD COLUMN deleted_at REAL")
        if "retention_until" not in col_names:
            conn.execute("ALTER TABLE documents ADD COLUMN retention_until REAL")
        if "legal_hold" not in col_names:
            conn.execute("ALTER TABLE documents ADD COLUMN legal_hold INTEGER NOT NULL DEFAULT 0")

    def _ensure_versions_columns(self, conn: sqlite3.Connection) -> None:
        cols = conn.execute("PRAGMA table_info(versions)").fetchall()
        col_names = {row["name"] for row in cols}
        if "content_path" not in col_names:
            conn.execute("ALTER TABLE versions ADD COLUMN content_path TEXT")
        if "content_size" not in col_names:
            conn.execute("ALTER TABLE versions ADD COLUMN content_size INTEGER")

    def _ensure_workflows_columns(self, conn: sqlite3.Connection) -> None:
        cols = conn.execute("PRAGMA table_info(workflows)").fetchall()
        col_names = {row["name"] for row in cols}
        if "template_name" not in col_names:
            conn.execute("ALTER TABLE workflows ADD COLUMN template_name TEXT")
        if "due_at" not in col_names:
            conn.execute("ALTER TABLE workflows ADD COLUMN due_at REAL")
        if "started_at" not in col_names:
            conn.execute("ALTER TABLE workflows ADD COLUMN started_at REAL")

    def _ensure_doc_fts(self, conn: sqlite3.Connection) -> None:
        cols = conn.execute("PRAGMA table_info(doc_fts)").fetchall()
        col_names = {row["name"] for row in cols}
        if not col_names:
            conn.execute(
                "CREATE VIRTUAL TABLE doc_fts USING fts5(document_id, title, content, metadata, "
                "tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')"
            )
            self._rebuild_fts(conn)
            return
        if "metadata" not in col_names or "title" not in col_names:
            conn.execute("DROP TABLE doc_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE doc_fts USING fts5(document_id, title, content, metadata, "
                "tokenize='unicode61 remove_diacritics 2', prefix='2 3 4')"
            )
            self._rebuild_fts(conn)

    def _rebuild_fts(self, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM doc_fts")
        rows = conn.execute(
            """
            SELECT documents.id, documents.title, versions.content, versions.content_path
            FROM documents
            JOIN versions ON versions.id = documents.current_version_id
            """
        ).fetchall()
        for row in rows:
            content = self._read_content(row["content"], row["content_path"])
            metadata = self._get_metadata(conn, row["id"])
            conn.execute(
                "INSERT INTO doc_fts (document_id, title, content, metadata) VALUES (?, ?, ?, ?)",
                (
                    row["id"],
                    row["title"],
                    self._content_to_text(content),
                    self._metadata_to_text(metadata),
                ),
            )

    def create_document(
        self,
        *,
        owner: str,
        title: str,
        doc_type: str,
        content: bytes,
        metadata: Optional[Dict[str, str]] = None,
        acl: Optional[Iterable[Tuple[str, str]]] = None,
    ) -> str:
        doc_id = self._new_id("doc")
        version_id = self._new_id("ver")
        created_at = time.time()
        checksum = self._checksum(content)
        stored_content, content_path, content_size = self._store_content(
            content, doc_id, 1, checksum
        )

        with self._connect() as conn:
            self._validate_metadata(conn, doc_type, metadata or {})
            conn.execute(
                """
                INSERT INTO documents
                    (id, doc_type, title, owner, status, current_version_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?)
                """,
                (doc_id, doc_type, title, owner, version_id, created_at, created_at),
            )
            conn.execute(
                """
                INSERT INTO versions
                    (id, document_id, version_number, content, content_path, content_size, checksum, created_at)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (version_id, doc_id, stored_content, content_path, content_size, checksum, created_at),
            )
            conn.execute(
                "INSERT INTO doc_fts (document_id, title, content, metadata) VALUES (?, ?, ?, ?)",
                (
                    doc_id,
                    title,
                    self._content_to_text(content),
                    self._metadata_to_text(metadata or {}),
                ),
            )
            self._upsert_metadata(conn, doc_id, metadata or {})
            self._init_acl(conn, doc_id, owner, acl)
            self._apply_doc_type_acl(conn, doc_id, doc_type)
            self._audit(conn, doc_id, owner, "create", {"title": title})
            self._emit_webhooks(
                "document.created",
                {"doc_id": doc_id, "doc_type": doc_type, "title": title, "owner": owner},
            )
        return doc_id

    def get_document(
        self, *, doc_id: str, actor: str, include_deleted: bool = False
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            doc = conn.execute(
                "SELECT * FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            if doc["deleted_at"] is not None and not include_deleted:
                raise NotFound(f"document {doc_id} not found")
            version = conn.execute(
                "SELECT * FROM versions WHERE id = ?",
                (doc["current_version_id"],),
            ).fetchone()
            meta = self._get_metadata(conn, doc_id)
            content = self._read_content(version["content"], version["content_path"])
            return {
                "id": doc["id"],
                "doc_type": doc["doc_type"],
                "title": doc["title"],
                "owner": doc["owner"],
                "status": doc["status"],
                "version": version["version_number"],
                "content": content,
                "checksum": version["checksum"],
                "metadata": meta,
                "deleted_at": doc["deleted_at"],
                "retention_until": doc["retention_until"],
                "legal_hold": bool(doc["legal_hold"]),
                "created_at": doc["created_at"],
                "updated_at": doc["updated_at"],
            }

    def update_content(
        self,
        *,
        doc_id: str,
        actor: str,
        content: bytes,
        metadata: Optional[Dict[str, str]] = None,
    ) -> int:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "write")
            doc = conn.execute(
                "SELECT current_version_id FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            last_version = conn.execute(
                """
                SELECT version_number FROM versions
                WHERE id = ?
                """,
                (doc["current_version_id"],),
            ).fetchone()
            next_version = int(last_version["version_number"]) + 1
            version_id = self._new_id("ver")
            created_at = time.time()
            checksum = self._checksum(content)
            stored_content, content_path, content_size = self._store_content(
                content, doc_id, next_version, checksum
            )
            conn.execute(
                """
                INSERT INTO versions
                    (id, document_id, version_number, content, content_path, content_size, checksum, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    doc_id,
                    next_version,
                    stored_content,
                    content_path,
                    content_size,
                    checksum,
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE documents
                SET current_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (version_id, created_at, doc_id),
            )
            conn.execute(
                "UPDATE doc_fts SET content = ? WHERE document_id = ?",
                (self._content_to_text(content), doc_id),
            )
            if metadata is not None:
                self._validate_metadata_for_doc(conn, doc_id, metadata)
                self._replace_metadata(conn, doc_id, metadata)
                conn.execute(
                    "UPDATE doc_fts SET metadata = ? WHERE document_id = ?",
                    (self._metadata_to_text(metadata), doc_id),
                )
            self._audit(
                conn,
                doc_id,
                actor,
                "update_content",
                {"version": next_version, "checksum": checksum},
            )
            self._emit_webhooks(
                "document.updated",
                {"doc_id": doc_id, "version": next_version, "checksum": checksum},
            )
            return next_version

    def set_metadata(self, *, doc_id: str, actor: str, metadata: Dict[str, str]) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "write")
            self._validate_metadata_for_doc(conn, doc_id, metadata)
            self._replace_metadata(conn, doc_id, metadata)
            conn.execute(
                "UPDATE doc_fts SET metadata = ? WHERE document_id = ?",
                (self._metadata_to_text(metadata), doc_id),
            )
            self._audit(conn, doc_id, actor, "set_metadata", {"keys": list(metadata.keys())})
            self._emit_webhooks(
                "document.metadata",
                {"doc_id": doc_id, "keys": list(metadata.keys())},
            )

    def add_acl(self, *, doc_id: str, actor: str, principal: str, permission: str) -> None:
        if permission not in PERMISSIONS:
            raise ValueError(f"unknown permission {permission}")
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "admin")
            conn.execute(
                """
                INSERT OR IGNORE INTO acls (document_id, principal, permission)
                VALUES (?, ?, ?)
                """,
                (doc_id, principal, permission),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "add_acl",
                {"principal": principal, "permission": permission},
            )

    def remove_acl(self, *, doc_id: str, actor: str, principal: str, permission: str) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "admin")
            conn.execute(
                """
                DELETE FROM acls WHERE document_id = ? AND principal = ? AND permission = ?
                """,
                (doc_id, principal, permission),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "remove_acl",
                {"principal": principal, "permission": permission},
            )

    def transition_workflow(
        self,
        *,
        doc_id: str,
        actor: str,
        state: str,
        assigned_to: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "write")
            current = conn.execute(
                "SELECT state, template_name, started_at FROM workflows WHERE document_id = ?",
                (doc_id,),
            ).fetchone()
            effective_template = template_name or (current["template_name"] if current else None)
            due_at = None
            if effective_template:
                state_def = self._get_workflow_state(conn, effective_template, state)
                if current:
                    self._validate_workflow_transition(
                        conn, effective_template, current["state"], state
                    )
                if state_def and state_def.get("sla_hours"):
                    due_at = time.time() + float(state_def["sla_hours"]) * 3600
                if not assigned_to and state_def and state_def.get("assignee_role"):
                    assigned_to = f"role:{state_def['assignee_role']}"
            conn.execute(
                """
                INSERT INTO workflows (document_id, state, assigned_to, template_name, due_at, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    state = excluded.state,
                    assigned_to = excluded.assigned_to,
                    template_name = excluded.template_name,
                    due_at = excluded.due_at,
                    started_at = COALESCE(workflows.started_at, excluded.started_at),
                    updated_at = excluded.updated_at
                """,
                (
                    doc_id,
                    state,
                    assigned_to,
                    effective_template,
                    due_at,
                    current["started_at"] if current else time.time(),
                    time.time(),
                ),
            )
            conn.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), doc_id),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "workflow_transition",
                {"state": state, "assigned_to": assigned_to, "template": effective_template},
            )
            self._emit_webhooks(
                "workflow.transition",
                {"doc_id": doc_id, "state": state, "template": effective_template},
            )

    def get_workflow(self, *, doc_id: str, actor: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            row = conn.execute(
                """
                SELECT state, assigned_to, template_name, due_at, started_at, updated_at
                FROM workflows WHERE document_id = ?
                """,
                (doc_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "state": row["state"],
                "assigned_to": row["assigned_to"],
                "template_name": row["template_name"],
                "due_at": row["due_at"],
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
            }

    def list_overdue_workflows(self, *, actor: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT workflows.document_id, workflows.state, workflows.assigned_to,
                       workflows.template_name, workflows.due_at, documents.title
                FROM workflows
                JOIN documents ON documents.id = workflows.document_id
                WHERE workflows.due_at IS NOT NULL AND workflows.due_at < ? AND documents.deleted_at IS NULL
                ORDER BY workflows.due_at ASC
                LIMIT ?
                """,
                (time.time(), limit),
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                if self._has_perm(conn, row["document_id"], actor, "read"):
                    results.append(
                        {
                            "doc_id": row["document_id"],
                            "title": row["title"],
                            "state": row["state"],
                            "assigned_to": row["assigned_to"],
                            "template_name": row["template_name"],
                            "due_at": row["due_at"],
                        }
                    )
            return results

    def search(self, *, query: str, actor: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT documents.id, documents.title, documents.doc_type, documents.status
                FROM doc_fts
                JOIN documents ON documents.id = doc_fts.document_id
                WHERE doc_fts MATCH ? AND documents.deleted_at IS NULL
                ORDER BY bm25(doc_fts, 0.0, 2.0, 1.0, 0.5)
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                if self._has_perm(conn, row["id"], actor, "read"):
                    results.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "doc_type": row["doc_type"],
                            "status": row["status"],
                        }
                    )
            return results

    def list_documents(
        self,
        *,
        actor: str,
        doc_type: Optional[str] = None,
        owner: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if doc_type:
            clauses.append("doc_type = ?")
            params.append(doc_type)
        if owner:
            clauses.append("owner = ?")
            params.append(owner)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, doc_type, owner, status, deleted_at, retention_until, legal_hold,
                       created_at, updated_at
                FROM documents
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                if self._has_perm(conn, row["id"], actor, "read"):
                    results.append(
                        {
                            "id": row["id"],
                            "title": row["title"],
                            "doc_type": row["doc_type"],
                            "owner": row["owner"],
                            "status": row["status"],
                            "deleted_at": row["deleted_at"],
                            "retention_until": row["retention_until"],
                            "legal_hold": bool(row["legal_hold"]),
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"],
                        }
                    )
            return results

    def register_doc_type(
        self,
        *,
        name: str,
        description: str,
        fields: Iterable[Dict[str, Any]],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO doc_types (name, description, created_at)
                VALUES (?, ?, ?)
                """,
                (name, description, time.time()),
            )
            conn.execute("DELETE FROM doc_type_fields WHERE doc_type = ?", (name,))
            for field in fields:
                allowed_values = field.get("allowed_values")
                allowed_json = json.dumps(allowed_values, ensure_ascii=True) if allowed_values else None
                conn.execute(
                    """
                    INSERT INTO doc_type_fields
                        (doc_type, field_name, field_type, required, allowed_values)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        field["name"],
                        field["type"],
                        1 if field.get("required") else 0,
                        allowed_json,
                    ),
                )
            self._audit(conn, None, "system", "register_doc_type", {"name": name})

    def register_workflow_template(
        self,
        *,
        name: str,
        description: str,
        states: Iterable[Dict[str, Any]],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO workflow_templates (name, description, created_at)
                VALUES (?, ?, ?)
                """,
                (name, description, time.time()),
            )
            conn.execute(
                "DELETE FROM workflow_template_states WHERE template_name = ?",
                (name,),
            )
            for state in states:
                conn.execute(
                    """
                    INSERT INTO workflow_template_states
                        (template_name, state_name, next_states, sla_hours, assignee_role)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        state["name"],
                        json.dumps(state.get("next_states", []), ensure_ascii=True),
                        state.get("sla_hours"),
                        state.get("assignee_role"),
                    ),
                )
            self._audit(conn, None, "system", "register_workflow_template", {"name": name})

    def list_workflow_templates(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, description FROM workflow_templates ORDER BY name",
            ).fetchall()
            return [{"name": row["name"], "description": row["description"]} for row in rows]

    def get_workflow_template(self, *, name: str) -> Dict[str, Any]:
        with self._connect() as conn:
            template = conn.execute(
                "SELECT name, description FROM workflow_templates WHERE name = ?",
                (name,),
            ).fetchone()
            if not template:
                raise NotFound(f"workflow template {name} not found")
            states = conn.execute(
                """
                SELECT state_name, next_states, sla_hours, assignee_role
                FROM workflow_template_states
                WHERE template_name = ?
                ORDER BY state_name
                """,
                (name,),
            ).fetchall()
            return {
                "name": template["name"],
                "description": template["description"],
                "states": [
                    {
                        "name": row["state_name"],
                        "next_states": json.loads(row["next_states"]),
                        "sla_hours": row["sla_hours"],
                        "assignee_role": row["assignee_role"],
                    }
                    for row in states
                ],
            }

    def add_doc_type_acl(self, *, doc_type: str, principal: str, permission: str) -> None:
        if permission not in PERMISSIONS:
            raise ValueError(f"unknown permission {permission}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO doc_type_acls (doc_type, principal, permission)
                VALUES (?, ?, ?)
                """,
                (doc_type, principal, permission),
            )
            self._audit(
                conn,
                None,
                "system",
                "add_doc_type_acl",
                {"doc_type": doc_type, "principal": principal, "permission": permission},
            )

    def list_doc_type_acls(self, *, doc_type: str) -> List[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT principal, permission FROM doc_type_acls
                WHERE doc_type = ?
                ORDER BY principal, permission
                """,
                (doc_type,),
            ).fetchall()
            return [{"principal": row["principal"], "permission": row["permission"]} for row in rows]

    def get_doc_type(self, *, name: str) -> Dict[str, Any]:
        with self._connect() as conn:
            doc_type = conn.execute(
                "SELECT * FROM doc_types WHERE name = ?",
                (name,),
            ).fetchone()
            if not doc_type:
                raise NotFound(f"doc_type {name} not found")
            fields = conn.execute(
                """
                SELECT field_name, field_type, required, allowed_values
                FROM doc_type_fields WHERE doc_type = ?
                ORDER BY field_name
                """,
                (name,),
            ).fetchall()
            return {
                "name": doc_type["name"],
                "description": doc_type["description"],
                "fields": [
                    {
                        "name": row["field_name"],
                        "type": row["field_type"],
                        "required": bool(row["required"]),
                        "allowed_values": json.loads(row["allowed_values"])
                        if row["allowed_values"]
                        else None,
                    }
                    for row in fields
                ],
            }

    def list_doc_types(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, description FROM doc_types ORDER BY name",
            ).fetchall()
            return [{"name": row["name"], "description": row["description"]} for row in rows]

    def get_schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(version) AS version FROM schema_versions").fetchone()
            return int(row["version"]) if row and row["version"] is not None else 1

    def sign_document(
        self,
        *,
        doc_id: str,
        actor: str,
        signer: str,
        key: str,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            row = conn.execute(
                """
                SELECT documents.current_version_id, versions.version_number, versions.checksum
                FROM documents
                JOIN versions ON versions.id = documents.current_version_id
                WHERE documents.id = ?
                """,
                (doc_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"document {doc_id} not found")
            version_number = int(row["version_number"])
            checksum = row["checksum"]
            signature = self._hmac_signature(key, f"{doc_id}:{version_number}:{checksum}:{note or ''}")
            sig_id = self._new_id("sig")
            conn.execute(
                """
                INSERT INTO signatures
                    (id, document_id, version_number, signer, checksum, signature, algorithm, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sig_id,
                    doc_id,
                    version_number,
                    signer,
                    checksum,
                    signature,
                    "HMAC-SHA256",
                    note,
                    time.time(),
                ),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "sign_document",
                {"signature_id": sig_id, "signer": signer, "version": version_number},
            )
            return {"id": sig_id, "version": version_number, "checksum": checksum, "signature": signature}

    def list_signatures(self, *, doc_id: str, actor: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            rows = conn.execute(
                """
                SELECT id, version_number, signer, checksum, signature, algorithm, note, created_at
                FROM signatures
                WHERE document_id = ?
                ORDER BY created_at DESC
                """,
                (doc_id,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "version": row["version_number"],
                    "signer": row["signer"],
                    "checksum": row["checksum"],
                    "signature": row["signature"],
                    "algorithm": row["algorithm"],
                    "note": row["note"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def verify_signature(self, *, doc_id: str, signature_id: str, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT version_number, checksum, signature, note
                FROM signatures
                WHERE document_id = ? AND id = ?
                """,
                (doc_id, signature_id),
            ).fetchone()
            if not row:
                raise NotFound(f"signature {signature_id} not found")
            expected = self._hmac_signature(
                key, f"{doc_id}:{row['version_number']}:{row['checksum']}:{row['note'] or ''}"
            )
            return expected == row["signature"]

    def add_webhook(
        self, *, name: str, url: str, events: List[str], secret: Optional[str] = None
    ) -> str:
        hook_id = self._new_id("wh")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO webhooks
                    (id, name, url, events, secret, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (hook_id, name, url, json.dumps(events, ensure_ascii=True), secret, time.time()),
            )
        return hook_id

    def list_webhooks(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, url, events, enabled, last_status, last_error,
                       last_attempt_at, last_success_at, created_at
                FROM webhooks
                ORDER BY created_at DESC
                """
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "url": row["url"],
                    "events": json.loads(row["events"]),
                    "enabled": bool(row["enabled"]),
                    "last_status": row["last_status"],
                    "last_error": row["last_error"],
                    "last_attempt_at": row["last_attempt_at"],
                    "last_success_at": row["last_success_at"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def delete_webhook(self, *, webhook_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))

    def create_group(self, *, name: str, description: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO groups (name, description, created_at)
                VALUES (?, ?, ?)
                """,
                (name, description, time.time()),
            )
            self._audit(conn, None, "system", "create_group", {"name": name})

    def add_group_member(self, *, group_name: str, member: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO group_members (group_name, member)
                VALUES (?, ?)
                """,
                (group_name, member),
            )
            self._audit(
                conn, None, "system", "add_group_member", {"group": group_name, "member": member}
            )

    def create_role(self, *, name: str, description: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO roles (name, description, created_at)
                VALUES (?, ?, ?)
                """,
                (name, description, time.time()),
            )
            self._audit(conn, None, "system", "create_role", {"name": name})

    def add_role_member(self, *, role_name: str, member: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO role_members (role_name, member)
                VALUES (?, ?)
                """,
                (role_name, member),
            )
            self._audit(
                conn, None, "system", "add_role_member", {"role": role_name, "member": member}
            )

    def add_role_permission(self, *, role_name: str, permission: str) -> None:
        if permission not in PERMISSIONS:
            raise ValueError(f"unknown permission {permission}")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO role_permissions (role_name, permission)
                VALUES (?, ?)
                """,
                (role_name, permission),
            )
            self._audit(
                conn,
                None,
                "system",
                "add_role_permission",
                {"role": role_name, "permission": permission},
            )

    def list_roles(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, description FROM roles ORDER BY name",
            ).fetchall()
            return [{"name": row["name"], "description": row["description"]} for row in rows]

    def get_role(self, *, name: str) -> Dict[str, Any]:
        with self._connect() as conn:
            role = conn.execute(
                "SELECT name, description FROM roles WHERE name = ?",
                (name,),
            ).fetchone()
            if not role:
                raise NotFound(f"role {name} not found")
            members = conn.execute(
                "SELECT member FROM role_members WHERE role_name = ? ORDER BY member",
                (name,),
            ).fetchall()
            permissions = conn.execute(
                "SELECT permission FROM role_permissions WHERE role_name = ? ORDER BY permission",
                (name,),
            ).fetchall()
            return {
                "name": role["name"],
                "description": role["description"],
                "members": [row["member"] for row in members],
                "permissions": [row["permission"] for row in permissions],
            }

    def list_groups(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, description FROM groups ORDER BY name",
            ).fetchall()
            return [{"name": row["name"], "description": row["description"]} for row in rows]

    def get_group(self, *, name: str) -> Dict[str, Any]:
        with self._connect() as conn:
            group = conn.execute(
                "SELECT name, description FROM groups WHERE name = ?",
                (name,),
            ).fetchone()
            if not group:
                raise NotFound(f"group {name} not found")
            members = conn.execute(
                "SELECT member FROM group_members WHERE group_name = ? ORDER BY member",
                (name,),
            ).fetchall()
            return {
                "name": group["name"],
                "description": group["description"],
                "members": [row["member"] for row in members],
            }

    def get_audit_log(self, *, doc_id: str, actor: str, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            rows = conn.execute(
                """
                SELECT actor, action, detail, created_at
                FROM audits WHERE document_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (doc_id, limit),
            ).fetchall()
            return [
                {
                    "actor": row["actor"],
                    "action": row["action"],
                    "detail": json.loads(row["detail"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def list_document_acls(self, *, doc_id: str, actor: str) -> List[Dict[str, str]]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "admin")
            rows = conn.execute(
                """
                SELECT principal, permission FROM acls
                WHERE document_id = ?
                ORDER BY principal, permission
                """,
                (doc_id,),
            ).fetchall()
            return [{"principal": row["principal"], "permission": row["permission"]} for row in rows]

    def import_document(
        self,
        *,
        actor: str,
        doc_id: Optional[str],
        doc_type: str,
        title: str,
        owner: str,
        status: str,
        versions: List[Dict[str, Any]],
        metadata: Dict[str, str],
        acls: Optional[List[Dict[str, str]]] = None,
        workflow: Optional[Dict[str, Any]] = None,
        deleted_at: Optional[float] = None,
        retention_until: Optional[float] = None,
        legal_hold: bool = False,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ) -> str:
        if not self._has_global_permission(actor, "admin"):
            raise AccessDenied(f"{actor} lacks admin permission")
        if not versions:
            raise ValueError("versions required")
        doc_id = doc_id or self._new_id("doc")
        created_at = created_at or time.time()
        updated_at = updated_at or created_at
        versions_sorted = sorted(versions, key=lambda v: int(v["version"]))
        current_version = versions_sorted[-1]
        current_version_id = self._new_id("ver")
        with self._connect() as conn:
            self._validate_metadata(conn, doc_type, metadata)
            conn.execute(
                """
                INSERT INTO documents
                    (id, doc_type, title, owner, status, current_version_id,
                     deleted_at, retention_until, legal_hold, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    doc_type,
                    title,
                    owner,
                    status,
                    current_version_id,
                    deleted_at,
                    retention_until,
                    1 if legal_hold else 0,
                    created_at,
                    updated_at,
                ),
            )
            for version in versions_sorted:
                version_id = current_version_id if version == current_version else self._new_id("ver")
                content = version["content"]
                checksum = version.get("checksum") or self._checksum(content)
                stored_content, content_path, content_size = self._store_content(
                    content, doc_id, int(version["version"]), checksum
                )
                conn.execute(
                    """
                    INSERT INTO versions
                        (id, document_id, version_number, content, content_path, content_size,
                         checksum, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        doc_id,
                        int(version["version"]),
                        stored_content,
                        content_path,
                        content_size,
                        checksum,
                        version.get("created_at", time.time()),
                    ),
                )
            conn.execute(
                "INSERT INTO doc_fts (document_id, title, content, metadata) VALUES (?, ?, ?, ?)",
                (
                    doc_id,
                    title,
                    self._content_to_text(current_version["content"]),
                    self._metadata_to_text(metadata),
                ),
            )
            self._upsert_metadata(conn, doc_id, metadata)
            self._init_acl(conn, doc_id, owner, [(row["principal"], row["permission"]) for row in acls or []])
            if workflow:
                conn.execute(
                    """
                    INSERT INTO workflows
                        (document_id, state, assigned_to, template_name, due_at, started_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_id,
                        workflow.get("state", status),
                        workflow.get("assigned_to"),
                        workflow.get("template_name"),
                        workflow.get("due_at"),
                        workflow.get("started_at", time.time()),
                        workflow.get("updated_at", time.time()),
                    ),
                )
            self._audit(conn, doc_id, actor, "import_document", {"doc_id": doc_id})
        return doc_id
    def list_versions(self, *, doc_id: str, actor: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            doc = conn.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            if doc["deleted_at"] is not None:
                raise NotFound(f"document {doc_id} not found")
            rows = conn.execute(
                """
                SELECT version_number, content_size, checksum, created_at
                FROM versions
                WHERE document_id = ?
                ORDER BY version_number DESC
                """,
                (doc_id,),
            ).fetchall()
            return [
                {
                    "version": row["version_number"],
                    "content_size": row["content_size"],
                    "checksum": row["checksum"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def get_version(
        self, *, doc_id: str, version_number: int, actor: str, include_deleted: bool = False
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "read")
            doc = conn.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            if doc["deleted_at"] is not None and not include_deleted:
                raise NotFound(f"document {doc_id} not found")
            version = conn.execute(
                """
                SELECT content, content_path, content_size, checksum, created_at
                FROM versions
                WHERE document_id = ? AND version_number = ?
                """,
                (doc_id, version_number),
            ).fetchone()
            if not version:
                raise NotFound(f"version {version_number} not found")
            content = self._read_content(version["content"], version["content_path"])
            return {
                "version": version_number,
                "content": content,
                "content_size": version["content_size"],
                "checksum": version["checksum"],
                "created_at": version["created_at"],
            }

    def delete_document(self, *, doc_id: str, actor: str, force: bool = False) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "delete")
            doc = conn.execute(
                "SELECT deleted_at, retention_until, legal_hold FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            if doc["deleted_at"] is not None:
                return
            if not force:
                if doc["legal_hold"]:
                    raise ValueError("document is under legal hold")
                if doc["retention_until"] and doc["retention_until"] > time.time():
                    raise ValueError("document is under retention")
            now = time.time()
            conn.execute(
                "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, doc_id),
            )
            self._audit(conn, doc_id, actor, "delete", {"force": force})
            self._emit_webhooks("document.deleted", {"doc_id": doc_id, "force": force})

    def restore_document(self, *, doc_id: str, actor: str) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "delete")
            doc = conn.execute(
                "SELECT deleted_at FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
            if not doc:
                raise NotFound(f"document {doc_id} not found")
            if doc["deleted_at"] is None:
                return
            now = time.time()
            conn.execute(
                "UPDATE documents SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                (now, doc_id),
            )
            self._audit(conn, doc_id, actor, "restore", {})
            self._emit_webhooks("document.restored", {"doc_id": doc_id})

    def set_retention(self, *, doc_id: str, actor: str, retention_until: Optional[float]) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "admin")
            now = time.time()
            conn.execute(
                "UPDATE documents SET retention_until = ?, updated_at = ? WHERE id = ?",
                (retention_until, now, doc_id),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "set_retention",
                {"retention_until": retention_until},
            )
            self._emit_webhooks(
                "document.retention",
                {"doc_id": doc_id, "retention_until": retention_until},
            )

    def set_legal_hold(self, *, doc_id: str, actor: str, enabled: bool) -> None:
        with self._connect() as conn:
            self._check_perm(conn, doc_id, actor, "admin")
            now = time.time()
            conn.execute(
                "UPDATE documents SET legal_hold = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now, doc_id),
            )
            self._audit(
                conn,
                doc_id,
                actor,
                "set_legal_hold",
                {"enabled": enabled},
            )
            self._emit_webhooks(
                "document.legal_hold",
                {"doc_id": doc_id, "enabled": enabled},
            )

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{int(time.time() * 1000)}_{hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]}"

    def _checksum(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _content_to_text(self, content: bytes) -> str:
        try:
            return content.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _metadata_to_text(self, metadata: Dict[str, str]) -> str:
        parts = []
        for key, value in metadata.items():
            parts.append(f"{key}:{value}")
            parts.append(str(value))
        return " ".join(parts)

    def _hmac_signature(self, key: str, message: str) -> str:
        return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _emit_webhooks(self, event: str, payload: Dict[str, Any]) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, url, events, secret, enabled
                FROM webhooks
                WHERE enabled = 1
                """
            ).fetchall()
            for row in rows:
                events = json.loads(row["events"])
                if event not in events and "*" not in events:
                    continue
                body = json.dumps({"event": event, "payload": payload}, ensure_ascii=True).encode(
                    "utf-8"
                )
                headers = {"Content-Type": "application/json"}
                if row["secret"]:
                    headers["X-Webhook-Signature"] = self._hmac_signature(
                        row["secret"], body.decode("utf-8")
                    )
                req = request.Request(row["url"], data=body, headers=headers, method="POST")
                now = time.time()
                try:
                    with request.urlopen(req, timeout=3) as resp:
                        status = resp.status
                    conn.execute(
                        """
                        UPDATE webhooks SET last_status = ?, last_error = NULL,
                            last_attempt_at = ?, last_success_at = ?
                        WHERE id = ?
                        """,
                        (status, now, now, row["id"]),
                    )
                except Exception as exc:
                    conn.execute(
                        """
                        UPDATE webhooks SET last_status = NULL, last_error = ?,
                            last_attempt_at = ?
                        WHERE id = ?
                        """,
                        (str(exc), now, row["id"]),
                    )

    def _get_workflow_state(
        self, conn: sqlite3.Connection, template_name: str, state_name: str
    ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            """
            SELECT next_states, sla_hours, assignee_role
            FROM workflow_template_states
            WHERE template_name = ? AND state_name = ?
            """,
            (template_name, state_name),
        ).fetchone()
        if not row:
            return None
        return {
            "next_states": json.loads(row["next_states"]),
            "sla_hours": row["sla_hours"],
            "assignee_role": row["assignee_role"],
        }

    def _validate_workflow_transition(
        self,
        conn: sqlite3.Connection,
        template_name: str,
        current_state: str,
        next_state: str,
    ) -> None:
        current_def = self._get_workflow_state(conn, template_name, current_state)
        if not current_def:
            raise ValueError(f"workflow state {current_state} not in template {template_name}")
        allowed = current_def.get("next_states", [])
        if next_state not in allowed:
            raise ValueError(f"transition {current_state} -> {next_state} not allowed")

    def _store_content(
        self, content: bytes, doc_id: str, version_number: int, checksum: str
    ) -> Tuple[bytes, Optional[str], Optional[int]]:
        if not self.storage_dir:
            return content, None, len(content)
        filename = f"{doc_id}_v{version_number}_{checksum[:8]}.bin"
        path = os.path.join(self.storage_dir, filename)
        with open(path, "wb") as handle:
            handle.write(content)
        return b"", path, len(content)

    def _read_content(self, content: bytes, content_path: Optional[str]) -> bytes:
        if content_path:
            with open(content_path, "rb") as handle:
                return handle.read()
        return content

    def _audit(
        self,
        conn: sqlite3.Connection,
        doc_id: Optional[str],
        actor: str,
        action: str,
        detail: Dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO audits (id, document_id, actor, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._new_id("aud"),
                doc_id,
                actor,
                action,
                json.dumps(detail, ensure_ascii=True),
                time.time(),
            ),
        )

    def _init_acl(
        self,
        conn: sqlite3.Connection,
        doc_id: str,
        owner: str,
        acl: Optional[Iterable[Tuple[str, str]]],
    ) -> None:
        conn.execute(
            "INSERT INTO acls (document_id, principal, permission) VALUES (?, ?, ?)",
            (doc_id, owner, "admin"),
        )
        if acl:
            for principal, permission in acl:
                if permission not in PERMISSIONS:
                    raise ValueError(f"unknown permission {permission}")
                conn.execute(
                    "INSERT OR IGNORE INTO acls (document_id, principal, permission) VALUES (?, ?, ?)",
                    (doc_id, principal, permission),
                )

    def _apply_doc_type_acl(self, conn: sqlite3.Connection, doc_id: str, doc_type: str) -> None:
        rows = conn.execute(
            "SELECT principal, permission FROM doc_type_acls WHERE doc_type = ?",
            (doc_type,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO acls (document_id, principal, permission) VALUES (?, ?, ?)",
                (doc_id, row["principal"], row["permission"]),
            )

    def _replace_metadata(
        self, conn: sqlite3.Connection, doc_id: str, metadata: Dict[str, str]
    ) -> None:
        conn.execute("DELETE FROM metadata WHERE document_id = ?", (doc_id,))
        self._upsert_metadata(conn, doc_id, metadata)

    def _upsert_metadata(
        self, conn: sqlite3.Connection, doc_id: str, metadata: Dict[str, str]
    ) -> None:
        if not metadata:
            return
        rows = [(doc_id, key, value) for key, value in metadata.items()]
        conn.executemany(
            "INSERT INTO metadata (document_id, key, value) VALUES (?, ?, ?)",
            rows,
        )

    def _get_metadata(self, conn: sqlite3.Connection, doc_id: str) -> Dict[str, str]:
        rows = conn.execute(
            "SELECT key, value FROM metadata WHERE document_id = ?",
            (doc_id,),
        ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def _validate_metadata_for_doc(
        self, conn: sqlite3.Connection, doc_id: str, metadata: Dict[str, str]
    ) -> None:
        doc = conn.execute(
            "SELECT doc_type FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if not doc:
            raise NotFound(f"document {doc_id} not found")
        self._validate_metadata(conn, doc["doc_type"], metadata)

    def _validate_metadata(
        self, conn: sqlite3.Connection, doc_type: str, metadata: Dict[str, str]
    ) -> None:
        fields = conn.execute(
            """
            SELECT field_name, field_type, required, allowed_values
            FROM doc_type_fields WHERE doc_type = ?
            """,
            (doc_type,),
        ).fetchall()
        if not fields:
            return
        rules = {
            row["field_name"]: {
                "type": row["field_type"],
                "required": bool(row["required"]),
                "allowed_values": json.loads(row["allowed_values"])
                if row["allowed_values"]
                else None,
            }
            for row in fields
        }
        for field_name, rule in rules.items():
            if rule["required"] and field_name not in metadata:
                raise ValueError(f"missing required metadata field {field_name}")
        for key, value in metadata.items():
            rule = rules.get(key)
            if not rule:
                continue
            self._validate_field_type(key, value, rule["type"])
            allowed = rule["allowed_values"]
            if allowed is not None and value not in allowed:
                raise ValueError(f"metadata field {key} must be in {allowed}")

    def _validate_field_type(self, key: str, value: str, field_type: str) -> None:
        if field_type == "string":
            return
        if field_type == "int":
            int(value)
            return
        if field_type == "float":
            float(value)
            return
        if field_type == "bool":
            if str(value).lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"metadata field {key} must be boolean")
            return
        if field_type == "date":
            time.strptime(str(value), "%Y-%m-%d")
            return
        raise ValueError(f"unknown field type {field_type}")

    def _check_perm(self, conn: sqlite3.Connection, doc_id: str, actor: str, permission: str) -> None:
        if not self._has_perm(conn, doc_id, actor, permission):
            raise AccessDenied(f"{actor} lacks {permission} on {doc_id}")

    def _has_perm(self, conn: sqlite3.Connection, doc_id: str, actor: str, permission: str) -> bool:
        if actor == "system":
            return True
        roles = self._get_actor_roles(conn, actor)
        if self._has_role_permission(conn, roles, permission):
            return True
        group_rows = conn.execute(
            "SELECT group_name FROM group_members WHERE member = ?",
            (actor,),
        ).fetchall()
        principals = [actor] + [f"group:{row['group_name']}" for row in group_rows]
        principals.extend([f"role:{role}" for role in roles])
        placeholders = ",".join("?" for _ in principals)
        row = conn.execute(
            f"""
            SELECT 1 FROM acls
            WHERE document_id = ? AND principal IN ({placeholders}) AND permission IN (?, 'admin')
            """,
            (doc_id, *principals, permission),
        ).fetchone()
        return row is not None

    def _has_global_permission(self, actor: str, permission: str) -> bool:
        if actor == "system":
            return True
        with self._connect() as conn:
            roles = self._get_actor_roles(conn, actor)
            return self._has_role_permission(conn, roles, permission)

    def _get_actor_roles(self, conn: sqlite3.Connection, actor: str) -> List[str]:
        rows = conn.execute(
            "SELECT role_name FROM role_members WHERE member = ?",
            (actor,),
        ).fetchall()
        return [row["role_name"] for row in rows]

    def _has_role_permission(
        self, conn: sqlite3.Connection, roles: List[str], permission: str
    ) -> bool:
        if not roles:
            return False
        placeholders = ",".join("?" for _ in roles)
        row = conn.execute(
            f"""
            SELECT 1 FROM role_permissions
            WHERE role_name IN ({placeholders}) AND permission IN (?, 'admin')
            """,
            (*roles, permission),
        ).fetchone()
        return row is not None
