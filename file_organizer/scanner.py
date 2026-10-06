
import os
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from .filesystem import (RESERVED, Cancelled, absolute, checkpoint, digest_file,
                         equal_files, linked, safe_path, snapshot)
from .models import DuplicateGroup, ScanResult


def scan(root: Path, cancel=None, progress=None, excluded=()) -> ScanResult:
    root = safe_path(absolute(root))
    if any(part.casefold() in RESERVED for part in root.parts):
        raise ValueError("Choose a folder outside application-managed or excluded directories")
    if not root.is_dir():
        raise ValueError(f"Folder does not exist: {root}")
    result = ScanResult(str(root))
    exclusions = tuple(absolute(p) for p in excluded)
    identities = set()
    sizes = defaultdict(list)

    def notify(phase):
        if progress:
            progress({"phase": phase, "files": len(result.files),
                      "bytes_hashed": result.bytes_hashed})

    def hashed(amount):
        result.bytes_hashed += amount
        notify("Comparing contents")

    try:
        pending = [root]
        while pending:
            checkpoint(cancel)
            folder = pending.pop()
            try:
                safe_path(folder, root)
                with os.scandir(folder) as entries:
                    for entry in entries:
                        checkpoint(cancel)
                        path = Path(entry.path)
                        try:
                            if (entry.name.casefold() in RESERVED or
                                any(path == p or path.is_relative_to(p) for p in exclusions)):
                                result.skipped.append(f"Excluded: {path}")
                            elif linked(path):
                                result.skipped.append(f"Link/reparse point: {path}")
                            elif entry.is_dir(follow_symlinks=False):
                                pending.append(path)
                            elif entry.is_file(follow_symlinks=False):
                                file = snapshot(path)
                                key = (file.device, file.inode)
                                if key in identities:
                                    result.skipped.append(f"Hard-link alias: {path}")
                                    continue
                                identities.add(key)
                                result.files.append(file)
                                sizes[file.size].append(file)
                                notify("Finding files")
                            else:
                                result.skipped.append(f"Not a regular file: {path}")
                        except (OSError, ValueError) as error:
                            result.warnings.append(f"{path}: {error}")
            except (OSError, ValueError) as error:
                result.warnings.append(f"{folder}: {error}")

        replacements = {}
        for candidates in sizes.values():
            checkpoint(cancel)
            if len(candidates) < 2:
                continue
            hashes = defaultdict(list)
            for file in sorted(candidates, key=lambda f: f.path.casefold()):
                try:
                    hashed_file = replace(file, digest=digest_file(file, cancel, hashed))
                    replacements[file.path] = hashed_file
                    hashes[hashed_file.digest].append(hashed_file)
                except (OSError, ValueError) as error:
                    result.warnings.append(f"{file.path}: {error}")
            for matches in hashes.values():
                verified = []
                for file in matches:
                    try:
                        for group in verified:
                            if equal_files(group[0], file, cancel):
                                group.append(file)
                                break
                        else:
                            verified.append([file])
                    except (OSError, ValueError) as error:
                        result.warnings.append(f"{file.path}: {error}")
                result.groups.extend(DuplicateGroup(tuple(g)) for g in verified if len(g) > 1)
        result.files = [replacements.get(f.path, f) for f in result.files]
        result.groups.sort(key=lambda g: (-g.reclaimable_bytes, g.files[0].path))
        checkpoint(cancel)
    except Cancelled:
        result.cancelled = True
    notify("Cancelled" if result.cancelled else "Finished")
    return result
