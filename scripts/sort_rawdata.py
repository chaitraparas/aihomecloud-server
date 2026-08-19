#!/usr/bin/env python3
"""
One-shot script: sort files in admin's personal folder and OCR-index documents.
Uses the backend's file_sorter and document_index modules directly.

Usage:
    cd backend && python -m scripts.sort_rawdata
"""

import asyncio
import os
import sys

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Override settings for local run (skip mount check, use real NAS root)
os.environ.setdefault("AHC_SKIP_MOUNT_CHECK", "true")
os.environ.setdefault("AHC_NAS_ROOT", "/srv/nas")
os.environ.setdefault("AHC_DATA_DIR", "/var/lib/aihomecloud")


async def main() -> None:
    from app.document_index import init_db
    from app.file_sorter import sort_folder_now
    from app.config import settings
    from pathlib import Path

    admin_folder = settings.personal_path / "admin"
    if not admin_folder.is_dir():
        print(f"ERROR: admin folder not found at {admin_folder}")
        sys.exit(1)

    print(f"Admin folder: {admin_folder}")
    print(f"Data dir:     {settings.data_dir}")
    print()

    # Initialize the FTS5 database
    print("Initializing document index DB …")
    await init_db()
    print("DB ready.")
    print()

    # Sort files and OCR-index documents
    print("Sorting files in admin folder …")
    stats = await sort_folder_now(admin_folder, added_by="admin")
    print()
    print("=== Sort Results ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # Show what ended up where
    print()
    for sub in ("Photos", "Documents", "Videos", "Others"):
        p = admin_folder / sub
        if p.is_dir():
            files = [f.name for f in p.iterdir() if f.is_file()]
            if files:
                print(f"{sub}/ ({len(files)} files):")
                for f in sorted(files):
                    print(f"  {f}")

    # Show indexed documents
    from app.document_index import list_recent_documents
    docs = await list_recent_documents(limit=20)
    if docs:
        print()
        print(f"=== Indexed Documents ({len(docs)}) ===")
        for d in docs:
            print(f"  {d['filename']}  added_by={d['added_by']}  path={d['path']}")


if __name__ == "__main__":
    asyncio.run(main())
