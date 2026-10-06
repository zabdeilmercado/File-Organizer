from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    nlink: int
    digest: str = ""


@dataclass(frozen=True)
class DuplicateGroup:
    files: tuple[FileSnapshot, ...]

    @property
    def reclaimable_bytes(self) -> int:
        # Multi-link identities must stay. If any exist, all single-link copies
        # can be quarantined; otherwise at least one single-link copy must stay.
        movable = sum(f.nlink == 1 for f in self.files)
        keep = 0 if any(f.nlink > 1 for f in self.files) else 1
        return max(0, movable - keep) * self.files[0].size


@dataclass
class ScanResult:
    root: str
    files: list[FileSnapshot] = field(default_factory=list)
    groups: list[DuplicateGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cancelled: bool = False
    bytes_hashed: int = 0
    id: str = field(default_factory=lambda: uuid4().hex)
    created: str = field(default_factory=timestamp)

    @property
    def complete(self) -> bool:
        return not self.cancelled and not self.warnings

    @property
    def reclaimable_bytes(self) -> int:
        return sum(g.reclaimable_bytes for g in self.groups)

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self), "complete": self.complete,
                "reclaimable_bytes": self.reclaimable_bytes}


@dataclass(frozen=True)
class MoveItem:
    source: FileSnapshot
    target: str
    keeper: FileSnapshot | None = None


@dataclass(frozen=True)
class Plan:
    id: str
    kind: str
    root: str
    items: tuple[MoveItem, ...]
    notes: tuple[str, ...] = ()
