import csv
import json
from pathlib import Path

from .models import ScanResult


def csv_safe(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or value.startswith(("\t", "\r", "\n")) else value


def export_report(result: ScanResult, path: Path, format: str) -> None:
    if format not in {"json", "csv"}:
        raise ValueError("Report format must be json or csv")
    with Path(path).open("x", encoding="utf-8-sig" if format == "csv" else "utf-8", newline="") as stream:
        if format == "json":
            json.dump(result.to_dict(), stream, ensure_ascii=False, indent=2)
        else:
            writer = csv.writer(stream)
            writer.writerow(["group", "name", "path", "size_bytes", "modified_ns", "digest", "scan_complete"])
            for number, group in enumerate(result.groups, 1):
                for file in group.files:
                    writer.writerow([number, csv_safe(Path(file.path).name), csv_safe(file.path),
                                     file.size, file.mtime_ns, file.digest, result.complete])
