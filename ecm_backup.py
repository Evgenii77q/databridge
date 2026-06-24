import argparse
import os
import shutil
import tempfile
import zipfile
from typing import Optional


def backup(db_path: str, storage_dir: Optional[str], output_path: str) -> None:
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"db file not found: {db_path}")
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(db_path, arcname=os.path.join("db", os.path.basename(db_path)))
        if storage_dir and os.path.isdir(storage_dir):
            for root, _, files in os.walk(storage_dir):
                for name in files:
                    full_path = os.path.join(root, name)
                    rel_path = os.path.relpath(full_path, storage_dir)
                    archive.write(full_path, arcname=os.path.join("blobs", rel_path))


def restore(backup_path: str, db_path: str, storage_dir: Optional[str], force: bool) -> None:
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"backup file not found: {backup_path}")
    if os.path.exists(db_path) and not force:
        raise FileExistsError(f"db path exists: {db_path}")
    if storage_dir and os.path.isdir(storage_dir) and os.listdir(storage_dir) and not force:
        raise FileExistsError(f"storage dir not empty: {storage_dir}")

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(backup_path, "r") as archive:
            archive.extractall(tmpdir)
        db_dir = os.path.join(tmpdir, "db")
        db_files = os.listdir(db_dir) if os.path.isdir(db_dir) else []
        if not db_files:
            raise FileNotFoundError("backup does not contain db")
        db_src = os.path.join(db_dir, db_files[0])
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        shutil.copy2(db_src, db_path)

        blobs_dir = os.path.join(tmpdir, "blobs")
        if storage_dir and os.path.isdir(blobs_dir):
            os.makedirs(storage_dir, exist_ok=True)
            for root, _, files in os.walk(blobs_dir):
                for name in files:
                    full_path = os.path.join(root, name)
                    rel_path = os.path.relpath(full_path, blobs_dir)
                    dest_path = os.path.join(storage_dir, rel_path)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    shutil.copy2(full_path, dest_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup/restore ECM core db and blobs.")
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--db", required=True)
    backup_parser.add_argument("--storage-dir")
    backup_parser.add_argument("--out", required=True)

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--db", required=True)
    restore_parser.add_argument("--storage-dir")
    restore_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if args.command == "backup":
        backup(args.db, args.storage_dir, args.out)
    elif args.command == "restore":
        restore(args.backup, args.db, args.storage_dir, args.force)


if __name__ == "__main__":
    main()
