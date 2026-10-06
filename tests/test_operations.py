import os
from pathlib import Path

import pytest

from file_organizer.operations import (
    apply_plan, cleanup_plan, organize_plan, recover, restore_operation,
)
from file_organizer.scanner import scan
from file_organizer.storage import Store
from test_scan import put


@pytest.fixture
def sample(tmp_path):
    root = tmp_path / "files"
    a = put(root, "a.txt")
    b = put(root, "b.txt")
    return root, a, b, Store(tmp_path / "state/history.sqlite3")


def test_quarantine_and_restore_round_trip(sample):
    root, a, b, store = sample
    result = scan(root)
    store.save_scan(result)
    plan = cleanup_plan(result, {str(b)})
    assert b.exists()  # planning is read-only
    record = apply_plan(plan, store)
    assert record["items"][0]["status"] == "done"
    assert a.read_bytes() == b"hello" and not b.exists()
    assert Path(record["items"][0]["target"]).read_bytes() == b"hello"
    record = restore_operation(plan.id, Store(store.path))
    assert record["items"][0]["status"] == "restored"
    assert b.read_bytes() == b"hello"
    assert len(store.scans()) == 1
    assert len(store.operations()) == 1


def test_refuse_every_copy_unknown_selection_and_empty_plan(sample):
    root, a, b, store = sample
    result = scan(root)
    for selected in ({str(a), str(b)}, {str(root / "unknown")}, set()):
        with pytest.raises(ValueError):
            cleanup_plan(result, selected)


@pytest.mark.parametrize("change", ["source", "keeper", "missing_keeper"])
def test_revalidate_before_cleanup(sample, change):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b)})
    if change == "source":
        b.write_bytes(b"changed")
    elif change == "keeper":
        a.write_bytes(b"changed")
    else:
        a.unlink()
    record = apply_plan(plan, store)
    assert record["items"][0]["status"] == "failed"
    assert b.exists()


def test_never_overwrite_on_apply_or_restore(sample):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b)})
    target = Path(plan.items[0].target)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    record = apply_plan(plan, store)
    assert record["items"][0]["status"] == "failed"
    assert target.read_bytes() == b"existing" and b.exists()
    plan = cleanup_plan(scan(root), {str(b)})
    apply_plan(plan, store)
    b.write_bytes(b"new file")
    record = restore_operation(plan.id, store)
    assert record["items"][0]["status"] == "done"
    assert record["items"][0]["error"]
    assert b.read_bytes() == b"new file"
    b.unlink()
    assert restore_operation(plan.id, store)["items"][0]["status"] == "restored"


def test_organization_collisions_unknown_and_undo(tmp_path):
    root = tmp_path / "files"
    picture = put(root, "nested/photo.JPG", b"photo")
    document = put(root, "a.txt")
    put(root, "Documents/a.txt", b"occupied")
    odd = put(root, "unknown.xyz", b"other")
    store = Store(tmp_path / "history.sqlite3")
    plan = organize_plan(scan(root))
    assert len(plan.items) == 2 and plan.notes
    assert document.exists() and picture.exists()
    record = apply_plan(plan, store)
    assert all(i["status"] == "done" for i in record["items"])
    assert (root / "Images/photo.JPG").exists()
    assert (root / "Other/unknown.xyz").exists()
    assert not organize_plan(scan(root)).items
    restore_operation(plan.id, store)
    assert picture.exists() and odd.exists()


def test_changed_quarantine_not_restored(sample):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b)})
    apply_plan(plan, store)
    Path(plan.items[0].target).write_bytes(b"tampered")
    record = restore_operation(plan.id, store)
    assert record["items"][0]["status"] == "done"
    assert record["items"][0]["error"] and not b.exists()


def test_crash_after_move_recovered(sample, monkeypatch):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b)})
    original = store.update
    def crash(record):
        if any(i["status"] == "done" for i in record["items"]):
            raise RuntimeError("simulated power loss")
        original(record)
    monkeypatch.setattr(store, "update", crash)
    with pytest.raises(RuntimeError):
        apply_plan(plan, store)
    reopened = Store(store.path)
    assert reopened.get(plan.id)["items"][0]["status"] == "moving"
    recover(reopened)
    assert reopened.get(plan.id)["items"][0]["status"] == "done"
    restore_operation(plan.id, reopened)
    assert b.exists()


def test_hardlinked_files_are_not_mutated(sample):
    root, a, b, store = sample
    os.link(b, root / "alias")
    result = scan(root)
    hardlinked = next(f for f in result.files if f.nlink > 1)
    with pytest.raises(ValueError):
        cleanup_plan(result, {hardlinked.path})
    plan = organize_plan(result)
    assert all(i.source.nlink == 1 for i in plan.items)


def test_incomplete_scan_cannot_be_used(sample):
    from threading import Event
    root, a, b, store = sample
    event = Event()
    event.set()
    result = scan(root, cancel=event)
    with pytest.raises(ValueError):
        organize_plan(result)


def test_same_plan_cannot_run_twice(sample):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b)})
    apply_plan(plan, store)
    with pytest.raises(ValueError):
        apply_plan(plan, store)


@pytest.mark.skipif(os.name != "nt", reason="Windows path case semantics")
def test_cleanup_selection_accepts_windows_path_case(sample):
    root, a, b, store = sample
    plan = cleanup_plan(scan(root), {str(b).upper()})
    assert plan.items[0].source.path == str(b)
