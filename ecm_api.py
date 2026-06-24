import base64
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ecm_core import AccessDenied, ECMCore, NotFound


DB_PATH = os.getenv("ECM_DB_PATH", "ecm_core.db")
STORAGE_DIR = os.getenv("ECM_STORAGE_DIR")
UI_INDEX = os.path.join(os.path.dirname(__file__), "web_ui", "index.html")
core = ECMCore(DB_PATH, storage_dir=STORAGE_DIR)
app = FastAPI(title="ECM Core API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def require_actor(x_actor: Optional[str] = Header(default=None)) -> str:
    if not x_actor:
        raise HTTPException(status_code=400, detail="X-Actor header required")
    return x_actor


class FieldSpec(BaseModel):
    name: str
    type: str = Field(pattern="^(string|int|float|bool|date)$")
    required: bool = False
    allowed_values: Optional[List[str]] = None


class DocTypeRequest(BaseModel):
    name: str
    description: str
    fields: List[FieldSpec]


class CreateDocRequest(BaseModel):
    owner: str
    title: str
    doc_type: str
    content_base64: str
    metadata: Optional[Dict[str, str]] = None
    acl: Optional[List[List[str]]] = None


class UpdateContentRequest(BaseModel):
    content_base64: str
    metadata: Optional[Dict[str, str]] = None


class MetadataRequest(BaseModel):
    metadata: Dict[str, str]


class ACLRequest(BaseModel):
    principal: str
    permission: str


class WorkflowRequest(BaseModel):
    state: str
    assigned_to: Optional[str] = None
    template_name: Optional[str] = None


class SignatureRequest(BaseModel):
    signer: str
    key: str
    note: Optional[str] = None


class SignatureVerifyRequest(BaseModel):
    key: str


class WebhookRequest(BaseModel):
    name: str
    url: str
    events: List[str]
    secret: Optional[str] = None


class WorkflowTemplateState(BaseModel):
    name: str
    next_states: List[str] = Field(default_factory=list)
    sla_hours: Optional[float] = None
    assignee_role: Optional[str] = None


class WorkflowTemplateRequest(BaseModel):
    name: str
    description: str
    states: List[WorkflowTemplateState]


def decode_content(content_base64: str) -> bytes:
    try:
        return base64.b64decode(content_base64.encode("ascii"), validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 content") from exc


def encode_content(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def parse_date_ymd(value: str) -> float:
    parsed = time.strptime(value, "%Y-%m-%d")
    return time.mktime(parsed)


@app.get("/", include_in_schema=False)
@app.get("/ui", include_in_schema=False)
def ui_index() -> FileResponse:
    if not os.path.exists(UI_INDEX):
        raise HTTPException(status_code=404, detail="UI not found")
    return FileResponse(UI_INDEX)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/schema/version")
def schema_version() -> Dict[str, Any]:
    return {"version": core.get_schema_version()}


@app.post("/doc-types")
def register_doc_type(payload: DocTypeRequest) -> Dict[str, Any]:
    core.register_doc_type(
        name=payload.name,
        description=payload.description,
        fields=[field.dict() for field in payload.fields],
    )
    return {"status": "ok"}


@app.get("/doc-types")
def list_doc_types() -> List[Dict[str, Any]]:
    return core.list_doc_types()


@app.get("/doc-types/{name}")
def get_doc_type(name: str) -> Dict[str, Any]:
    try:
        return core.get_doc_type(name=name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/doc-types/{name}/acl")
def add_doc_type_acl(name: str, payload: ACLRequest) -> Dict[str, Any]:
    try:
        core.add_doc_type_acl(doc_type=name, principal=payload.principal, permission=payload.permission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/doc-types/{name}/acl")
def list_doc_type_acl(name: str) -> List[Dict[str, str]]:
    return core.list_doc_type_acls(doc_type=name)


@app.post("/documents")
def create_document(payload: CreateDocRequest) -> Dict[str, Any]:
    acl_pairs: Optional[Iterable[List[str]]] = payload.acl
    acl = None
    if acl_pairs:
        acl = [(pair[0], pair[1]) for pair in acl_pairs]
    doc_id = core.create_document(
        owner=payload.owner,
        title=payload.title,
        doc_type=payload.doc_type,
        content=decode_content(payload.content_base64),
        metadata=payload.metadata,
        acl=acl,
    )
    return {"id": doc_id}


@app.get("/documents/{doc_id}")
def get_document(
    doc_id: str, actor: str = Depends(require_actor), include_deleted: bool = False
) -> Dict[str, Any]:
    try:
        doc = core.get_document(doc_id=doc_id, actor=actor, include_deleted=include_deleted)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    doc["content_base64"] = encode_content(doc.pop("content"))
    return doc


@app.post("/documents/{doc_id}/content")
def update_content(
    doc_id: str, payload: UpdateContentRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        version = core.update_content(
            doc_id=doc_id,
            actor=actor,
            content=decode_content(payload.content_base64),
            metadata=payload.metadata,
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"version": version}


@app.get("/documents")
def list_documents(
    actor: str = Depends(require_actor),
    doc_type: Optional[str] = None,
    owner: Optional[str] = None,
    status: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    return core.list_documents(
        actor=actor,
        doc_type=doc_type,
        owner=owner,
        status=status,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )


@app.put("/documents/{doc_id}/metadata")
def set_metadata(
    doc_id: str, payload: MetadataRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.set_metadata(doc_id=doc_id, actor=actor, metadata=payload.metadata)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/documents/{doc_id}/acl")
def add_acl(
    doc_id: str, payload: ACLRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.add_acl(
            doc_id=doc_id, actor=actor, principal=payload.principal, permission=payload.permission
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/documents/{doc_id}/acl")
def list_acl(doc_id: str, actor: str = Depends(require_actor)) -> List[Dict[str, Any]]:
    try:
        return core.list_document_acls(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/documents/{doc_id}/acl")
def remove_acl(
    doc_id: str, payload: ACLRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.remove_acl(
            doc_id=doc_id, actor=actor, principal=payload.principal, permission=payload.permission
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/documents/{doc_id}/workflow")
def transition_workflow(
    doc_id: str, payload: WorkflowRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.transition_workflow(
            doc_id=doc_id,
            actor=actor,
            state=payload.state,
            assigned_to=payload.assigned_to,
            template_name=payload.template_name,
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/documents/{doc_id}/workflow")
def get_workflow(doc_id: str, actor: str = Depends(require_actor)) -> Dict[str, Any]:
    try:
        workflow = core.get_workflow(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if workflow is None:
        return {"status": "none"}
    return workflow


class DeleteRequest(BaseModel):
    force: bool = False


class RetentionRequest(BaseModel):
    retention_until_date: Optional[str] = None
    retention_until_epoch: Optional[float] = None


class LegalHoldRequest(BaseModel):
    enabled: bool


@app.post("/documents/{doc_id}/delete")
def delete_document(
    doc_id: str, payload: DeleteRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.delete_document(doc_id=doc_id, actor=actor, force=payload.force)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/documents/{doc_id}/restore")
def restore_document(doc_id: str, actor: str = Depends(require_actor)) -> Dict[str, Any]:
    try:
        core.restore_document(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/documents/{doc_id}/retention")
def set_retention(
    doc_id: str, payload: RetentionRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    if payload.retention_until_date and payload.retention_until_epoch is not None:
        raise HTTPException(status_code=400, detail="Provide only one retention value")
    retention_until = None
    if payload.retention_until_date:
        retention_until = parse_date_ymd(payload.retention_until_date)
    elif payload.retention_until_epoch is not None:
        retention_until = payload.retention_until_epoch
    try:
        core.set_retention(doc_id=doc_id, actor=actor, retention_until=retention_until)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/documents/{doc_id}/legal-hold")
def set_legal_hold(
    doc_id: str, payload: LegalHoldRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        core.set_legal_hold(doc_id=doc_id, actor=actor, enabled=payload.enabled)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/documents/{doc_id}/audit")
def get_audit_log(doc_id: str, actor: str = Depends(require_actor)) -> List[Dict[str, Any]]:
    try:
        return core.get_audit_log(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/documents/{doc_id}/audit/export", response_class=PlainTextResponse)
def export_audit_log(doc_id: str, actor: str = Depends(require_actor)) -> str:
    try:
        rows = core.get_audit_log(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    lines = ["actor,action,detail,created_at"]
    for row in rows:
        detail = json.dumps(row["detail"], ensure_ascii=True).replace('"', '""')
        lines.append(f"{row['actor']},{row['action']},\"{detail}\",{row['created_at']}")
    return "\n".join(lines)


@app.get("/documents/{doc_id}/versions")
def list_versions(doc_id: str, actor: str = Depends(require_actor)) -> List[Dict[str, Any]]:
    try:
        return core.list_versions(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/documents/{doc_id}/versions/{version_number}")
def get_version(
    doc_id: str, version_number: int, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        version = core.get_version(doc_id=doc_id, version_number=version_number, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    version["content_base64"] = encode_content(version.pop("content"))
    return version


@app.post("/documents/{doc_id}/sign")
def sign_document(
    doc_id: str, payload: SignatureRequest, actor: str = Depends(require_actor)
) -> Dict[str, Any]:
    try:
        return core.sign_document(
            doc_id=doc_id,
            actor=actor,
            signer=payload.signer,
            key=payload.key,
            note=payload.note,
        )
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/documents/{doc_id}/signatures")
def list_signatures(doc_id: str, actor: str = Depends(require_actor)) -> List[Dict[str, Any]]:
    try:
        return core.list_signatures(doc_id=doc_id, actor=actor)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/documents/{doc_id}/signatures/{signature_id}/verify")
def verify_signature(
    doc_id: str,
    signature_id: str,
    payload: SignatureVerifyRequest,
    actor: str = Depends(require_actor),
) -> Dict[str, Any]:
    try:
        ok = core.verify_signature(doc_id=doc_id, signature_id=signature_id, key=payload.key)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"valid": ok}


@app.post("/webhooks")
def add_webhook(payload: WebhookRequest) -> Dict[str, Any]:
    hook_id = core.add_webhook(
        name=payload.name, url=payload.url, events=payload.events, secret=payload.secret
    )
    return {"id": hook_id}


@app.get("/webhooks")
def list_webhooks() -> List[Dict[str, Any]]:
    return core.list_webhooks()


@app.delete("/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str) -> Dict[str, Any]:
    core.delete_webhook(webhook_id=webhook_id)
    return {"status": "ok"}


@app.get("/search")
def search(query: str, actor: str = Depends(require_actor), limit: int = 50) -> List[Dict[str, Any]]:
    try:
        return core.search(query=query, actor=actor, limit=limit)
    except AccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/workflows/overdue")
def list_overdue_workflows(
    actor: str = Depends(require_actor), limit: int = 200
) -> List[Dict[str, Any]]:
    return core.list_overdue_workflows(actor=actor, limit=limit)


@app.post("/workflow-templates")
def register_workflow_template(payload: WorkflowTemplateRequest) -> Dict[str, Any]:
    core.register_workflow_template(
        name=payload.name,
        description=payload.description,
        states=[state.dict() for state in payload.states],
    )
    return {"status": "ok"}


@app.get("/workflow-templates")
def list_workflow_templates() -> List[Dict[str, Any]]:
    return core.list_workflow_templates()


@app.get("/workflow-templates/{name}")
def get_workflow_template(name: str) -> Dict[str, Any]:
    try:
        return core.get_workflow_template(name=name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class GroupRequest(BaseModel):
    name: str
    description: str


class GroupMemberRequest(BaseModel):
    member: str


@app.post("/groups")
def create_group(payload: GroupRequest) -> Dict[str, Any]:
    core.create_group(name=payload.name, description=payload.description)
    return {"status": "ok"}


@app.get("/groups")
def list_groups() -> List[Dict[str, Any]]:
    return core.list_groups()


@app.get("/groups/{name}")
def get_group(name: str) -> Dict[str, Any]:
    try:
        return core.get_group(name=name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/groups/{name}/members")
def add_group_member(name: str, payload: GroupMemberRequest) -> Dict[str, Any]:
    core.add_group_member(group_name=name, member=payload.member)
    return {"status": "ok"}


class RoleRequest(BaseModel):
    name: str
    description: str


class RoleMemberRequest(BaseModel):
    member: str


class RolePermissionRequest(BaseModel):
    permission: str


@app.post("/roles")
def create_role(payload: RoleRequest) -> Dict[str, Any]:
    core.create_role(name=payload.name, description=payload.description)
    return {"status": "ok"}


@app.get("/roles")
def list_roles() -> List[Dict[str, Any]]:
    return core.list_roles()


@app.get("/roles/{name}")
def get_role(name: str) -> Dict[str, Any]:
    try:
        return core.get_role(name=name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/roles/{name}/members")
def add_role_member(name: str, payload: RoleMemberRequest) -> Dict[str, Any]:
    core.add_role_member(role_name=name, member=payload.member)
    return {"status": "ok"}


@app.post("/roles/{name}/permissions")
def add_role_permission(name: str, payload: RolePermissionRequest) -> Dict[str, Any]:
    try:
        core.add_role_permission(role_name=name, permission=payload.permission)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}
