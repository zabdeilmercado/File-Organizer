import argparse
import json
import sys
from pathlib import Path

from .operations import apply_plan, cleanup_plan, organize_plan, recover, restore_operation
from .reports import export_report
from .scanner import scan
from .storage import DEFAULT_DB, Store


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Review, organize, and recover local file moves.")
    command.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite history location")
    sub = command.add_subparsers(dest="command")
    sub.add_parser("gui", help="Open the desktop application (default)")
    scan_args = sub.add_parser("scan", help="Find exact duplicates without changing files")
    scan_args.add_argument("folder", type=Path)
    scan_args.add_argument("--json", type=Path)
    scan_args.add_argument("--csv", type=Path)
    for name in ("cleanup", "organize"):
        options = sub.add_parser(name, help=f"Preview {name}; add --apply to move files")
        options.add_argument("folder", type=Path)
        options.add_argument("--apply", action="store_true")
        if name == "cleanup":
            options.add_argument("--remove", action="append", required=True,
                                 help="Path of a duplicate copy to quarantine; repeat as needed")
    sub.add_parser("history", help="Show scans, operations, and recovery status")
    restore = sub.add_parser("restore", help="Preview restoring an operation")
    restore.add_argument("operation_id")
    restore.add_argument("--apply", action="store_true")
    return command


def operation_ok(record: dict, expected: str) -> bool:
    return all(i["status"] == expected and not i["error"] for i in record["items"])


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        store = Store(args.db)
        if args.command in (None, "gui"):
            from .gui import launch
            launch(store)
            return 0
        recover(store)
        if args.command == "history":
            print(json.dumps({"scans": store.scans(), "operations": store.operations()}, indent=2))
            return 0
        if args.command == "restore":
            record = store.get(args.operation_id)
            if not args.apply:
                for item in record["items"]:
                    if item["status"] == "done":
                        print(f"RESTORE {item['target']} -> {item['source']['path']}")
                print("Preview only. Add --apply to restore; occupied paths will be skipped.")
                return 0
            record = restore_operation(args.operation_id, store)
            print(json.dumps(record, indent=2))
            return 0 if operation_ok(record, "restored") else 2
        result = scan(args.folder, excluded=store.excluded)
        store.save_scan(result)
        print(f"{'Complete' if result.complete else 'Incomplete'}: {len(result.files)} files, "
              f"{len(result.groups)} duplicate groups, "
              f"{result.reclaimable_bytes:,} logical bytes reclaimable.")
        for warning in result.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if args.command == "scan":
            for n, group in enumerate(result.groups, 1):
                print(f"\nDuplicate group {n}")
                for file in group.files:
                    print(f"  {file.path} ({file.size:,} bytes)")
            for format in ("json", "csv"):
                path = getattr(args, format)
                if path:
                    export_report(result, path, format)
            return 0 if result.complete else 2
        plan = cleanup_plan(result, set(args.remove)) if args.command == "cleanup" else organize_plan(result)
        for item in plan.items:
            print(f"MOVE {item.source.path} -> {item.target}")
        for note in plan.notes:
            print(note)
        if not args.apply:
            print("Preview only. Add --apply to execute these moves.")
            return 0
        if not plan.items:
            print("No files to move.")
            return 0
        record = apply_plan(plan, store)
        print(json.dumps(record, indent=2))
        return 0 if operation_ok(record, "done") else 2
    except KeyboardInterrupt:
        print("Interrupted. Any started moves will be reconciled on the next run.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"Unexpected error: {error}", file=sys.stderr)
        return 1
