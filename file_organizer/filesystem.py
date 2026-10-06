

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from .models import FileSnapshot

CHUNK = 1024 * 1024
RESERVED = {".file-organizer-quarantine", ".file-organizer.lock", ".data", ".venv", "__pycache__", ".git"}


@contextmanager
def exclusive_lock(path: Path):
    """OS releases the lock after a crash; keep the lock file to avoid races."""
    safe_path(path)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Another operation is already using this folder or history; retry when it finishes") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Cancelled(Exception):
    pass


def checkpoint(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(path))


def linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def safe_path(path: Path, root: Path | None = None) -> Path:
    path = absolute(path)
    if root is not None and not path.is_relative_to(absolute(root)):
        raise ValueError(f"Path is outside the selected folder: {path}")
    for component in reversed((path, *path.parents)):
        if os.path.lexists(component) and linked(component):
            raise ValueError(f"Links and reparse points are not supported: {component}")
    return path


def snapshot(path: Path) -> FileSnapshot:
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Not a regular file: {path}")
    return FileSnapshot(str(absolute(path)), info.st_size, info.st_mtime_ns,
                        info.st_dev, info.st_ino, info.st_nlink)


def unchanged(file: FileSnapshot, path: Path | None = None) -> None:
    current = snapshot(path or Path(file.path))
    fields = ("size", "mtime_ns", "device", "inode", "nlink")
    if any(getattr(current, f) != getattr(file, f) for f in fields):
        raise ValueError(f"File changed since it was scanned: {current.path}")


def digest_file(file: FileSnapshot, cancel=None, progress=None) -> str:
    unchanged(file)
    digest = hashlib.sha256()
    with open(file.path, "rb") as stream:
        while True:
            checkpoint(cancel)
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            if progress:
                progress(len(chunk))
    unchanged(file)
    return digest.hexdigest()


def equal_files(a: FileSnapshot, b: FileSnapshot, cancel=None) -> bool:
    unchanged(a)
    unchanged(b)
    same = True
    with open(a.path, "rb") as left, open(b.path, "rb") as right:
        while True:
            checkpoint(cancel)
            x, y = left.read(CHUNK), right.read(CHUNK)
            if x != y:
                same = False
                break
            if not x:
                break
    unchanged(a)
    unchanged(b)
    return same


def move_exclusive(source: Path, target: Path, root: Path) -> None:
    safe_path(source, root)
    safe_path(target, root)
    if os.path.lexists(target):
        raise FileExistsError(f"Destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_path(target, root)
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError("Cross-volume moves are not supported; source retained")
    if os.name == "nt":
        # Windows rename fails if the destination exists, including a race.
        os.rename(source, target)
    else:
        # Exclusive target creation; a crash between steps is recoverable as a
        # conflict with both paths present, never by silently removing either.
        os.link(source, target)
        source.unlink()
