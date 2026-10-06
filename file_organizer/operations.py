"""Reviewable plans and per-file recovery journals for reversible moves."""

import os
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from .filesystem import (RESERVED, absolute, digest_file, equal_files, exclusive_lock,
                         move_exclusive, safe_path, snapshot, unchanged)
from .models import FileSnapshot, MoveItem, Plan, ScanResult, timestamp
from .storage import Store

CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".heic"},
    "Documents": {".pdf", ".txt", ".md", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".odt"},
    "Audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "Video": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "Archives": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"},
}


def ready(result: ScanResult):
    if not result.complete:
        raise ValueError("Run a complete scan without read errors before planning changes")


def with_digest(file: FileSnapshot) -> FileSnapshot:
    current = digest_file(file)
    if file.digest and current != file.digest:
        raise ValueError(f"File content changed: {file.path}")
    return replace(file, digest=current)


def cleanup_plan(result: ScanResult, selected: set[str]) -> Plan:
    ready(result)
    selected_keys = {os.path.normcase(str(absolute(p))) for p in selected}
    known = {os.path.normcase(f.path): f.path for g in result.groups for f in g.files}
    if not selected_keys or not selected_keys <= known.keys():
        raise ValueError("Select at least one file from the duplicate groups")
    selected = {known[key] for key in selected_keys}
    operation_id = uuid4().hex
    root = Path(result.root)
    items = []
    for group in result.groups:
        removals = [f for f in group.files if f.path in selected]
        if not removals:
            continue
        keepers = [f for f in group.files if f.path not in selected]
        if not keepers:
            raise ValueError("Keep at least one copy in every duplicate group")
        keeper = with_digest(keepers[0])
        for file in removals:
            if file.nlink != 1:
                raise ValueError("Hard-linked files cannot be quarantined")
            source = with_digest(file)
            target = root / ".file-organizer-quarantine" / operation_id / Path(file.path).relative_to(root)
            items.append(MoveItem(source, str(target), keeper))
    return Plan(operation_id, "cleanup", str(root), tuple(items))


def organize_plan(result: ScanResult) -> Plan:
    ready(result)
    root = Path(result.root)
    items, notes, targets = [], [], set()
    for file in sorted(result.files, key=lambda f: f.path.casefold()):
        path = Path(file.path)
        category = next((c for c, suffixes in CATEGORIES.items() if path.suffix.lower() in suffixes), "Other")
        target = root / category / path.name
        if target == path:
            continue
        if file.nlink != 1:
            notes.append(f"Hard-linked file retained: {path}")
            continue
        if os.path.lexists(target) or str(target).casefold() in targets:
            notes.append(f"Destination conflict; retained: {path} -> {target}")
            continue
        try:
            safe_path(target, root)
            source = with_digest(file)
        except (OSError, ValueError) as error:
            notes.append(f"Skipped {path}: {error}")
            continue
        targets.add(str(target).casefold())
        items.append(MoveItem(source, str(target)))
    return Plan(uuid4().hex, "organize", str(root), tuple(items), tuple(notes))


def _validate_plan(plan: Plan):
    if plan.kind not in {"cleanup", "organize"} or not plan.items:
        raise ValueError("Plan must contain at least one cleanup or organization move")
    root = safe_path(Path(plan.root))
    sources = {absolute(i.source.path) for i in plan.items}
    targets = {absolute(i.target) for i in plan.items}
    if len(sources) != len(plan.items) or len(targets) != len(plan.items) or sources & targets:
        raise ValueError("Plan contains repeated or overlapping paths")
    for item in plan.items:
        source = safe_path(Path(item.source.path), root)
        target = safe_path(Path(item.target), root)
        if any(part.casefold() in RESERVED for part in source.relative_to(root).parts):
            raise ValueError("Cannot move application-managed files")
        if item.source.nlink != 1 or not item.source.digest:
            raise ValueError("Only verified single-link files can be moved")
        if plan.kind == "cleanup":
            expected = root / ".file-organizer-quarantine" / plan.id / source.relative_to(root)
            if target != expected or item.keeper is None:
                raise ValueError("Invalid quarantine destination or missing keeper")
            safe_path(Path(item.keeper.path), root)
            if absolute(item.keeper.path) in sources:
                raise ValueError("A retained copy cannot be moved in the same plan")
        elif target.parent.parent != root or target.parent.name not in {*CATEGORIES, "Other"}:
            raise ValueError("Invalid organization destination")


def apply_plan(plan: Plan, store: Store) -> dict:
    _validate_plan(plan)
    with store.exclusive(), exclusive_lock(Path(plan.root) / ".file-organizer.lock"):
        return _apply_plan(plan, store)


def _apply_plan(plan: Plan, store: Store) -> dict:
    _validate_plan(plan)
    record = {"id": plan.id, "created": timestamp(), "kind": plan.kind,
              "root": plan.root, "notes": list(plan.notes),
              "items": [{**asdict(i), "status": "pending", "error": ""} for i in plan.items]}
    store.create(record)
    for item in record["items"]:
        source = FileSnapshot(**item["source"])
        try:
            verified = with_digest(source)
            if item["keeper"]:
                keeper = with_digest(FileSnapshot(**item["keeper"]))
                if verified.digest != keeper.digest or not equal_files(verified, keeper):
                    raise ValueError("Retained copy no longer matches")
            unchanged(source)
        except (OSError, ValueError) as error:
            item.update(status="failed", error=str(error))
            store.update(record)
            continue
        item["status"] = "moving"
        store.update(record)  # Commit intent BEFORE touching the filesystem.
        try:
            move_exclusive(Path(source.path), Path(item["target"]), Path(plan.root))
        except (OSError, ValueError) as error:
            item.update(status="failed", error=str(error))
        else:
            item.update(status="done", error="")
        store.update(record)  # If this fails, recovery inspects the moving entry.
    return record


def _matches(path: Path, data: dict) -> bool:
    original = FileSnapshot(**data)
    unchanged(original, path)
    return digest_file(replace(original, path=str(path))) == original.digest


def recover(store: Store) -> None:
    with store.exclusive():
        _recover(store)


def _recover(store: Store) -> None:
    for record in store.operations():
        if not any(i["status"] in {"pending", "moving", "restoring"} for i in record["items"]):
            continue
        if not Path(record["root"]).is_dir():
            for item in record["items"]:
                if item["status"] in {"pending", "moving", "restoring"}:
                    item.update(status="conflict", error="Original root folder is unavailable; inspect manually")
            store.update(record)
            continue
        with exclusive_lock(Path(record["root"]) / ".file-organizer.lock"):
            _recover_record(store, record)


def _recover_record(store, record):
    for item in record["items"]:
        state = item["status"]
        if state not in {"pending", "moving", "restoring"}:
            continue
        if state == "pending":
            item.update(status="failed", error="Interrupted before this move started; rescan to retry")
            store.update(record)
            continue
        try:
            source = safe_path(Path(item["source"]["path"]), Path(record["root"]))
            target = safe_path(Path(item["target"]), Path(record["root"]))
            exists_source, exists_target = os.path.lexists(source), os.path.lexists(target)
            if exists_source and not exists_target and _matches(source, item["source"]):
                item.update(status="restored" if state == "restoring" else "failed",
                            error="" if state == "restoring" else "Interrupted before move; source retained")
            elif exists_target and not exists_source and _matches(target, item["source"]):
                item.update(status="done", error="" if state == "moving" else "Restore interrupted; retry restore")
            else:
                raise ValueError("Ambiguous interrupted operation; inspect both paths manually")
        except (OSError, ValueError) as error:
            item.update(status="conflict", error=str(error))
        store.update(record)


def restore_operation(operation_id: str, store: Store) -> dict:
    with store.exclusive():
        _recover(store)
        record = store.get(operation_id)
        with exclusive_lock(Path(record["root"]) / ".file-organizer.lock"):
            return _restore_operation(operation_id, store)


def _restore_operation(operation_id: str, store: Store) -> dict:
    record = store.get(operation_id)
    for item in reversed(record["items"]):
        if item["status"] != "done":
            continue
        source, target = Path(item["source"]["path"]), Path(item["target"])
        try:
            safe_path(source, Path(record["root"]))
            safe_path(target, Path(record["root"]))
            if not _matches(target, item["source"]):
                raise ValueError("Moved file contents changed; restore refused")
            if os.path.lexists(source):
                raise FileExistsError(f"Original path is occupied: {source}")
        except (OSError, ValueError) as error:
            item["error"] = str(error)
            store.update(record)
            continue
        item.update(status="restoring", error="")
        store.update(record)
        try:
            move_exclusive(target, source, Path(record["root"]))
        except (OSError, ValueError) as error:
            item.update(status="done", error=str(error))
        else:
            item.update(status="restored", error="")
        store.update(record)
    return record
