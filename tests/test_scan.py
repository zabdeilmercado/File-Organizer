import csv
import json
import os
from pathlib import Path
from threading import Event

import pytest

from file_organizer.scanner import scan
from file_organizer.reports import export_report


def put(root, name, data=b"hello"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_exact_duplicates_nested_unicode_and_empty(tmp_path):
    put(tmp_path, "a.txt")
    put(tmp_path, "nested/日本.txt")
    put(tmp_path, "other/a.txt", b"world")
    put(tmp_path, "empty", b"")
    put(tmp_path, "empty2", b"")
    result = scan(tmp_path)
    assert result.complete
    assert len(result.files) == 5
    assert len(result.groups) == 2
    assert result.reclaimable_bytes == 5
    assert sorted(len(g.files) for g in result.groups) == [2, 2]


def test_cancellation_and_invalid_root(tmp_path):
    put(tmp_path, "a")
    cancel = Event()
    cancel.set()
    result = scan(tmp_path, cancel=cancel)
    assert result.cancelled and not result.complete
    with pytest.raises(ValueError):
        scan(tmp_path / "missing")


def test_hardlinks_not_double_counted_and_internal_folders_skipped(tmp_path):
    source = put(tmp_path, "a")
    os.link(source, tmp_path / "alias")
    put(tmp_path, "copy")
    put(tmp_path, ".file-organizer-quarantine/hidden")
    put(tmp_path, ".data/hidden")
    result = scan(tmp_path)
    assert len(result.files) == 2
    assert result.reclaimable_bytes == 5
    assert result.skipped


def test_excludes_and_large_streaming_files(tmp_path):
    put(tmp_path, "large", b"x" * (2 * 1024 * 1024 + 13))
    put(tmp_path, "large2", b"x" * (2 * 1024 * 1024 + 13))
    excluded = put(tmp_path, "excluded/x")
    progress = []
    result = scan(tmp_path, excluded=(excluded.parent,), progress=progress.append)
    assert len(result.files) == 2
    assert result.bytes_hashed > 4 * 1024 * 1024
    assert progress


def test_read_error_marks_incomplete(tmp_path, monkeypatch):
    put(tmp_path, "a")
    put(tmp_path, "b")
    from file_organizer import scanner
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(scanner, "digest_file", denied)
    result = scan(tmp_path)
    assert not result.complete
    assert result.warnings


def test_export_is_exclusive_and_csv_is_safe(tmp_path):
    root = tmp_path / "files"
    put(root, "=formula.csv")
    put(root, "copy.csv")
    result = scan(root)
    export_report(result, tmp_path / "report.json", "json")
    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["complete"] is True
    assert payload["reclaimable_bytes"] == 5
    export_report(result, tmp_path / "report.csv", "csv")
    with (tmp_path / "report.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert next(r for r in rows if "formula" in r["name"])["name"].startswith("'=")
    with pytest.raises(FileExistsError):
        export_report(result, tmp_path / "report.json", "json")
    with pytest.raises(ValueError):
        export_report(result, tmp_path / "bad", "xml")


def test_managed_folder_cannot_be_chosen_as_root(tmp_path):
    folder = tmp_path / ".file-organizer-quarantine" / "operation"
    put(folder, "a")
    with pytest.raises(ValueError, match="managed"):
        scan(folder)
