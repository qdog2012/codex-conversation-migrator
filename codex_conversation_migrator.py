#!/usr/bin/env python3
"""
Export local Codex conversations from one machine and import them into another
machine under a chosen model_provider value.

Examples:
    # Same machine: rewrite all local conversations from "crs" to "openai".
    python codex_conversation_migrator.py migrate crs openai
    # Backward-compatible shorthand:
    python codex_conversation_migrator.py crs openai

    # Source machine: export all local conversations from all providers.
    # Project/workspace directories are included by default.
    python codex_conversation_migrator.py export --output codex-conversations.zip
    # Skip packaging project/workspace directories.
    python codex_conversation_migrator.py export --output codex-conversations.zip --skip-workspaces

    # Target machine: import the package and make all imported conversations
    # visible under the "openai" provider. Duplicate thread ids are merged by
    # default; use --overwrite to replace or --skip-existing to skip them.
    python codex_conversation_migrator.py import codex-conversations.zip openai
    # Choose where packaged project/workspace directories are restored.
    python codex_conversation_migrator.py import codex-conversations.zip openai --restore-workspaces-to D:\\CodexImportedProjects

    # Repair local Codex sidebar/project history indexes without changing providers.
    python codex_conversation_migrator.py repair-indexes

The script uses CODEX_HOME when set; otherwise it uses ~/.codex.
Close Codex App before export/import/migration/repair so it does not overwrite
the repaired UI state when it exits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_FORMAT = 2
SESSION_DIRS = ("sessions", "archived_sessions")
BACKUP_FILES = (
    "state_5.sqlite",
    "goals_1.sqlite",
    "session_index.jsonl",
    ".codex-global-state.json",
    "config.toml",
)
WORKSPACE_MANIFEST = "workspace_manifest.json"
LOCAL_THREAD_KEY_PREFIX = "local:"
PROJECTLESS_THREAD_IDS_KEY = "projectless-thread-ids"
THREAD_WORKSPACE_ROOT_HINTS_KEY = "thread-workspace-root-hints"
THREAD_PROJECTLESS_OUTPUT_DIRECTORIES_KEY = "thread-projectless-output-directories"
THREAD_PROJECT_ASSIGNMENTS_KEY = "thread-project-assignments"
SIDEBAR_PROJECT_THREAD_ORDERS_KEY = "sidebar-project-thread-orders"
WORKSPACE_ROOT_OPTIONS_KEY = "electron-saved-workspace-roots"
PROJECT_ORDER_KEY = "project-order"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "value"


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def codex_home(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def as_db_path(path: Path) -> str:
    resolved = str(path.resolve())
    if os.name == "nt" and not resolved.startswith("\\\\?\\"):
        return "\\\\?\\" + resolved
    return resolved


def as_global_state_path(path: Path) -> str:
    return str(path.resolve())


def path_from_db(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value).expanduser()


def read_first_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline()
        obj = json.loads(line)
        return obj if isinstance(obj, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def read_session_meta(path: Path) -> dict[str, Any] | None:
    obj = read_first_json(path)
    if not obj or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else None


def write_session_provider(
    src: Path, dst: Path, provider: str, cwd: Path | None = None
) -> bool:
    try:
        with src.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            rest = handle.read()
        obj = json.loads(first_line)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False

    payload = obj.get("payload")
    if obj.get("type") == "session_meta" and isinstance(payload, dict):
        payload["model_provider"] = provider
        if cwd is not None:
            payload["cwd"] = as_db_path(cwd)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.write(rest)

    stat = src.stat()
    os.utime(dst, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    return True


def canonical_event_line(line: str) -> str:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip("\n")
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_session_provider(
    src: Path, dst: Path, provider: str, cwd: Path | None = None
) -> tuple[bool, int]:
    """Merge src into existing dst and rewrite the session_meta provider.

    Returns (changed, appended_line_count). If dst does not exist, this behaves
    like a copy with provider rewrite.
    """
    if not dst.exists():
        return (write_session_provider(src, dst, provider, cwd), 0)

    try:
        src_lines = src.read_text(encoding="utf-8").splitlines()
        dst_lines = dst.read_text(encoding="utf-8").splitlines()
        src_obj = json.loads(src_lines[0]) if src_lines else {}
        dst_obj = json.loads(dst_lines[0]) if dst_lines else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return (write_session_provider(src, dst, provider, cwd), 0)

    if not src_lines:
        return (False, 0)

    if not dst_lines:
        return (write_session_provider(src, dst, provider, cwd), 0)

    first_obj = dst_obj if isinstance(dst_obj, dict) else src_obj
    payload = first_obj.get("payload")
    existing_provider = payload.get("model_provider") if isinstance(payload, dict) else None
    existing_cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if first_obj.get("type") == "session_meta" and isinstance(payload, dict):
        payload["model_provider"] = provider
        if cwd is not None:
            payload["cwd"] = as_db_path(cwd)

    body = list(dst_lines[1:])
    seen = {canonical_event_line(line) for line in body}
    appended = 0
    for line in src_lines[1:]:
        key = canonical_event_line(line)
        if key not in seen:
            body.append(line)
            seen.add(key)
            appended += 1

    stat_src = src.stat()
    stat_dst = dst.stat()
    changed = appended > 0 or existing_provider != provider
    if cwd is not None:
        changed = changed or existing_cwd != as_db_path(cwd)
    if changed:
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(first_obj, ensure_ascii=False, separators=(",", ":"))
        if body:
            text += "\n" + "\n".join(body)
        text += "\n"
        dst.write_text(text, encoding="utf-8", newline="")

    mtime_ns = max(stat_src.st_mtime_ns, stat_dst.st_mtime_ns)
    atime_ns = max(stat_src.st_atime_ns, stat_dst.st_atime_ns)
    os.utime(dst, ns=(atime_ns, mtime_ns))
    return (changed, appended)


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return (
        con.execute(
            "select 1 from sqlite_master where type='table' and name=?", (table,)
        ).fetchone()
        is not None
    )


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'pragma table_info("{table}")')]


def sqlite_backup(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(dst)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    return True


def copy_tree_if_exists(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    count = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            count += 1
    return count


def provider_counts_from_state(db: Path) -> dict[str, int]:
    if not db.exists():
        return {}
    con = connect_ro(db)
    try:
        if not table_exists(con, "threads"):
            return {}
        return {
            str(provider): int(count)
            for provider, count in con.execute(
                "select coalesce(model_provider, ''), count(*) "
                "from threads group by coalesce(model_provider, '')"
            )
        }
    finally:
        con.close()


def provider_counts_from_jsonl(base: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for dirname in SESSION_DIRS:
        folder = base / dirname
        if not folder.exists():
            continue
        for path in folder.rglob("*.jsonl"):
            meta = read_session_meta(path)
            provider = str((meta or {}).get("model_provider", ""))
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def collect_session_files(base: Path) -> dict[str, tuple[str, Path]]:
    sessions: dict[str, tuple[str, Path]] = {}
    for dirname in SESSION_DIRS:
        folder = base / dirname
        if not folder.exists():
            continue
        for path in folder.rglob("*.jsonl"):
            meta = read_session_meta(path)
            thread_id = meta.get("id") if meta else None
            if isinstance(thread_id, str) and thread_id:
                sessions[thread_id] = (dirname, path)
    return sessions


def workspace_id_for_path(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).lower().encode("utf-8")).hexdigest()
    return digest[:16]


def collect_thread_cwds(base: Path) -> dict[str, Path]:
    db = base / "state_5.sqlite"
    if not db.exists():
        return {}

    con = connect_ro(db)
    try:
        if not table_exists(con, "threads"):
            return {}
        result: dict[str, Path] = {}
        for thread_id, cwd in con.execute("select id, cwd from threads"):
            path = path_from_db(cwd)
            if isinstance(thread_id, str) and path is not None and path.is_dir():
                result[thread_id] = path.resolve()
        return result
    finally:
        con.close()


def collect_thread_cwds_for_ids(base: Path, thread_ids: set[str]) -> dict[str, Path]:
    db = base / "state_5.sqlite"
    if not db.exists() or not thread_ids:
        return {}

    con = connect_ro(db)
    try:
        if not table_exists(con, "threads"):
            return {}
        result: dict[str, Path] = {}
        ids = list(thread_ids)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            rows = con.execute(
                "select id, cwd from threads where id in "
                f"({','.join('?' for _ in chunk)})",
                tuple(chunk),
            )
            for thread_id, cwd in rows:
                path = path_from_db(cwd)
                if isinstance(thread_id, str) and path is not None:
                    result[thread_id] = path
        return result
    finally:
        con.close()


def export_workspaces(base: Path, stage: Path) -> tuple[list[dict[str, Any]], int]:
    thread_cwds = collect_thread_cwds(base)
    by_workspace: dict[Path, list[str]] = {}
    for thread_id, cwd in thread_cwds.items():
        by_workspace.setdefault(cwd, []).append(thread_id)

    entries: list[dict[str, Any]] = []
    copied = 0
    workspaces_dir = stage / "workspaces"
    for cwd, thread_ids in sorted(by_workspace.items(), key=lambda item: str(item[0])):
        workspace_id = workspace_id_for_path(cwd)
        target_dir_name = f"{safe_name(cwd.name)}-{workspace_id}"
        package_path = Path("workspaces") / target_dir_name
        dst = stage / package_path
        shutil.copytree(cwd, dst)
        copied += 1
        entries.append(
            {
                "workspace_id": workspace_id,
                "source_path": str(cwd),
                "package_path": package_path.as_posix(),
                "target_dir_name": target_dir_name,
                "thread_ids": sorted(thread_ids),
            }
        )

    if copied == 0 and workspaces_dir.exists():
        workspaces_dir.rmdir()

    if entries:
        (stage / WORKSPACE_MANIFEST).write_text(
            json.dumps({"workspaces": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return entries, copied


def load_workspace_entries(package: Path) -> list[dict[str, Any]]:
    manifest = load_json_file(package / WORKSPACE_MANIFEST)
    entries = manifest.get("workspaces")
    return entries if isinstance(entries, list) else []


def default_workspace_restore_root(base: Path, package_zip: Path) -> Path:
    return base / "imported_workspaces" / safe_name(package_zip.stem)


def make_target_backup(base: Path, label: str) -> Path:
    backup_dir = base / "migration_backups" / f"{label}-{now_stamp()}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    for dirname in SESSION_DIRS:
        copy_tree_if_exists(base / dirname, backup_dir / dirname)

    for name in BACKUP_FILES:
        path = base / name
        if name.endswith(".sqlite"):
            sqlite_backup(path, backup_dir / name)
        elif path.exists():
            if path.is_file():
                shutil.copy2(path, backup_dir / name)
            elif path.is_dir():
                copy_tree_if_exists(path, backup_dir / name)

    shutil.make_archive(str(backup_dir), "zip", root_dir=backup_dir)
    return backup_dir


def create_export(args: argparse.Namespace) -> int:
    base = codex_home(args.codex_home)
    if not base.exists():
        print(f"Codex home does not exist: {base}", file=sys.stderr)
        return 2

    output = Path(args.output).expanduser().resolve() if args.output else None
    if output is None:
        output = Path.cwd() / f"codex-conversations-{now_stamp()}.zip"
    if output.suffix.lower() != ".zip":
        output = output.with_suffix(".zip")
    output.parent.mkdir(parents=True, exist_ok=True)

    include_workspaces = not args.skip_workspaces
    if include_workspaces:
        print(
            "WARNING: project/workspace directories are included by default and "
            "are copied with no exclusion rules. Review for secrets, large files, "
            "build outputs, dependencies, and private data before sharing the "
            "package. Use --skip-workspaces to export conversations only."
        )

    with tempfile.TemporaryDirectory(prefix="codex-export-") as temp_name:
        stage = Path(temp_name) / "package"
        stage.mkdir(parents=True)

        copied_files = 0
        for dirname in SESSION_DIRS:
            copied_files += copy_tree_if_exists(base / dirname, stage / dirname)

        sqlite_backup(base / "state_5.sqlite", stage / "state_5.sqlite")
        sqlite_backup(base / "goals_1.sqlite", stage / "goals_1.sqlite")

        for name in ("session_index.jsonl", ".codex-global-state.json", "config.toml"):
            path = base / name
            if path.exists() and path.is_file():
                shutil.copy2(path, stage / name)

        workspace_entries: list[dict[str, Any]] = []
        workspace_count = 0
        if include_workspaces:
            workspace_entries, workspace_count = export_workspaces(base, stage)

        manifest = {
            "format": PACKAGE_FORMAT,
            "created_at": utc_now_iso(),
            "source_codex_home": str(base),
            "session_file_count": copied_files,
            "include_workspaces": bool(include_workspaces),
            "workspace_count": workspace_count,
            "provider_counts_from_state": provider_counts_from_state(
                stage / "state_5.sqlite"
            ),
            "provider_counts_from_jsonl": provider_counts_from_jsonl(stage),
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        if output.exists():
            output.unlink()
        shutil.make_archive(str(output.with_suffix("")), "zip", root_dir=stage)

    print(f"Exported: {output}")
    if include_workspaces:
        print(f"Project directories included: {len(workspace_entries)}")
    print("Providers in state DB:")
    for provider, count in provider_counts_from_state_from_zip(output).items():
        print(f"  {provider or '<empty>'}: {count}")
    print("Transfer this zip to the target machine, then run the import command.")
    return 0


def provider_counts_from_state_from_zip(zip_path: Path) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="codex-export-check-") as temp_name:
        with zipfile.ZipFile(zip_path) as archive:
            if "state_5.sqlite" not in archive.namelist():
                return {}
            archive.extract("state_5.sqlite", temp_name)
        return provider_counts_from_state(Path(temp_name) / "state_5.sqlite")


def migrate_local_sqlite(base: Path, old_provider: str, new_provider: str) -> tuple[int, int, int]:
    state_db = base / "state_5.sqlite"
    if not state_db.exists():
        raise FileNotFoundError(f"Missing Codex state DB: {state_db}")

    con = sqlite3.connect(state_db)
    try:
        before = con.execute(
            "select count(*) from threads where model_provider = ?", (old_provider,)
        ).fetchone()[0]
        con.execute(
            "update threads set model_provider = ? where model_provider = ?",
            (new_provider, old_provider),
        )
        con.commit()
        old_after = con.execute(
            "select count(*) from threads where model_provider = ?", (old_provider,)
        ).fetchone()[0]
        new_after = con.execute(
            "select count(*) from threads where model_provider = ?", (new_provider,)
        ).fetchone()[0]
    finally:
        con.close()

    return before, old_after, new_after


def migrate_local_jsonl(base: Path, old_provider: str, new_provider: str) -> tuple[int, int, int]:
    checked = 0
    changed = 0
    skipped = 0

    for dirname in SESSION_DIRS:
        folder = base / dirname
        if not folder.exists():
            continue
        for path in folder.rglob("*.jsonl"):
            checked += 1
            stat = path.stat()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                obj = json.loads(lines[0]) if lines else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                skipped += 1
                continue

            payload = obj.get("payload")
            if (
                obj.get("type") == "session_meta"
                and isinstance(payload, dict)
                and payload.get("model_provider") == old_provider
            ):
                payload["model_provider"] = new_provider
                text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                if len(lines) > 1:
                    text += "\n" + "\n".join(lines[1:])
                text += "\n"
                path.write_text(text, encoding="utf-8", newline="")
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                changed += 1

    return checked, changed, skipped


def migrate_local_provider(args: argparse.Namespace) -> int:
    if args.old_provider == args.new_provider:
        print("old_provider and new_provider are the same; nothing to do.", file=sys.stderr)
        return 2

    base = codex_home(args.codex_home)
    if not base.exists():
        print(f"Codex home does not exist: {base}", file=sys.stderr)
        return 2

    backup_dir = make_target_backup(
        base,
        f"provider-{safe_name(args.old_provider)}-to-{safe_name(args.new_provider)}",
    )
    db_before, db_old_after, db_new_after = migrate_local_sqlite(
        base, args.old_provider, args.new_provider
    )
    jsonl_checked, jsonl_changed, jsonl_skipped = migrate_local_jsonl(
        base, args.old_provider, args.new_provider
    )
    config_changed = False
    if not args.no_set_config:
        config_changed = set_config_provider(base, args.new_provider)
    index_repairs = repair_project_history_indexes(base)

    jsonl_counts = provider_counts_from_jsonl(base)
    db_counts = provider_counts_from_state(base / "state_5.sqlite")

    print(f"Backup: {backup_dir}")
    print(f"Backup zip: {backup_dir}.zip")
    print()
    print("Local provider migration complete.")
    print(f"Old provider: {args.old_provider}")
    print(f"New provider: {args.new_provider}")
    print(f"DB rows changed from old provider: {db_before}")
    print(f"DB old provider rows after: {db_old_after}")
    print(f"DB new provider rows after: {db_new_after}")
    print(f"JSONL checked: {jsonl_checked}")
    print(f"JSONL changed: {jsonl_changed}")
    print(f"JSONL skipped/unreadable: {jsonl_skipped}")
    print(f"Project history index entries repaired: {index_repairs}")
    print(f"config.toml set to new provider: {config_changed}")
    print(f"DB provider counts: {db_counts}")
    print(f"JSONL provider counts: {jsonl_counts}")
    print()
    print("Restart Codex App after migration.")
    return 0


def repair_indexes_command(args: argparse.Namespace) -> int:
    base = codex_home(args.codex_home)
    if not base.exists():
        print(f"Codex home does not exist: {base}", file=sys.stderr)
        return 2

    backup_dir = make_target_backup(base, "repair-indexes")
    changed = repair_project_history_indexes(base)

    print(f"Backup: {backup_dir}")
    print(f"Backup zip: {backup_dir}.zip")
    print()
    print("Project history index repair complete.")
    print(f"Codex home: {base}")
    print(f"Global state entries repaired: {changed}")
    print()
    print("Restart Codex App after repair.")
    return 0


def extract_package(zip_path: Path, dst: Path) -> Path:
    if not zip_path.exists():
        raise FileNotFoundError(f"Package not found: {zip_path}")
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dst)

    manifest = dst / "manifest.json"
    if manifest.exists():
        return dst

    # Tolerate zip files that contain one top-level package directory.
    children = [p for p in dst.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "manifest.json").exists():
        return children[0]

    raise FileNotFoundError("Package manifest.json not found")


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def comparable_time(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def local_thread_key(thread_id: str) -> str:
    return f"{LOCAL_THREAD_KEY_PREFIX}{thread_id}"


def normalize_global_path(path: Path) -> str:
    return as_global_state_path(path)


def comparable_path_key(value: str | Path) -> str:
    text = str(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text.replace("\\", "/").rstrip("/").lower()


def path_is_under(path: Path, root: str) -> bool:
    path_key = comparable_path_key(path)
    root_key = comparable_path_key(root)
    return path_key == root_key or path_key.startswith(root_key + "/")


def get_state_list(state: dict[str, Any], key: str) -> list[Any]:
    value = state.setdefault(key, [])
    if not isinstance(value, list):
        value = []
        state[key] = value
    return value


def get_state_dict(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.setdefault(key, {})
    if not isinstance(value, dict):
        value = {}
        state[key] = value
    return value


def state_project_roots(state: dict[str, Any]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for key in (PROJECT_ORDER_KEY, WORKSPACE_ROOT_OPTIONS_KEY):
        value = state.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, str) or not item:
                continue
            normalized = comparable_path_key(item)
            if normalized not in seen:
                roots.append(item)
                seen.add(normalized)
    return roots


def choose_project_root(cwd: Path, roots: list[str]) -> str:
    matches = [root for root in roots if path_is_under(cwd, root)]
    if matches:
        return max(matches, key=lambda item: len(comparable_path_key(item)))
    return normalize_global_path(cwd)


def default_projectless_output_dir(cwd: Path) -> Path:
    return cwd / "outputs"


def add_unique_path(state: dict[str, Any], key: str, path_value: str) -> int:
    values = get_state_list(state, key)
    path_key = comparable_path_key(path_value)
    if any(isinstance(item, str) and comparable_path_key(item) == path_key for item in values):
        return 0
    values.append(path_value)
    return 1


def collect_thread_project_records(
    base: Path, thread_ids: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    db = base / "state_5.sqlite"
    if not db.exists():
        return {}

    con = connect_ro(db)
    try:
        if not table_exists(con, "threads"):
            return {}
        cols = table_columns(con, "threads")
        wanted = ["id", "cwd", "updated_at", "updated_at_ms", "archived"]
        select_cols = [col for col in wanted if col in cols]
        if "id" not in select_cols or "cwd" not in select_cols:
            return {}

        records: dict[str, dict[str, Any]] = {}
        if thread_ids:
            ids = list(thread_ids)
            chunks = [ids[start : start + 500] for start in range(0, len(ids), 500)]
            where_prefix = f"where id in "
        else:
            chunks = [[]]
            where_prefix = ""

        quoted_cols = ",".join(f'"{col}"' for col in select_cols)
        for chunk in chunks:
            if chunk:
                sql = f"select {quoted_cols} from threads {where_prefix}({','.join('?' for _ in chunk)})"
                params: tuple[Any, ...] = tuple(chunk)
            else:
                sql = f"select {quoted_cols} from threads"
                params = ()
            for row in con.execute(sql, params):
                data = dict(zip(select_cols, row))
                if data.get("archived") not in (None, 0, False):
                    continue
                thread_id = data.get("id")
                cwd = path_from_db(data.get("cwd"))
                if not isinstance(thread_id, str) or cwd is None:
                    continue
                updated = data.get("updated_at_ms")
                if updated is None:
                    updated = data.get("updated_at")
                records[thread_id] = {"cwd": cwd, "updated": comparable_time(updated)}
        return records
    finally:
        con.close()


def repair_project_history_state(
    state: dict[str, Any],
    thread_records: dict[str, dict[str, Any]],
    projectless_ids: set[str] | None = None,
) -> int:
    if not thread_records:
        return 0

    changed = 0
    projectless_ids = projectless_ids or set()

    dst_projectless = get_state_list(state, PROJECTLESS_THREAD_IDS_KEY)
    filtered_projectless = [
        item
        for item in dst_projectless
        if not (
            isinstance(item, str)
            and item in thread_records
            and item not in projectless_ids
        )
    ]
    if filtered_projectless != dst_projectless:
        state[PROJECTLESS_THREAD_IDS_KEY] = dst_projectless = filtered_projectless
        changed += 1
    seen_projectless = set(item for item in dst_projectless if isinstance(item, str))
    for thread_id in sorted(projectless_ids):
        if thread_id in thread_records and thread_id not in seen_projectless:
            dst_projectless.append(thread_id)
            seen_projectless.add(thread_id)
            changed += 1

    roots = state_project_roots(state)
    assignments = get_state_dict(state, THREAD_PROJECT_ASSIGNMENTS_KEY)
    orders = get_state_dict(state, SIDEBAR_PROJECT_THREAD_ORDERS_KEY)
    hints = get_state_dict(state, THREAD_WORKSPACE_ROOT_HINTS_KEY)
    output_dirs = get_state_dict(state, THREAD_PROJECTLESS_OUTPUT_DIRECTORIES_KEY)

    project_threads: dict[str, list[tuple[str, float]]] = {}
    for thread_id, record in thread_records.items():
        cwd = record["cwd"]
        if thread_id in projectless_ids:
            if thread_id not in hints:
                hints[thread_id] = normalize_global_path(cwd)
                changed += 1
            if thread_id not in output_dirs:
                output_dirs[thread_id] = normalize_global_path(
                    default_projectless_output_dir(cwd)
                )
                changed += 1
            continue

        project_root = choose_project_root(cwd, roots)
        changed += add_unique_path(state, WORKSPACE_ROOT_OPTIONS_KEY, project_root)
        changed += add_unique_path(state, PROJECT_ORDER_KEY, project_root)
        roots = state_project_roots(state)

        assignment = {"projectId": project_root, "projectKind": "local"}
        if assignments.get(thread_id) != assignment:
            assignments[thread_id] = assignment
            changed += 1

        key = local_thread_key(thread_id)
        project_threads.setdefault(project_root, []).append((key, record["updated"]))

    for project_root, keyed_threads in project_threads.items():
        keyed_threads.sort(key=lambda item: item[1], reverse=True)
        existing = orders.get(project_root)
        if not isinstance(existing, list):
            existing = []
        existing_strings = [item for item in existing if isinstance(item, str)]
        seen = set(existing_strings)
        additions = [key for key, _ in keyed_threads if key not in seen]
        if additions or existing_strings != existing:
            orders[project_root] = existing_strings + additions
            changed += 1

    return changed


def repair_project_history_indexes(base: Path, thread_ids: set[str] | None = None) -> int:
    state_path = base / ".codex-global-state.json"
    state = load_json_file(state_path)
    records = collect_thread_project_records(base, thread_ids)
    projectless_value = state.get(PROJECTLESS_THREAD_IDS_KEY)
    projectless_ids = (
        {item for item in projectless_value if isinstance(item, str)}
        if isinstance(projectless_value, list)
        else set()
    )
    changed = repair_project_history_state(state, records, projectless_ids)
    if changed:
        save_json_file(state_path, state)
    return changed


def append_or_merge_session_index(
    target_index: Path, source_index: Path, imported_ids: set[str], mode: str
) -> int:
    if not source_index.exists() or not imported_ids:
        return 0

    order: list[str] = []
    existing_by_id: dict[str, tuple[dict[str, Any], str]] = {}
    passthrough_lines: list[str] = []
    if target_index.exists():
        for line in target_index.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                thread_id = obj.get("id")
            except json.JSONDecodeError:
                thread_id = None
            if isinstance(thread_id, str):
                if thread_id not in existing_by_id:
                    order.append(thread_id)
                existing_by_id[thread_id] = (obj, line)
            else:
                passthrough_lines.append(line)

    changed = 0
    for line in source_index.read_text(encoding="utf-8").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = obj.get("id")
        if not isinstance(thread_id, str) or thread_id not in imported_ids:
            continue

        if thread_id not in existing_by_id:
            order.append(thread_id)
            existing_by_id[thread_id] = (obj, line)
            changed += 1
            continue

        if mode == "overwrite":
            existing_by_id[thread_id] = (obj, line)
            changed += 1
        elif mode == "merge":
            current_obj = existing_by_id[thread_id][0]
            if comparable_time(obj.get("updated_at")) > comparable_time(
                current_obj.get("updated_at")
            ):
                existing_by_id[thread_id] = (obj, line)
                changed += 1

    lines = passthrough_lines + [existing_by_id[thread_id][1] for thread_id in order]
    target_index.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return changed


def merge_global_state(
    base: Path,
    package: Path,
    imported_ids: set[str],
    id_to_cwd_path: dict[str, Path] | None = None,
) -> int:
    if not imported_ids:
        return 0

    src = load_json_file(package / ".codex-global-state.json")
    dst_path = base / ".codex-global-state.json"
    dst = load_json_file(dst_path)
    changed = 0

    src_projectless_value = src.get(PROJECTLESS_THREAD_IDS_KEY)
    src_projectless_ids = (
        {
            thread_id
            for thread_id in src_projectless_value
            if isinstance(thread_id, str) and thread_id in imported_ids
        }
        if isinstance(src_projectless_value, list)
        else set()
    )

    # Pinned threads are optional UI state, so only copy them when the source
    # explicitly had them pinned.
    for key in ("pinned-thread-ids",):
        src_list = src.get(key)
        if not isinstance(src_list, list):
            continue
        dst_list = dst.setdefault(key, [])
        if not isinstance(dst_list, list):
            dst[key] = dst_list = []
        seen = set(x for x in dst_list if isinstance(x, str))
        for thread_id in src_list:
            if isinstance(thread_id, str) and thread_id in imported_ids and thread_id not in seen:
                dst_list.append(thread_id)
                seen.add(thread_id)
                changed += 1

    db_cwds = collect_thread_cwds_for_ids(base, imported_ids)
    for key in (
        THREAD_WORKSPACE_ROOT_HINTS_KEY,
        THREAD_PROJECTLESS_OUTPUT_DIRECTORIES_KEY,
    ):
        src_map = src.get(key)
        if not isinstance(src_map, dict):
            src_map = {}
        dst_map = dst.setdefault(key, {})
        if not isinstance(dst_map, dict):
            dst[key] = dst_map = {}
        for thread_id in imported_ids:
            if thread_id not in src_map and key != THREAD_WORKSPACE_ROOT_HINTS_KEY:
                continue
            value = src_map.get(thread_id)
            if (
                key == THREAD_WORKSPACE_ROOT_HINTS_KEY
                and id_to_cwd_path
                and thread_id in id_to_cwd_path
            ):
                value = as_global_state_path(id_to_cwd_path[thread_id])
            elif key == THREAD_WORKSPACE_ROOT_HINTS_KEY and thread_id in db_cwds:
                value = as_global_state_path(db_cwds[thread_id])
            if value is not None and dst_map.get(thread_id) != value:
                dst_map[thread_id] = value
                changed += 1

    thread_records = collect_thread_project_records(base, imported_ids)
    changed += repair_project_history_state(dst, thread_records, src_projectless_ids)

    if changed:
        save_json_file(dst_path, dst)

    return changed


def upsert_rows(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    table: str,
    id_filter_column: str,
    ids: set[str],
    overwrite: bool,
    extra_values: dict[str, Any] | None = None,
) -> int:
    if not ids or not table_exists(source, table) or not table_exists(target, table):
        return 0

    src_cols = table_columns(source, table)
    dst_cols = table_columns(target, table)
    cols = [col for col in src_cols if col in dst_cols]
    if id_filter_column not in cols:
        return 0

    extra_values = extra_values or {}
    placeholders = ",".join("?" for _ in cols)
    quoted_cols = ",".join(f'"{col}"' for col in cols)
    verb = "insert or replace" if overwrite else "insert or ignore"
    insert_sql = f'{verb} into "{table}" ({quoted_cols}) values ({placeholders})'

    select_sql = (
        f'select {quoted_cols} from "{table}" '
        f'where "{id_filter_column}" in ({",".join("?" for _ in ids)})'
    )
    changed = 0
    for row in source.execute(select_sql, tuple(ids)):
        values = list(row)
        for col, value in extra_values.items():
            if col in cols:
                values[cols.index(col)] = value
        target.execute(insert_sql, values)
        changed += 1
    return changed


def delete_rows_for_ids(
    con: sqlite3.Connection, table: str, id_filter_column: str, ids: set[str]
) -> int:
    if not ids or not table_exists(con, table):
        return 0
    before = con.total_changes
    con.execute(
        f'delete from "{table}" where "{id_filter_column}" in '
        f'({",".join("?" for _ in ids)})',
        tuple(ids),
    )
    return con.total_changes - before


def row_updated_value(row: list[Any], cols: list[str]) -> float:
    if "updated_at_ms" in cols:
        value = row[cols.index("updated_at_ms")]
        if value is not None:
            return comparable_time(value)
    if "updated_at" in cols:
        return comparable_time(row[cols.index("updated_at")])
    return 0.0


def import_threads(
    target_db: Path,
    source_db: Path,
    imported_ids: set[str],
    id_to_rollout_path: dict[str, Path],
    id_to_cwd_path: dict[str, Path],
    target_provider: str,
    mode: str,
) -> tuple[int, int, int]:
    if not imported_ids:
        return (0, 0, 0)

    target = sqlite3.connect(target_db)
    source = sqlite3.connect(source_db)
    try:
        if not table_exists(source, "threads") or not table_exists(target, "threads"):
            raise RuntimeError("threads table missing in source or target state DB")

        src_cols = table_columns(source, "threads")
        dst_cols = table_columns(target, "threads")
        cols = [col for col in src_cols if col in dst_cols]
        if "id" not in cols:
            raise RuntimeError("threads.id column missing")

        placeholders = ",".join("?" for _ in cols)
        quoted_cols = ",".join(f'"{col}"' for col in cols)
        insert_sql = f'insert or replace into "threads" ({quoted_cols}) values ({placeholders})'
        select_sql = (
            f'select {quoted_cols} from "threads" '
            f'where "id" in ({",".join("?" for _ in imported_ids)})'
        )
        target_select_sql = f'select {quoted_cols} from "threads" where "id" = ?'

        thread_rows = 0
        for row in source.execute(select_sql, tuple(imported_ids)):
            values = list(row)
            thread_id = values[cols.index("id")]
            current = target.execute(target_select_sql, (thread_id,)).fetchone()
            if mode == "skip" and current is not None:
                continue
            if mode == "merge" and current is not None:
                current_values = list(current)
                if row_updated_value(current_values, cols) > row_updated_value(values, cols):
                    values = current_values
            if "model_provider" in cols:
                values[cols.index("model_provider")] = target_provider
            if "cwd" in cols and thread_id in id_to_cwd_path:
                values[cols.index("cwd")] = as_db_path(id_to_cwd_path[thread_id])
            if "rollout_path" in cols and thread_id in id_to_rollout_path:
                values[cols.index("rollout_path")] = as_db_path(id_to_rollout_path[thread_id])
            target.execute(insert_sql, values)
            thread_rows += 1

        if mode in {"merge", "overwrite"}:
            delete_rows_for_ids(target, "thread_dynamic_tools", "thread_id", imported_ids)
        dynamic_rows = upsert_rows(
            target,
            source,
            "thread_dynamic_tools",
            "thread_id",
            imported_ids,
            False,
        )

        edge_rows = 0
        if table_exists(source, "thread_spawn_edges") and table_exists(
            target, "thread_spawn_edges"
        ):
            src_cols = table_columns(source, "thread_spawn_edges")
            dst_cols = table_columns(target, "thread_spawn_edges")
            cols = [col for col in src_cols if col in dst_cols]
            if "parent_thread_id" in cols and "child_thread_id" in cols:
                quoted_cols = ",".join(f'"{col}"' for col in cols)
                placeholders = ",".join("?" for _ in cols)
                if mode in {"merge", "overwrite"}:
                    target.execute(
                        'delete from "thread_spawn_edges" where '
                        f'"parent_thread_id" in ({",".join("?" for _ in imported_ids)}) '
                        'or '
                        f'"child_thread_id" in ({",".join("?" for _ in imported_ids)})',
                        tuple(imported_ids) + tuple(imported_ids),
                    )
                verb = "insert or replace" if mode == "overwrite" else "insert or ignore"
                insert_sql = (
                    f'{verb} into "thread_spawn_edges" ({quoted_cols}) '
                    f"values ({placeholders})"
                )
                for row in source.execute(f'select {quoted_cols} from "thread_spawn_edges"'):
                    parent = row[cols.index("parent_thread_id")]
                    child = row[cols.index("child_thread_id")]
                    if parent in imported_ids and child in imported_ids:
                        target.execute(insert_sql, row)
                        edge_rows += 1

        target.commit()
        return (thread_rows, dynamic_rows, edge_rows)
    finally:
        source.close()
        target.close()


def import_goals(
    target_db: Path,
    source_db: Path,
    imported_ids: set[str],
    mode: str,
) -> int:
    if not imported_ids or not target_db.exists() or not source_db.exists():
        return 0
    target = sqlite3.connect(target_db)
    source = sqlite3.connect(source_db)
    try:
        if mode in {"merge", "overwrite"}:
            delete_rows_for_ids(target, "thread_goals", "thread_id", imported_ids)
        rows = upsert_rows(
            target, source, "thread_goals", "thread_id", imported_ids, False
        )
        target.commit()
        return rows
    finally:
        source.close()
        target.close()


def set_config_provider(base: Path, provider: str) -> bool:
    config = base / "config.toml"
    if not config.exists():
        config.write_text(f'model_provider = "{provider}"\n', encoding="utf-8")
        return True

    text = config.read_text(encoding="utf-8")
    pattern = re.compile(r'(?m)^(#\s*)?model_provider\s*=\s*"[^"]*"\s*$')
    replacement = f'model_provider = "{provider}"'
    if pattern.search(text):
        new_text = pattern.sub(replacement, text, count=1)
    else:
        new_text = replacement + "\n" + text

    if new_text != text:
        config.write_text(new_text, encoding="utf-8")
        return True
    return False


def restore_workspace_for_entry(
    package: Path,
    restore_root: Path,
    entry: dict[str, Any],
    restored_cache: dict[str, Path],
) -> Path | None:
    workspace_id = entry.get("workspace_id")
    package_path = entry.get("package_path")
    target_dir_name = entry.get("target_dir_name")
    if not isinstance(workspace_id, str) or not isinstance(package_path, str):
        return None
    if not isinstance(target_dir_name, str) or not target_dir_name:
        target_dir_name = workspace_id
    if workspace_id in restored_cache:
        return restored_cache[workspace_id]

    src = package / package_path
    if not src.is_dir():
        return None

    dst = restore_root / target_dir_name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    restored_cache[workspace_id] = dst.resolve()
    return restored_cache[workspace_id]


def import_package(args: argparse.Namespace) -> int:
    base = codex_home(args.codex_home)
    if not base.exists():
        print(f"Codex home does not exist: {base}", file=sys.stderr)
        return 2

    target_db = base / "state_5.sqlite"
    if not target_db.exists():
        print(
            f"Target state DB does not exist: {target_db}\n"
            "Open Codex App once on the target machine, then close it and retry.",
            file=sys.stderr,
        )
        return 2

    package_zip = Path(args.package).expanduser().resolve()
    if args.overwrite and args.skip_existing:
        print("--overwrite and --skip-existing cannot be used together.", file=sys.stderr)
        return 2
    mode = "overwrite" if args.overwrite else "skip" if args.skip_existing else "merge"

    backup_dir = make_target_backup(
        base, f"import-to-{safe_name(args.target_provider)}"
    )

    with tempfile.TemporaryDirectory(prefix="codex-import-") as temp_name:
        package = extract_package(package_zip, Path(temp_name))
        manifest = load_json_file(package / "manifest.json")
        if manifest.get("format") != PACKAGE_FORMAT:
            print(
                f"Warning: package format is {manifest.get('format')}, "
                f"expected {PACKAGE_FORMAT}. Continuing."
            )

        source_sessions = collect_session_files(package)
        target_sessions = collect_session_files(base)
        target_ids = set(target_sessions)
        workspace_entries = load_workspace_entries(package)
        workspace_by_thread: dict[str, dict[str, Any]] = {}
        for entry in workspace_entries:
            thread_ids = entry.get("thread_ids")
            if not isinstance(thread_ids, list):
                continue
            for thread_id in thread_ids:
                if isinstance(thread_id, str):
                    workspace_by_thread[thread_id] = entry

        workspace_restore_root = (
            Path(args.restore_workspaces_to).expanduser().resolve()
            if args.restore_workspaces_to
            else default_workspace_restore_root(base, package_zip)
        )
        restored_workspaces: dict[str, Path] = {}

        imported_ids: set[str] = set()
        skipped_existing = 0
        copied_files = 0
        merged_files = 0
        overwritten_files = 0
        merged_event_lines = 0
        workspace_restored_threads = 0
        id_to_rollout_path: dict[str, Path] = {}
        id_to_cwd_path: dict[str, Path] = {}

        for thread_id, (dirname, src_path) in source_sessions.items():
            exists = thread_id in target_ids
            if exists and mode == "skip":
                skipped_existing += 1
                continue

            cwd_path = None
            workspace_entry = workspace_by_thread.get(thread_id)
            if workspace_entry is not None:
                cwd_path = restore_workspace_for_entry(
                    package, workspace_restore_root, workspace_entry, restored_workspaces
                )
                if cwd_path is not None:
                    workspace_restored_threads += 1

            if exists:
                dst_path = target_sessions[thread_id][1]
            else:
                rel = src_path.relative_to(package / dirname)
                dst_path = base / dirname / rel

            if mode == "overwrite":
                ok = write_session_provider(
                    src_path, dst_path, args.target_provider, cwd_path
                )
                if exists and ok:
                    overwritten_files += 1
                elif ok:
                    copied_files += 1
            elif mode == "merge" and exists:
                changed, appended = merge_session_provider(
                    src_path, dst_path, args.target_provider, cwd_path
                )
                ok = dst_path.exists()
                if ok:
                    merged_files += 1
                    merged_event_lines += appended
            else:
                ok = write_session_provider(
                    src_path, dst_path, args.target_provider, cwd_path
                )
                if ok:
                    copied_files += 1

            if ok:
                imported_ids.add(thread_id)
                id_to_rollout_path[thread_id] = dst_path
                if cwd_path is not None:
                    id_to_cwd_path[thread_id] = cwd_path

        source_db = package / "state_5.sqlite"
        if not source_db.exists():
            raise FileNotFoundError("Package is missing state_5.sqlite")

        thread_rows, dynamic_rows, edge_rows = import_threads(
            target_db,
            source_db,
            imported_ids,
            id_to_rollout_path,
            id_to_cwd_path,
            args.target_provider,
            mode,
        )

        goal_rows = import_goals(
            base / "goals_1.sqlite",
            package / "goals_1.sqlite",
            imported_ids,
            mode,
        )

        index_rows = append_or_merge_session_index(
            base / "session_index.jsonl",
            package / "session_index.jsonl",
            imported_ids,
            mode,
        )
        global_changes = merge_global_state(
            base, package, imported_ids, id_to_cwd_path
        )

    config_changed = False
    if not args.no_set_config:
        config_changed = set_config_provider(base, args.target_provider)

    print(f"Backup: {backup_dir}")
    print(f"Backup zip: {backup_dir}.zip")
    print()
    print("Import complete.")
    print(f"Package: {package_zip}")
    print(f"Target provider: {args.target_provider}")
    print(f"Import mode: {mode}")
    print(f"New session files copied: {copied_files}")
    print(f"Existing session files merged: {merged_files}")
    print(f"New event lines appended during merge: {merged_event_lines}")
    print(f"Existing session files overwritten: {overwritten_files}")
    print(f"Existing thread IDs skipped: {skipped_existing}")
    print(f"Project directories in package: {len(workspace_entries)}")
    print(f"Project directories restored: {len(restored_workspaces)}")
    print(f"Threads mapped to restored project directories: {workspace_restored_threads}")
    if restored_workspaces:
        print(f"Project restore root: {workspace_restore_root}")
    print(f"Thread rows imported: {thread_rows}")
    print(f"Dynamic tool rows imported: {dynamic_rows}")
    print(f"Spawn edge rows imported: {edge_rows}")
    print(f"Goal rows imported: {goal_rows}")
    print(f"Session index rows appended/replaced: {index_rows}")
    print(f"Global state entries merged/repaired: {global_changes}")
    print(f"config.toml set to provider: {config_changed}")
    print()
    print("Restart Codex App after import.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate, export, and import local Codex conversations across providers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Locally rewrite conversations from one provider value to another.",
    )
    migrate_parser.add_argument("old_provider", help='Existing provider value, e.g. "crs".')
    migrate_parser.add_argument("new_provider", help='New provider value, e.g. "openai".')
    migrate_parser.add_argument(
        "--codex-home",
        help="Target Codex home. Defaults to CODEX_HOME or ~/.codex.",
    )
    migrate_parser.add_argument(
        "--no-set-config",
        action="store_true",
        help="Do not change config.toml model_provider.",
    )
    migrate_parser.set_defaults(func=migrate_local_provider)

    repair_parser = subparsers.add_parser(
        "repair-indexes",
        aliases=["repair"],
        help="Repair Codex sidebar/project history indexes without changing providers.",
    )
    repair_parser.add_argument(
        "--codex-home",
        help="Target Codex home. Defaults to CODEX_HOME or ~/.codex.",
    )
    repair_parser.set_defaults(func=repair_indexes_command)

    export_parser = subparsers.add_parser(
        "export", help="Export all local Codex conversations from all providers."
    )
    export_parser.add_argument(
        "--codex-home",
        help="Source Codex home. Defaults to CODEX_HOME or ~/.codex.",
    )
    export_parser.add_argument(
        "-o",
        "--output",
        help="Output zip path. Defaults to ./codex-conversations-<timestamp>.zip.",
    )
    export_parser.add_argument(
        "--skip-workspaces",
        action="store_true",
        help=(
            "Do not package project/workspace directories. By default they are "
            "included with no exclusion rules."
        ),
    )
    export_parser.add_argument(
        "--include-workspaces",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    export_parser.set_defaults(func=create_export)

    import_parser = subparsers.add_parser(
        "import",
        help="Import a package, merge duplicates, and rewrite imported conversations to one provider.",
    )
    import_parser.add_argument("package", help="Export zip created by this script.")
    import_parser.add_argument(
        "target_provider", help='Provider value to use on the target, e.g. "openai".'
    )
    import_parser.add_argument(
        "--codex-home",
        help="Target Codex home. Defaults to CODEX_HOME or ~/.codex.",
    )
    import_parser.add_argument(
        "--restore-workspaces-to",
        help=(
            "Directory where packaged project/workspace directories should be "
            "restored. Defaults to ~/.codex/imported_workspaces/<package-name>."
        ),
    )
    import_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace target conversations that already have the same thread id.",
    )
    import_parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip target conversations that already have the same thread id. Default is merge.",
    )
    import_parser.add_argument(
        "--no-set-config",
        action="store_true",
        help="Do not change target config.toml model_provider.",
    )
    import_parser.set_defaults(func=import_package)

    return parser


def main() -> int:
    legacy_commands = {"migrate", "export", "import", "repair-indexes", "repair", "-h", "--help"}
    if len(sys.argv) >= 3 and sys.argv[1] not in legacy_commands and not sys.argv[1].startswith("-"):
        sys.argv.insert(1, "migrate")
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
