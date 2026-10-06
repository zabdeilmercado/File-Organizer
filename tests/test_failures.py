import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from file_organizer import filesystem, operations, scanner
from file_organizer.filesystem import snapshot
from file_organizer.operations import apply_plan, cleanup_plan, organize_plan, recover, restore_operation
from file_organizer.scanner import scan
from file_organizer.storage import Store
from test_scan import put


def test_changed_during_read_is_incomplete(tmp_path, monkeypatch):
    a = put(tmp_path, "a")
    put(tmp_path, "b")
    original = scanner.digest_file
    def change(file, cancel, progress):
        if file.path == str(a):
            a.write_bytes(b"changed")
        return original(file, cancel, progress)
    monkeypatch.setattr(scanner, "digest_file", change)
    result = scan(tmp_path)
    assert not result.complete and not result.groups


def test_hash_collision_still_checks_bytes(tmp_path, monkeypatch):
    put(tmp_path, "a", b"hello")
    put(tmp_path, "b", b"world")
    monkeypatch.setattr(scanner, "digest_file", lambda *args: "same hash")
    assert not scan(tmp_path).groups


def test_cancellation_during_hashing(tmp_path):
    put(tmp_path, "a", b"x" * (filesystem.CHUNK + 10))
    put(tmp_path, "b", b"x" * (filesystem.CHUNK + 10))
    cancel = Event()
    def progress(event):
        if event["bytes_hashed"]:
            cancel.set()
    result = scan(tmp_path, cancel, progress)
    assert result.cancelled and not result.complete


def test_reparse_directory_skipped(tmp_path, monkeypatch):
    put(tmp_path, "linked/file")
    original = scanner.linked
    monkeypatch.setattr(scanner, "linked", lambda path: path.name == "linked" or original(path))
    result = scan(tmp_path)
    assert not result.files and result.skipped


def test_actual_directory_junction_is_skipped(tmp_path):
    import subprocess
    target = tmp_path / "outside"
    put(target, "private")
    root = tmp_path / "files"
    root.mkdir()
    junction = root / "junction"
    if os.name != "nt":
        pytest.skip("Windows junction test")
    # Explicit paths in a temporary test fixture; this command creates only a link.
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True)
    if completed.returncode:
        pytest.skip("Junction creation unavailable")
    try:
        result = scan(root)
        assert not result.files and result.skipped
        with pytest.raises(ValueError, match="reparse"):
            scan(junction)
    finally:
        junction.rmdir()
    assert (target / "private").exists()


def setup_plan(tmp_path):
    root = tmp_path / "files"
    put(root, "a")
    b = put(root, "b")
    store = Store(tmp_path / "db.sqlite3")
    plan = cleanup_plan(scan(root), {str(b)})
    return root, b, store, plan


def test_mid_move_failure_allows_other_files_to_finish(tmp_path, monkeypatch):
    root, b, store, _ = setup_plan(tmp_path)
    c = put(root, "c")
    plan = cleanup_plan(scan(root), {str(b), str(c)})
    original = operations.move_exclusive
    def move(source, target, root):
        if source == b:
            raise PermissionError("file is open")
        original(source, target, root)
    monkeypatch.setattr(operations, "move_exclusive", move)
    record = apply_plan(plan, store)
    assert [i["status"] for i in record["items"]] == ["failed", "done"]
    assert b.exists() and not c.exists()


@pytest.mark.parametrize("phase", ["before_move", "after_restore", "both_paths", "missing_paths", "pending"])
def test_recovery_failure_points(tmp_path, monkeypatch, phase):
    root, b, store, plan = setup_plan(tmp_path)
    if phase == "after_restore":
        apply_plan(plan, store)
        original = store.update
        def crash(record):
            if record["items"][0]["status"] == "restored":
                raise RuntimeError("power loss")
            original(record)
        monkeypatch.setattr(store, "update", crash)
        with pytest.raises(RuntimeError):
            restore_operation(plan.id, store)
    else:
        original = operations.move_exclusive
        def crash(source, target, root):
            if phase != "before_move":
                original(source, target, root)
            raise RuntimeError("power loss")
        monkeypatch.setattr(operations, "move_exclusive", crash)
        with pytest.raises(RuntimeError):
            apply_plan(plan, store)
        if phase == "both_paths":
            b.write_bytes(b"new")
        elif phase == "missing_paths":
            Path(plan.items[0].target).unlink()
        elif phase == "pending":
            record = store.get(plan.id)
            record["items"][0]["status"] = "pending"
            store.update(record)
    reopened = Store(store.path)
    recover(reopened)
    record = reopened.get(plan.id)
    expected = {"before_move": "failed", "after_restore": "restored", "both_paths": "conflict", "missing_paths": "conflict", "pending": "failed"}
    assert record["items"][0]["status"] == expected[phase]


def test_missing_quarantine_and_restore_io_error(tmp_path, monkeypatch):
    root, b, store, plan = setup_plan(tmp_path)
    apply_plan(plan, store)
    def denied(*args):
        raise PermissionError("locked")
    monkeypatch.setattr(operations, "move_exclusive", denied)
    record = restore_operation(plan.id, store)
    assert record["items"][0]["error"] == "locked"
    Path(plan.items[0].target).unlink()
    record = restore_operation(plan.id, store)
    assert record["items"][0]["error"] and not b.exists()


def test_plan_validation_rejects_outside_and_keeper_moves(tmp_path):
    root, b, store, plan = setup_plan(tmp_path)
    with pytest.raises(ValueError):
        apply_plan(replace(plan, items=(replace(plan.items[0], target=str(tmp_path / "escape")),)), store)
    with pytest.raises(ValueError):
        apply_plan(replace(plan, items=(replace(plan.items[0], keeper=plan.items[0].source),)), store)
    with pytest.raises(ValueError):
        apply_plan(replace(plan, items=plan.items * 2), store)
    assert b.exists() and not store.operations()


def test_store_schema_and_unknown_id(tmp_path):
    import sqlite3
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    with pytest.raises(ValueError, match="not found"):
        store.get("unknown")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="schema"):
        Store(path)


def test_concurrent_journal_mutation_refused(tmp_path):
    root, b, store, plan = setup_plan(tmp_path)
    with store.exclusive():
        with pytest.raises(ValueError, match="already"):
            apply_plan(plan, Store(store.path))
    assert b.exists() and not store.operations()


def test_concurrent_folder_mutation_refused_across_databases(tmp_path):
    root, b, store, plan = setup_plan(tmp_path)
    with filesystem.exclusive_lock(root / ".file-organizer.lock"):
        with pytest.raises(ValueError, match="already"):
            apply_plan(plan, store)
    assert b.exists()
