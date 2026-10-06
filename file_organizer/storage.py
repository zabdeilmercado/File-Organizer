
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .filesystem import exclusive_lock, safe_path

DEFAULT_DB = Path(__file__).resolve().parent.parent / ".data" / "history.sqlite3"


class Store:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = safe_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported history schema: {version}")
            db.execute("CREATE TABLE IF NOT EXISTS scans (id TEXT PRIMARY KEY, created TEXT, root TEXT, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, created TEXT, kind TEXT, root TEXT, payload TEXT NOT NULL)")
            db.execute("PRAGMA user_version = 1")

    def exclusive(self):
        return exclusive_lock(Path(str(self.path) + ".lock"))

    @property
    def excluded(self):
        return tuple(Path(str(self.path) + suffix) for suffix in ("", "-journal", "-wal", "-shm", ".lock"))

    @contextmanager
    def connection(self):
        safe_path(self.path)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_scan(self, result):
        with self.connection() as db:
            db.execute("INSERT INTO scans VALUES (?, ?, ?, ?)",
                       (result.id, result.created, result.root, json.dumps(result.to_dict())))

    def create(self, record: dict):
        try:
            with self.connection() as db:
                db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?)",
                           (record["id"], record["created"], record["kind"], record["root"], json.dumps(record)))
        except sqlite3.IntegrityError as error:
            raise ValueError("This plan has already been executed; create a fresh plan") from error

    def update(self, record: dict):
        with self.connection() as db:
            db.execute("UPDATE operations SET payload = ? WHERE id = ?", (json.dumps(record), record["id"]))

    def get(self, operation_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM operations WHERE id = ?", (operation_id,)).fetchone()
        if not row:
            raise ValueError(f"Operation not found: {operation_id}")
        return json.loads(row[0])

    def operations(self) -> list[dict]:
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM operations ORDER BY created DESC, rowid DESC")]

    def scans(self) -> list[dict]:
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM scans ORDER BY created DESC, rowid DESC")]
