
import json
import queue
import threading
import time
import tkinter as tk
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .operations import apply_plan, cleanup_plan, organize_plan, recover, restore_operation
from .reports import export_report
from .scanner import scan
from .storage import Store


def size_label(size):
    amount = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:,.1f} {unit}"
        amount /= 1024


class OrganizerApp:
    def __init__(self, root: tk.Tk, store: Store):
        self.root, self.store = root, store
        self.result = None
        self.selected = set()
        self.file_rows = {}
        self.history_rows = {}
        self.busy = False
        self.closing = False
        self.scan_job = False
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.folder = tk.StringVar()
        self.status = tk.StringVar(value="Ready — choose a folder to begin.")
        self.summary = tk.StringVar(value="Your files stay on this computer.")
        self.buttons = []
        self.root.title("File Organizer · local & recoverable")
        self.root.geometry("1120x760")
        self.root.minsize(850, 600)
        self.root.configure(background="#edf2f7")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabel", background="#edf2f7", foreground="#182c44", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#176b68", foreground="white")
        style.map("Accent.TButton", background=[("active", "#11534f"), ("disabled", "#839a99")])
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=7)
        header = tk.Frame(root, bg="#172c43", padx=24, pady=18)
        header.pack(fill="x")
        tk.Label(header, text="FILE ORGANIZER", bg="#172c43", fg="white",
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(header, text="Find exact duplicates. Review every move. Restore when needed.",
                 bg="#172c43", fg="#bdcfdd", font=("Segoe UI", 11)).pack(anchor="w", pady=(5, 0))
        content = ttk.Frame(root, padding=20)
        content.pack(fill="both", expand=True)
        picker = ttk.Frame(content)
        picker.pack(fill="x")
        self.entry = ttk.Entry(picker, textvariable=self.folder, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.button(picker, "Browse…", self.browse)
        self.button(picker, "Scan folder", self.start_scan, accent=True)
        self.cancel_button = ttk.Button(picker, text="Cancel", command=self.cancel_scan, state="disabled")
        self.cancel_button.pack(side="left", padx=4)
        ttk.Label(content, textvariable=self.status, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(16, 4))
        ttk.Label(content, textvariable=self.summary).pack(anchor="w", pady=(0, 8))
        self.progressbar = ttk.Progressbar(content, mode="indeterminate")
        self.progressbar.pack(fill="x", pady=(0, 16))
        self.tabs = ttk.Notebook(content)
        self.tabs.pack(fill="both", expand=True)
        duplicates_tab, organize_tab, history_tab, details_tab = [ttk.Frame(self.tabs, padding=12) for _ in range(4)]
        for tab, label in zip((duplicates_tab, organize_tab, history_tab, details_tab),
                              ("  Duplicates  ", "  Organize  ", "  History & restore  ", "  Scan details  ")):
            self.tabs.add(tab, text=label)
        ttk.Label(duplicates_tab, text="Select copies and press Space to mark for quarantine. Keep at least one copy per group.").pack(anchor="w", pady=(0, 8))
        self.duplicates = self.tree(duplicates_tab, ("action", "path", "size", "modified"), (100, 520, 100, 155), tree=True)
        self.duplicates.bind("<space>", lambda event: self.toggle_selected())
        self.duplicates.bind("<Double-1>", lambda event: self.toggle_selected())
        actions = ttk.Frame(duplicates_tab)
        actions.pack(fill="x", pady=(10, 0))
        self.button(actions, "Toggle selected copies", self.toggle_selected)
        self.button(actions, "Preview cleanup", self.preview_cleanup, accent=True)
        self.button(actions, "Export JSON", lambda: self.export("json"))
        self.button(actions, "Export CSV", lambda: self.export("csv"))
        ttk.Label(organize_tab, text="Organize by file type", font=("Segoe UI", 17, "bold")).pack(anchor="w", pady=12)
        ttk.Label(organize_tab, text="Images  ·  Documents  ·  Audio  ·  Video  ·  Archives  ·  Other",
                  font=("Segoe UI", 12)).pack(anchor="w", pady=8)
        ttk.Label(organize_tab, text="Preview moves into category folders under your scanned folder.\n"
                  "Existing files are never overwritten. Conflicts stay where they are.\n"
                  "You can choose which planned moves to apply and undo them from History.",
                  wraplength=750).pack(anchor="w", pady=12)
        self.button(organize_tab, "Preview organization", self.preview_organize, accent=True)
        self.history = self.tree(history_tab, ("created", "kind", "summary", "id"), (185, 90, 260, 270))
        history_actions = ttk.Frame(history_tab)
        history_actions.pack(fill="x", pady=(10, 0))
        self.button(history_actions, "Refresh", self.refresh_history)
        self.button(history_actions, "View details", self.history_details)
        self.button(history_actions, "Restore selected operation", self.restore, accent=True)
        self.details = tk.Text(details_tab, wrap="word", font=("Consolas", 10), relief="flat", padx=10, pady=10)
        self.details.pack(fill="both", expand=True)
        self.set_details("Scan a folder to see warnings, skipped paths, and exact byte counts.")
        ttk.Label(content, text="Cleanup uses quarantine • No permanent deletion • Physical disk savings may differ",
                  foreground="#50647b").pack(anchor="w", pady=(12, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_history()
        self.entry.focus_set()

    def button(self, parent, text, command, accent=False):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        button.pack(side="left", padx=(0, 6))
        self.buttons.append(button)
        return button

    def tree(self, parent, columns, widths, tree=False):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        widget = ttk.Treeview(frame, columns=columns, height=4,
                             show="tree headings" if tree else "headings", selectmode="extended")
        for column, width in zip(columns, widths):
            widget.heading(column, text=column.replace("_", " ").title())
            widget.column(column, width=width, minwidth=70, stretch=column in {"path", "summary"})
        if tree:
            widget.heading("#0", text="Group")
            widget.column("#0", width=125, minwidth=100, stretch=False)
        scroll_y = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
        scroll_x = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        widget.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        widget.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return widget

    def browse(self):
        folder = filedialog.askdirectory(parent=self.root, title="Choose a folder to scan")
        if folder:
            self.folder.set(folder)

    def run_job(self, work, finished, label, scanning=False):
        if self.busy:
            return
        self.busy, self.scan_job = True, scanning
        self.cancel.clear()
        self.status.set(label)
        for button in self.buttons:
            button.configure(state="disabled")
        self.entry.configure(state="disabled")
        self.cancel_button.configure(state="normal" if scanning else "disabled")
        self.progressbar.start(15)
        def worker():
            try:
                self.events.put(("done", (finished, work())))
            except Exception as error:
                self.events.put(("error", error))
        threading.Thread(target=worker, name="organizer-worker", daemon=False).start()
        self.root.after(40, self.poll)

    def poll(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.summary.set(f"{payload['phase']} · {payload['files']:,} files · {size_label(payload['bytes_hashed'])} hashed")
            else:
                self.busy = False
                self.progressbar.stop()
                for button in self.buttons:
                    button.configure(state="normal")
                self.entry.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                if self.closing:
                    self.root.destroy()
                    return
                if kind == "error":
                    self.status.set("Could not complete the operation.")
                    messagebox.showerror("Operation stopped", str(payload), parent=self.root)
                else:
                    callback, result = payload
                    callback(result)
        if self.busy:
            self.root.after(40, self.poll)

    def start_scan(self):
        if not self.folder.get().strip():
            messagebox.showinfo("Choose a folder", "Browse to a folder first.", parent=self.root)
            return
        folder = Path(self.folder.get())
        self.result = None
        self.selected.clear()
        self.file_rows.clear()
        self.duplicates.delete(*self.duplicates.get_children())
        def work():
            last = 0.0
            def progress(event):
                nonlocal last
                now = time.monotonic()
                if now - last >= 0.1:
                    self.events.put(("progress", event))
                    last = now
            result = scan(folder, self.cancel, progress, excluded=self.store.excluded)
            self.store.save_scan(result)
            return result
        self.run_job(work, self.show_scan, "Scanning folder…", scanning=True)

    def show_scan(self, result):
        self.result = result
        self.status.set(f"{'Complete' if result.complete else 'Incomplete'} — {len(result.groups)} duplicate groups found.")
        self.summary.set(f"{len(result.files):,} files · {size_label(result.reclaimable_bytes)} potentially reclaimable · {len(result.warnings)} warnings")
        for number, group in enumerate(result.groups, 1):
            parent = self.duplicates.insert("", "end", text=f"Group {number}", open=True,
                                            values=("", f"{len(group.files)} copies", size_label(group.reclaimable_bytes), ""))
            for file in group.files:
                modified = datetime.fromtimestamp(file.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M")
                iid = self.duplicates.insert(parent, "end", values=("Keep (linked)" if file.nlink > 1 else "Keep", file.path, size_label(file.size), modified))
                self.file_rows[iid] = file
        self.set_details(f"Root: {result.root}\nComplete: {result.complete}\nCancelled: {result.cancelled}\n"
                         f"Logical bytes reclaimable: {result.reclaimable_bytes:,}\nBytes hashed: {result.bytes_hashed:,}\n\n"
                         + "WARNINGS\n" + ("\n".join(result.warnings) or "None") + "\n\nSKIPPED\n" + ("\n".join(result.skipped) or "None"))
        self.refresh_history()

    def set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def toggle_selected(self):
        if self.busy or not self.result:
            return
        for iid in self.duplicates.selection():
            file = self.file_rows.get(iid)
            if not file or file.nlink > 1:
                continue
            if file.path in self.selected:
                self.selected.remove(file.path)
            else:
                self.selected.add(file.path)
            self.duplicates.set(iid, "action", "Quarantine" if file.path in self.selected else "Keep")

    def require_scan(self):
        if self.result is None or not self.result.complete:
            messagebox.showinfo("Complete scan needed", "Run a complete scan without read errors before planning changes.", parent=self.root)
            return False
        return True

    def preview_cleanup(self):
        if self.require_scan():
            result, selected = self.result, set(self.selected)
            self.run_job(lambda: cleanup_plan(result, selected), self.show_plan, "Verifying selected copies…")

    def preview_organize(self):
        if self.require_scan():
            result = self.result
            self.run_job(lambda: organize_plan(result), self.show_plan, "Preparing organization preview…")

    def show_plan(self, plan):
        self.status.set(f"Preview ready — {len(plan.items)} moves. Nothing has changed yet.")
        dialog = tk.Toplevel(self.root)
        dialog.title("Review changes · " + plan.kind)
        dialog.geometry("1000x530")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Review source and destination. Select the rows you want to apply.", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))
        table = self.tree(body, ("source", "destination"), (460, 460))
        for index, item in enumerate(plan.items):
            table.insert("", "end", iid=str(index), values=(item.source.path, item.target))
        table.selection_set(table.get_children())
        notes = tk.Text(body, height=4, wrap="word", font=("Segoe UI", 10))
        notes.pack(fill="x", pady=10)
        notes.insert("1.0", "\n".join(plan.notes) or "No destination conflicts. Files are rechecked before each move.")
        notes.configure(state="disabled")
        controls = ttk.Frame(body)
        controls.pack(fill="x")
        def apply_selected():
            chosen = tuple(plan.items[int(i)] for i in table.selection())
            if not chosen:
                return
            if messagebox.askyesno("Apply file moves?", f"Move {len(chosen)} files?\n\n"
                                  "You can restore these moves from History. Close other apps editing these files first.", parent=dialog):
                dialog.destroy()
                self.run_job(lambda: apply_plan(replace(plan, items=chosen), self.store), self.show_operation, "Applying reviewed moves…")
        ttk.Button(controls, text="Apply selected moves", command=apply_selected, style="Accent.TButton",
                   state="normal" if plan.items else "disabled").pack(side="right")
        ttk.Button(controls, text="Close preview", command=dialog.destroy).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda event: dialog.destroy())

    def show_operation(self, record):
        self.result = None
        self.selected.clear()
        self.file_rows.clear()
        self.duplicates.delete(*self.duplicates.get_children())
        counts = ", ".join(f"{v} {k}" for k, v in Counter(i["status"] for i in record["items"]).items())
        self.status.set(f"Operation finished — {counts}. Rescan before making more changes.")
        self.set_details(json.dumps(record, indent=2, ensure_ascii=False))
        self.refresh_history()
        self.tabs.select(2)
        errors = [i["error"] for i in record["items"] if i["error"]]
        if errors:
            messagebox.showwarning("Some files were not moved", "\n".join(errors[:8]) + "\n\nSee History details for the full record.", parent=self.root)

    def export(self, format):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension="." + format,
                                          filetypes=[(format.upper(), "*." + format)], confirmoverwrite=False)
        if path:
            result = self.result
            self.run_job(lambda: export_report(result, Path(path), format),
                         lambda _: self.status.set(f"Report saved: {path}"), "Exporting report…")

    def refresh_history(self):
        self.history.delete(*self.history.get_children())
        self.history_rows.clear()
        for record in self.store.operations():
            summary = ", ".join(f"{v} {k}" for k, v in Counter(i["status"] for i in record["items"]).items())
            iid = self.history.insert("", "end", values=(record["created"], record["kind"], summary, record["id"]))
            self.history_rows[iid] = record
        for record in self.store.scans():
            summary = f"{len(record['files'])} files · {'complete' if record['complete'] else 'incomplete'}"
            iid = self.history.insert("", "end", values=(record["created"], "scan", summary, record["id"]))
            self.history_rows[iid] = record

    def history_record(self):
        selection = self.history.selection()
        return self.history_rows.get(selection[0]) if selection else None

    def history_details(self):
        record = self.history_record()
        if record:
            self.set_details(json.dumps(record, ensure_ascii=False, indent=2))
            self.tabs.select(3)

    def restore(self):
        record = self.history_record()
        if not record or "items" not in record:
            messagebox.showinfo("Choose an operation", "Select a cleanup or organization entry in History.", parent=self.root)
            return
        count = sum(i["status"] == "done" for i in record["items"])
        if not count:
            messagebox.showinfo("Nothing to restore", "This operation has no completed moves available to restore.", parent=self.root)
        elif messagebox.askyesno("Restore operation?", f"Restore {count} files to their original paths?\nOccupied paths will be skipped.", parent=self.root):
            self.run_job(lambda: restore_operation(record["id"], self.store), self.show_operation, "Restoring files…")

    def cancel_scan(self):
        self.cancel.set()
        self.status.set("Cancelling scan…")
        self.cancel_button.configure(state="disabled")

    def close(self):
        if not self.busy:
            self.root.destroy()
        elif self.scan_job:
            self.closing = True
            self.cancel_scan()
        elif messagebox.askyesno("Operation in progress", "Close automatically after this operation finishes?", parent=self.root):
            self.closing = True


def launch(store: Store):
    root = tk.Tk()
    app = OrganizerApp(root, store)
    app.run_job(lambda: recover(store), lambda _: (app.refresh_history(), app.status.set("Ready — choose a folder to begin.")),
                "Checking for interrupted operations…")
    root.mainloop()
