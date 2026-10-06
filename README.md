# File Organizer and Duplicate Finder

An offline Python desktop app and CLI for exact duplicate detection, reviewed cleanup, and category-based organization. Runtime dependencies are Python's standard library only. Developed and tested on Windows with Python 3.12 and Tk 8.6.

## Start the desktop app

Open PowerShell in this `File_Organizer` directory:

```powershell
python -m file_organizer gui
```

Alternatively run `launch.ps1` from PowerShell. If your system blocks PowerShell scripts, use the Python command above; no execution-policy change is necessary. There is no installer or standalone executable yet.

1. Choose a folder, then **Scan folder**. Scan and export never move your files.
2. In **Duplicates**, select individual file rows. Press Space or **Toggle selected copies** to mark copies for quarantine. Everything starts as **Keep**.
3. Choose **Preview cleanup**, inspect the source/destination table, select the moves to apply, and confirm. At least one copy in each group must stay.
4. In **Organize**, preview moves into Images, Documents, Audio, Video, Archives, and Other beneath the scanned folder. Select which moves to apply. Conflicting names are skipped and explained.
5. Use **History & restore** to inspect operations and restore completed moves. Restoring refuses to overwrite occupied original paths. Rescan after changes.

Cancellation or read errors produce an **Incomplete** scan: you can export it, but cannot use it to plan changes. Scan details explain errors and deliberate skips. Quarantine and history are retained until you manage them yourself.

## Storage and recovery

- History: `.data/history.sqlite3` beside this README by default.
- Quarantine: `.file-organizer-quarantine/<operation-id>/...` under the scanned folder.
- Small `.file-organizer.lock` and database `.lock` files coordinate running instances; leave them in place. The OS releases locks after a crash.
- The app reconciles interrupted moves on startup. Ambiguous paths become **conflict**, requiring manual inspection; it never guesses which file to remove.
- To retain restore capability, preserve the journal, quarantine, and file paths. Back up the journal with the application closed. Relocating the project does not rewrite paths in old history.
- Restore applies only to unchanged moved files. Changed content/metadata, missing files, or occupied destinations are reported. A failed restore can be retried after an ordinary destination conflict is resolved.

**Quarantine does not free disk space.** The report estimates logical duplicate bytes you could eventually reclaim; this app intentionally provides no permanent-delete/purge feature. Compression, sparse files, and filesystem allocation can make physical savings differ.

The app skips symlinks, junctions/reparse points, `.git`, `.venv`, `__pycache__`, `.data`, quarantine, and managed lock files. Hard-link aliases count once; files with multiple hard links cannot be moved. Moves stay on one volume and are never silently converted into copy/delete operations. Close programs editing the target files before applying a plan: rechecking files reduces stale-result risks but cannot make filesystem operations atomic against all external changes.

## CLI examples

These example paths should be replaced with your own test folder. Commands without `--apply` only preview changes.

```powershell
python -m file_organizer scan "C:\MyTestFiles" --json report.json --csv report.csv
python -m file_organizer cleanup "C:\MyTestFiles" --remove "C:\MyTestFiles\copy.txt"
python -m file_organizer cleanup "C:\MyTestFiles" --remove "C:\MyTestFiles\copy.txt" --apply
python -m file_organizer organize "C:\MyTestFiles"
python -m file_organizer organize "C:\MyTestFiles" --apply
python -m file_organizer history
python -m file_organizer restore OPERATION_ID
python -m file_organizer restore OPERATION_ID --apply
```

Repeat `--remove` to select more copies. Use a global `--db "C:\path\history.sqlite3"` before the subcommand to select a different journal. Report export refuses existing paths; choose a new filename. JSON contains full scan data; CSV lists duplicate files, includes the completeness flag, and escapes spreadsheet formula prefixes.

Exit codes: `0` success/preview, `2` invalid input or incomplete/failed action, `130` user interruption, `1` unexpected error.

## Development and tests

Create a local development environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q --cov --cov-report=term-missing
```

Tests create temporary fixtures, exercise real SQLite/filesystem operations, inject crash/read failures, and test Tk workflows. They do not clean up personal folders. Tk tests require a working Tk installation; the native desktop screenshot check is recorded separately. Coverage excludes widget presentation and the tiny entrypoint, while desktop behavior is exercised by integration tests.

## Development workflow

Built through requirements, architecture, interface design, and test-driven implementation. The workflow was adapted from [WorldFlowAI/everything-claude-code](https://github.com/WorldFlowAI/everything-claude-code). Planning and learning documents are kept locally and are not included in this repository.
