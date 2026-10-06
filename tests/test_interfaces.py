import json
from pathlib import Path

import pytest

from file_organizer.cli import main
from test_scan import put


def test_cli_scan_preview_apply_and_restore(tmp_path, capsys):
    root = tmp_path / "files"
    put(root, "a")
    b = put(root, "b")
    db = tmp_path / "history.sqlite3"
    args = ["--db", str(db)]
    assert main(args + ["scan", str(root), "--json", str(tmp_path / "report.json")]) == 0
    assert "duplicate" in capsys.readouterr().out.lower()
    assert main(args + ["cleanup", str(root), "--remove", str(b)]) == 0
    assert b.exists()
    assert main(args + ["cleanup", str(root), "--remove", str(b), "--apply"]) == 0
    assert not b.exists()
    from file_organizer.storage import Store
    operation_id = Store(db).operations()[0]["id"]
    assert main(args + ["restore", operation_id]) == 0
    assert not b.exists()
    assert main(args + ["restore", operation_id, "--apply"]) == 0
    assert b.exists()
    assert main(args + ["history"]) == 0


def test_cli_organization_and_errors(tmp_path, capsys):
    put(tmp_path / "files", "x.png")
    args = ["--db", str(tmp_path / "db.sqlite3")]
    assert main(args + ["organize", str(tmp_path / "files")]) == 0
    assert (tmp_path / "files/x.png").exists()
    assert main(args + ["organize", str(tmp_path / "files"), "--apply"]) == 0
    assert (tmp_path / "files/Images/x.png").exists()
    assert main(args + ["scan", str(tmp_path / "missing")]) == 2
    assert "Error" in capsys.readouterr().err


def test_tk_scan_and_review_flow(tmp_path):
    import time
    import tkinter as tk
    from file_organizer.gui import OrganizerApp
    from file_organizer.storage import Store
    root = tk.Tk()
    root.withdraw()
    try:
        app = OrganizerApp(root, Store(tmp_path / "db.sqlite3"))
        folder = tmp_path / "files"
        put(folder, "a")
        put(folder, "b")
        app.folder.set(str(folder))
        app.start_scan()
        deadline = time.monotonic() + 10
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert not app.busy and app.result.complete
        assert len(app.result.groups) == 1
        assert len(app.file_rows) == 2
        iid = next(iter(app.file_rows))
        app.duplicates.selection_set(iid)
        app.toggle_selected()
        assert len(app.selected) == 1
        app.toggle_selected()
        assert not app.selected
        assert app.status.get().startswith("Complete")
    finally:
        root.destroy()


def test_tk_controls_fit_minimum_window(tmp_path):
    import tkinter as tk
    from file_organizer.gui import OrganizerApp
    from file_organizer.storage import Store
    root = tk.Tk()
    try:
        app = OrganizerApp(root, Store(tmp_path / "db.sqlite3"))
        root.geometry("850x600")
        root.update()
        visible_buttons = [b for b in app.buttons if b.winfo_ismapped()]
        assert len(visible_buttons) == 6
        bottom = root.winfo_rooty() + root.winfo_height()
        assert all(b.winfo_rooty() + b.winfo_height() <= bottom - 10 for b in visible_buttons)
        assert all(b.winfo_height() >= 30 for b in visible_buttons)
    finally:
        root.destroy()


def pump(root, app):
    import time
    deadline = time.monotonic() + 10
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert not app.busy
    root.update()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def test_desktop_preview_apply_restore_export_journey(tmp_path, monkeypatch):
    import tkinter as tk
    from tkinter import ttk
    from file_organizer import gui
    from file_organizer.storage import Store
    root = tk.Tk()
    root.withdraw()
    errors = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **kw: errors.append(a))
    try:
        app = gui.OrganizerApp(root, Store(tmp_path / "db.sqlite3"))
        folder = tmp_path / "files"
        put(folder, "a.txt")
        b = put(folder, "b.txt")
        app.folder.set(str(folder))
        app.start_scan()
        pump(root, app)
        export = tmp_path / "scan.json"
        monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(export))
        app.export("json")
        pump(root, app)
        assert json.loads(export.read_text())["complete"]
        row = next(i for i, f in app.file_rows.items() if f.path == str(b))
        app.duplicates.selection_set(row)
        app.toggle_selected()
        app.preview_cleanup()
        pump(root, app)
        assert b.exists()
        apply = next(w for w in descendants(root) if isinstance(w, ttk.Button) and w.cget("text") == "Apply selected moves")
        apply.invoke()
        pump(root, app)
        assert not b.exists() and app.result is None
        history_row = next(i for i, r in app.history_rows.items() if r.get("kind") == "cleanup")
        app.history.selection_set(history_row)
        app.restore()
        pump(root, app)
        assert b.read_bytes() == b"hello"
        app.start_scan()
        pump(root, app)
        app.preview_organize()
        pump(root, app)
        apply = next(w for w in descendants(root) if isinstance(w, ttk.Button) and w.cget("text") == "Apply selected moves")
        apply.invoke()
        pump(root, app)
        assert (folder / "Documents/a.txt").exists()
        assert (folder / "Documents/b.txt").exists()
        assert not errors
    finally:
        root.destroy()


def test_desktop_cancellation_disables_overlapping_jobs(tmp_path, monkeypatch):
    import tkinter as tk
    from file_organizer import gui
    from file_organizer.models import ScanResult
    from file_organizer.storage import Store
    root = tk.Tk()
    root.withdraw()
    def delayed_scan(folder, cancel, *args, **kwargs):
        assert cancel.wait(5)
        return ScanResult(str(folder), cancelled=True)
    monkeypatch.setattr(gui, "scan", delayed_scan)
    try:
        app = gui.OrganizerApp(root, Store(tmp_path / "db.sqlite3"))
        app.folder.set(str(tmp_path))
        app.start_scan()
        assert app.busy
        assert all(str(b.cget("state")) == "disabled" for b in app.buttons)
        app.cancel_scan()
        pump(root, app)
        assert app.result.cancelled
        assert app.status.get().startswith("Incomplete")
    finally:
        root.destroy()
