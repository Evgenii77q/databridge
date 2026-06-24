import argparse
import base64
import json
import os
from typing import Any, Dict, Iterable, List, Optional

from ecm_core import ECMCore


def encode_content(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def decode_content(content_base64: str) -> bytes:
    return base64.b64decode(content_base64.encode("ascii"), validate=True)


def export_documents(
    core: ECMCore,
    output_path: str,
    actor: str,
    include_deleted: bool,
    include_versions: bool,
) -> None:
    docs = core.list_documents(actor=actor, include_deleted=include_deleted, limit=100000, offset=0)
    with open(output_path, "w", encoding="utf-8") as handle:
        for doc in docs:
            doc_id = doc["id"]
            document = core.get_document(doc_id=doc_id, actor=actor, include_deleted=True)
            acls = core.list_document_acls(doc_id=doc_id, actor=actor)
            workflow = core.get_workflow(doc_id=doc_id, actor=actor)
            versions: List[Dict[str, Any]] = []
            if include_versions:
                version_rows = core.list_versions(doc_id=doc_id, actor=actor)
                for version_row in version_rows:
                    version = core.get_version(
                        doc_id=doc_id, version_number=version_row["version"], actor=actor
                    )
                    versions.append(
                        {
                            "version": version["version"],
                            "content_base64": encode_content(version["content"]),
                            "checksum": version["checksum"],
                            "created_at": version["created_at"],
                        }
                    )
            else:
                versions.append(
                    {
                        "version": document["version"],
                        "content_base64": encode_content(document["content"]),
                        "checksum": document["checksum"],
                        "created_at": document["updated_at"],
                    }
                )
            record = {
                "document": {
                    "id": document["id"],
                    "doc_type": document["doc_type"],
                    "title": document["title"],
                    "owner": document["owner"],
                    "status": document["status"],
                    "deleted_at": document["deleted_at"],
                    "retention_until": document["retention_until"],
                    "legal_hold": document["legal_hold"],
                    "created_at": document["created_at"],
                    "updated_at": document["updated_at"],
                    "metadata": document["metadata"],
                },
                "acls": acls,
                "workflow": workflow,
                "versions": versions,
            }
            handle.write(json.dumps(record, ensure_ascii=True))
            handle.write("\n")


def import_documents(core: ECMCore, input_path: str, actor: str) -> None:
    with open(input_path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            doc = record["document"]
            versions_raw: Iterable[Dict[str, Any]] = record["versions"]
            versions: List[Dict[str, Any]] = []
            for version in versions_raw:
                versions.append(
                    {
                        "version": int(version["version"]),
                        "content": decode_content(version["content_base64"]),
                        "checksum": version.get("checksum"),
                        "created_at": version.get("created_at"),
                    }
                )
            core.import_document(
                actor=actor,
                doc_id=doc.get("id"),
                doc_type=doc["doc_type"],
                title=doc["title"],
                owner=doc["owner"],
                status=doc.get("status", "draft"),
                versions=versions,
                metadata=doc.get("metadata", {}),
                acls=record.get("acls"),
                workflow=record.get("workflow"),
                deleted_at=doc.get("deleted_at"),
                retention_until=doc.get("retention_until"),
                legal_hold=bool(doc.get("legal_hold", False)),
                created_at=doc.get("created_at"),
                updated_at=doc.get("updated_at"),
            )


def import_folder(
    core: ECMCore,
    folder: str,
    actor: str,
    doc_type: str,
    owner: str,
    extensions: Optional[List[str]],
    recursive: bool,
) -> None:
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"folder not found: {folder}")
    for root, _, files in os.walk(folder):
        for name in files:
            if extensions:
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext not in extensions:
                    continue
            path = os.path.join(root, name)
            with open(path, "rb") as handle:
                content = handle.read()
            rel_path = os.path.relpath(path, folder)
            metadata = {
                "filename": name,
                "path": rel_path,
                "extension": os.path.splitext(name)[1].lstrip("."),
                "size": str(os.path.getsize(path)),
            }
            core.create_document(
                owner=owner,
                title=name,
                doc_type=doc_type,
                content=content,
                metadata=metadata,
                acl=None,
            )
        if not recursive:
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk export/import documents.")
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export")
    export_parser.add_argument("--db", required=True)
    export_parser.add_argument("--storage-dir")
    export_parser.add_argument("--out", required=True)
    export_parser.add_argument("--actor", default="system")
    export_parser.add_argument("--include-deleted", action="store_true")
    export_parser.add_argument("--include-versions", action="store_true")

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--db", required=True)
    import_parser.add_argument("--storage-dir")
    import_parser.add_argument("--in", dest="input_path", required=True)
    import_parser.add_argument("--actor", default="system")

    folder_parser = sub.add_parser("import-folder")
    folder_parser.add_argument("--db", required=True)
    folder_parser.add_argument("--storage-dir")
    folder_parser.add_argument("--folder", required=True)
    folder_parser.add_argument("--actor", default="system")
    folder_parser.add_argument("--doc-type", required=True)
    folder_parser.add_argument("--owner", required=True)
    folder_parser.add_argument("--extensions")
    folder_parser.add_argument("--recursive", action="store_true")

    args = parser.parse_args()
    core = ECMCore(args.db, storage_dir=args.storage_dir)
    if args.command == "export":
        export_documents(
            core,
            args.out,
            actor=args.actor,
            include_deleted=args.include_deleted,
            include_versions=args.include_versions,
        )
    elif args.command == "import":
        import_documents(core, args.input_path, actor=args.actor)
    elif args.command == "import-folder":
        extensions = None
        if args.extensions:
            extensions = [ext.strip().lower().lstrip(".") for ext in args.extensions.split(",")]
        import_folder(
            core,
            args.folder,
            actor=args.actor,
            doc_type=args.doc_type,
            owner=args.owner,
            extensions=extensions,
            recursive=args.recursive,
        )


if __name__ == "__main__":
    main()
