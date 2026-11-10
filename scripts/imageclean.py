#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["psutil>=5.9"]
# ///
"""Report or remove embedded image payloads from a Codex rollout JSONL file."""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import sys

import psutil
from typing import Any


IMAGE_MARKERS = ("data:image/", "input_image", "image_url")
IMAGE_REMOVED_TEXT = (
    "[image payload removed from local Codex rollout by rollout-session-imageclean; "
    "original rollout is preserved in backup]"
)
DATA_IMAGE_RE = re.compile(r"data:image/[^,\s\"']+,[A-Za-z0-9+/=_%-]+")
DROP = object()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def parse_thread_id(value: str) -> str:
    if value.startswith("codex://threads/"):
        return value.removeprefix("codex://threads/").strip("/")
    return value


def resolve_rollout(thread: str | None, path: str | None, state_db: str | None) -> tuple[str | None, Path]:
    if path:
        return thread, Path(path).expanduser()
    if not thread:
        raise SystemExit("Provide --thread or --path.")

    thread_id = parse_thread_id(thread)
    db = Path(state_db).expanduser() if state_db else codex_home() / "state_5.sqlite"
    if not db.exists():
        raise SystemExit(f"State DB not found: {db}")

    con = sqlite3.connect(str(db))
    try:
        row = con.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise SystemExit(f"Thread not found in {db}: {thread_id}")
    return thread_id, Path(row[0])


def line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def compacted_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def count_payloads(value: Any) -> dict[str, int]:
    counts = {"data_image_values": 0, "input_image_items": 0, "event_image_items": 0}

    def visit(item: Any) -> None:
        if isinstance(item, str):
            counts["data_image_values"] += len(DATA_IMAGE_RE.findall(item))
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        if item.get("type") == "input_image":
            counts["input_image_items"] += 1
        if item.get("type") == "user_message" and isinstance(item.get("images"), list):
            counts["event_image_items"] += len(item["images"])
        for child in item.values():
            visit(child)

    visit(value)
    return counts


def add_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] += value


def clean_value(value: Any, stats: dict[str, int], *, content_list: bool = False) -> Any:
    if isinstance(value, str):
        cleaned, replacements = DATA_IMAGE_RE.subn(IMAGE_REMOVED_TEXT, value)
        stats["data_uris_removed"] += replacements
        return cleaned

    if isinstance(value, list):
        result = []
        changed_content = False
        for item in value:
            if isinstance(item, dict) and item.get("type") == "input_image":
                stats["removed_input_image"] += 1
                changed_content = True
                continue
            if (
                content_list
                and isinstance(item, dict)
                and item.get("type") in {"input_text", "text"}
                and item.get("text") in {"<image>", "</image>"}
            ):
                stats["removed_image_text_tags"] += 1
                changed_content = True
                continue
            cleaned = clean_value(item, stats)
            if cleaned is not DROP:
                result.append(cleaned)
        if content_list and changed_content:
            result.append({"type": "input_text", "text": IMAGE_REMOVED_TEXT})
        return result

    if not isinstance(value, dict):
        return value
    if value.get("type") == "input_image":
        stats["removed_input_image"] += 1
        return DROP

    result = {}
    for key, item in value.items():
        if key == "images" and value.get("type") == "user_message" and isinstance(item, list) and item:
            stats["cleared_event_images"] += len(item)
            result[key] = []
            continue
        cleaned = clean_value(item, stats, content_list=(key == "content" and isinstance(item, list)))
        if cleaned is not DROP:
            result[key] = cleaned
    return result


def analyze_rollout(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
        "line_count": 0,
        "type_counts": {},
        "active_payload_counts": {"data_image_values": 0, "input_image_items": 0, "event_image_items": 0},
        "active_raw_marker_counts": {marker: 0 for marker in IMAGE_MARKERS},
        "compacted_raw_marker_counts": {marker: 0 for marker in IMAGE_MARKERS},
        "active_image_related_lines": 0,
        "compacted_image_related_lines": 0,
        "compacted_line_count": 0,
        "compacted_bytes_preserved": 0,
        "estimated_cleaned_size": None,
    }
    if not path.exists():
        return report

    estimated_size = 0
    dry_stats = new_clean_stats()
    with path.open("r", encoding="utf-8", newline="") as source:
        for line_no, line in enumerate(source, 1):
            report["line_count"] = line_no
            obj = json.loads(line)
            typ = obj.get("type", "?")
            report["type_counts"][typ] = report["type_counts"].get(typ, 0) + 1
            if typ == "compacted":
                report["compacted_line_count"] += 1
                report["compacted_bytes_preserved"] += len(line.encode("utf-8"))
                found = False
                for marker in IMAGE_MARKERS:
                    count = line.count(marker)
                    report["compacted_raw_marker_counts"][marker] += count
                    found = found or count > 0
                report["compacted_image_related_lines"] += int(found)
                estimated_size += len(line.encode("utf-8"))
                continue

            payload_counts = count_payloads(obj)
            add_counts(report["active_payload_counts"], payload_counts)
            report["active_image_related_lines"] += int(any(payload_counts.values()))
            for marker in IMAGE_MARKERS:
                report["active_raw_marker_counts"][marker] += line.count(marker)
            cleaned = clean_value(obj, dry_stats)
            ending = line_ending(line)
            estimated_size += len((json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + ending).encode("utf-8"))

    report["estimated_cleaned_size"] = estimated_size
    return report


def new_clean_stats() -> dict[str, int]:
    return {
        "data_uris_removed": 0,
        "removed_input_image": 0,
        "removed_image_text_tags": 0,
        "cleared_event_images": 0,
        "lines": 0,
    }


def validate_rollout(path: Path, expected_compacted_hashes: list[str] | None = None) -> dict[str, Any]:
    active = {"data_image_values": 0, "input_image_items": 0, "event_image_items": 0}
    hashes: list[str] = []
    lines = 0
    with path.open("r", encoding="utf-8", newline="") as source:
        for lines, line in enumerate(source, 1):
            obj = json.loads(line)
            if obj.get("type") == "compacted":
                hashes.append(compacted_hash(line))
            else:
                add_counts(active, count_payloads(obj))
    if any(active.values()):
        raise SystemExit(f"Active image payloads remain in {path}: {active}")
    if expected_compacted_hashes is not None and hashes != expected_compacted_hashes:
        raise SystemExit(f"Compacted history changed in {path}; refusing to replace the rollout.")
    return {"line_count": lines, "active_payload_counts": active, "compacted_hashes": hashes}


def write_restore_scripts(backup_dir: Path, backup_path: Path, target: Path) -> tuple[Path, Path]:
    restore_ps1 = backup_dir / "restore.ps1"
    restore_ps1.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"Copy-Item -LiteralPath '{str(backup_path).replace(chr(39), chr(39) * 2)}' "
        f"-Destination '{str(target).replace(chr(39), chr(39) * 2)}' -Force\n",
        encoding="utf-8",
    )
    restore_sh = backup_dir / "restore.sh"
    restore_sh.write_text(
        "#!/bin/sh\nset -eu\ncp -- " + shlex.quote(str(backup_path)) + " " + shlex.quote(str(target)) + "\n",
        encoding="utf-8",
    )
    restore_sh.chmod(0o700)
    return restore_ps1, restore_sh


def inspect_file_usage(path: Path) -> dict[str, Any]:
    """Return in_use, not_found, or unknown; a snapshot, never a lock.

    Scan current-user Codex/ChatGPT processes and this process only.
    Unknown ownership or permission failures within scope make the result unknown.
    A known holder takes precedence.
    Windows open_files may silently omit handles, so a negative is unknown.
    """
    pids: set[int] = set()
    errors: list[str] = []
    out_of_scope = 0
    try:
        target = path.resolve(strict=True)
        stat = target.stat()
        identity = (stat.st_dev, stat.st_ino)
        uid = os.geteuid() if sys.platform != "win32" else None
        username = psutil.Process().username() if uid is None else None
        for process in psutil.process_iter():
            try:
                same_user = uid in process.uids() if uid is not None else process.username() == username
                if not same_user:
                    out_of_scope += 1
                    continue
                if process.status() == psutil.STATUS_ZOMBIE:
                    # Zombies have exited and released their file descriptors.
                    continue
                name = process.name().casefold()
                if process.pid != os.getpid() and not any(token in name for token in ("codex", "chatgpt")):
                    out_of_scope += 1
                    continue
                files = process.open_files()
            except psutil.ZombieProcess:
                errors.append(f"PID {process.pid}: ZombieProcess")
                continue
            except psutil.NoSuchProcess:
                # A process which exited cannot retain an open handle.
                continue
            except (psutil.Error, OSError) as exc:
                errors.append(f"PID {process.pid}: {type(exc).__name__}")
                continue
            for file in files:
                try:
                    # stat follows aliases/hardlinks; don't rely on path equality.
                    if os.path.normcase(os.path.abspath(file.path)) == os.path.normcase(str(target)):
                        pids.add(process.pid)
                        continue
                    opened = os.stat(file.path)
                    if (opened.st_dev, opened.st_ino) == identity:
                        pids.add(process.pid)
                except FileNotFoundError:
                    # This other pathname has disappeared since enumeration.
                    continue
                except OSError as exc:
                    errors.append(f"PID {process.pid}: {type(exc).__name__}")
        if sys.platform == "win32":
            errors.append("Windows open_files() may omit file handles")
    except (psutil.Error, OSError) as exc:
        errors.append(type(exc).__name__)
    status = "in_use" if pids else "unknown" if errors else "not_found"
    return {"status": status, "pids": sorted(pids), "errors": errors,
            "scope": "current_user_codex", "out_of_scope_count": out_of_scope}


def assert_rollout_closed(path: Path, *, force_replace: bool = False) -> None:
    result = inspect_file_usage(path)
    if force_replace and result["status"] != "not_found":
        print(
            f"WARNING: --force-replace bypasses {result['status']} file-usage check for {path}; "
            f"PIDs: {result['pids']}; errors: {result['errors'][:8]}. "
            "Live writers may lose subsequent conversations; concurrent database access may invalidate history repair.",
            file=sys.stderr,
        )
        return
    if result["status"] == "in_use":
        raise SystemExit(
            f"File is open by a process (PIDs: {result['pids']}). Close Codex App "
            "and any CLI/app-server using this thread, then run from an external "
            "terminal. Replacing an open rollout can lose future conversations after restart."
        )
    if result["status"] != "not_found":
        details = "; ".join(result["errors"][:8])
        raise SystemExit(f"Cannot verify file is closed for current-user Codex processes (unknown): {path}: {details}")


def validate_thread_identity(path: Path, thread_id: str | None) -> str | None:
    with path.open("r", encoding="utf-8") as source:
        meta = json.loads(source.readline())
    payload = meta.get("payload")
    recorded_id = payload.get("id") if meta.get("type") == "session_meta" and isinstance(payload, dict) else None
    requested_id = parse_thread_id(thread_id) if thread_id else None
    if recorded_id and requested_id and recorded_id != requested_id:
        raise SystemExit("Thread ID does not match rollout session_meta; refusing to modify files.")
    return requested_id or recorded_id


def repair_history_index(path: Path, db: Path, thread_id: str, backup_root: Path, *, force_replace: bool = False) -> dict[str, Any]:
    """Rebase cached byte offsets using unchanged zero-based rollout ordinals.

    The end ordinal is inclusive; the projection cursor is the next ordinal.
    Do not discard indexed items or change any other thread's history.
    """
    if not db.exists():
        raise SystemExit(f"History DB not found: {db}")
    assert_rollout_closed(path, force_replace=force_replace)
    assert_rollout_closed(db, force_replace=force_replace)
    validate_thread_identity(path, thread_id)
    offsets = [0]
    with path.open("rb") as source:
        for line in source:
            json.loads(line)
            offsets.append(offsets[-1] + len(line))

    def offset(ordinal: int | None, *, end: bool = False) -> int | None:
        if ordinal is None:
            return None
        index = ordinal + int(end)
        if index < 0 or index >= len(offsets):
            raise SystemExit(f"History ordinal {ordinal} is outside rollout; refusing repair.")
        return offsets[index]

    con = sqlite3.connect(str(db))
    try:
        turns = con.execute(
            "SELECT rowid, rollout_ordinal, rollout_end_ordinal FROM thread_turns WHERE thread_id=?", (thread_id,)
        ).fetchall()
        cursor = con.execute(
            "SELECT next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id=?", (thread_id,)
        ).fetchone()
        updates = [(offset(start), offset(end, end=True), rowid) for rowid, start, end in turns]
        next_offset = offset(cursor[0]) if cursor else None
        backup_dir = backup_root / ("history-index-repair-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup_dir.mkdir(parents=True, exist_ok=False)
        backup = backup_dir / db.name
        with closing(sqlite3.connect(str(backup))) as dest:
            con.backup(dest)
        # Clients must remain stopped throughout; file-usage inspection is not a lock.
        with con:
            con.executemany(
                "UPDATE thread_turns SET rollout_byte_offset=?, rollout_end_byte_offset=? WHERE rowid=?", updates
            )
            if cursor:
                con.execute(
                    "UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?",
                    (next_offset, thread_id),
                )
    finally:
        con.close()
    return {"force_replace": force_replace, "thread_id": thread_id, "history_backup": str(backup),
            "turns_rebased": len(updates), "next_rollout_byte_offset": next_offset}


def clean_rollout(path: Path, backup_root: Path, thread_id: str | None, history_db: Path | None = None, *, force_replace: bool = False) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Rollout not found: {path}")

    assert_rollout_closed(path, force_replace=force_replace)
    indexed_thread = validate_thread_identity(path, thread_id)
    history_db = history_db if history_db is not None else codex_home() / "thread_history_1.sqlite"
    repair_needed = False
    if indexed_thread and history_db.exists():
        con = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
        try:
            repair_needed = con.execute(
                "SELECT 1 FROM thread_history_projection_state WHERE thread_id=?", (indexed_thread,)
            ).fetchone() is not None
        finally:
            con.close()
        if repair_needed:
            assert_rollout_closed(history_db, force_replace=force_replace)
    source_stat = path.stat()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = thread_id[:8] if thread_id else path.stem[-8:]
    backup_dir = backup_root / f"rollout-session-imageclean-{stamp}-{slug}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / path.name
    shutil.copy2(path, backup_path)

    stats = new_clean_stats()
    compacted_hashes: list[str] = []
    compacted_bytes = 0
    tmp = path.with_name(path.name + f".tmp-imageclean-{os.getpid()}")
    try:
        with path.open("r", encoding="utf-8", newline="") as source, tmp.open("w", encoding="utf-8", newline="") as output:
            for line_no, line in enumerate(source, 1):
                stats["lines"] = line_no
                obj = json.loads(line)
                if obj.get("type") == "compacted":
                    output.write(line)
                    compacted_hashes.append(compacted_hash(line))
                    compacted_bytes += len(line.encode("utf-8"))
                    continue
                cleaned = clean_value(obj, stats)
                output.write(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + line_ending(line))

        checked = validate_rollout(tmp, compacted_hashes)
        assert_rollout_closed(path, force_replace=force_replace)
        current_stat = path.stat()
        if (current_stat.st_dev, current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns) != (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns):
            raise SystemExit("Rollout changed during cleanup; target was not replaced.")

        os.chmod(tmp, source_stat.st_mode)
        os.replace(tmp, path)
        final = validate_rollout(path, compacted_hashes)
    finally:
        tmp.unlink(missing_ok=True)

    restore_ps1, restore_sh = write_restore_scripts(backup_dir, backup_path, path)
    try:
        history_repair = (
            repair_history_index(path, history_db, indexed_thread, backup_dir, force_replace=force_replace)
            if repair_needed else None
        )
    except (Exception, SystemExit):
        # Index repair validates before updating and uses a SQLite transaction.
        # Restore the matching rollout if it cannot complete.
        assert_rollout_closed(path, force_replace=force_replace)
        try:
            shutil.copy2(backup_path, tmp)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        raise
    manifest = {
        "force_replace": force_replace,
        "history_index_repair": history_repair,
        "thread_id": thread_id,
        "target": str(path),
        "backup": str(backup_path),
        "restore_ps1": str(restore_ps1),
        "restore_sh": str(restore_sh),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "old_size": source_stat.st_size,
        "new_size": path.stat().st_size,
        "saved_bytes": source_stat.st_size - path.stat().st_size,
        "stats": stats,
        "compacted_lines_preserved": len(compacted_hashes),
        "compacted_bytes_preserved": compacted_bytes,
        "active_payload_counts": final["active_payload_counts"],
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="Codex thread UUID or codex://threads/<uuid>")
    parser.add_argument("--path", help="Explicit rollout JSONL path")
    parser.add_argument("--state-db", help="Codex state_5.sqlite path")
    parser.add_argument("--backup-root", default=str(Path.home() / "Documents" / "Codex" / "codex-backups"))
    parser.add_argument("--repair-history-index", action="store_true", help="Offline repair of cached history byte offsets; requires --thread and --apply.")
    parser.add_argument("--history-db", help="Override thread_history_1.sqlite path")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Without this flag, report only.")
    parser.add_argument("--force-replace", action="store_true", help="With --apply, bypass in_use/unknown file-usage checks. Live writers may lose subsequent conversations.")
    args = parser.parse_args()
    if args.force_replace and not args.apply:
        parser.error("--force-replace requires --apply")

    thread_id, rollout_path = resolve_rollout(args.thread, args.path, args.state_db)
    rollout_path = rollout_path.expanduser()
    if not rollout_path.exists():
        raise SystemExit(f"Rollout not found: {rollout_path}")
    if args.repair_history_index:
        if not args.apply or not thread_id:
            raise SystemExit("--repair-history-index requires --thread and --apply.")
        result = repair_history_index(
            rollout_path, Path(args.history_db).expanduser() if args.history_db else codex_home() / "thread_history_1.sqlite",
            parse_thread_id(thread_id), Path(args.backup_root).expanduser(), force_replace=args.force_replace,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = (
        {"mode": "apply", "thread_id": thread_id, "result": clean_rollout(rollout_path, Path(args.backup_root).expanduser(), thread_id, Path(args.history_db).expanduser() if args.history_db else None, force_replace=args.force_replace)}
        if args.apply
        else {"mode": "report", "thread_id": thread_id, "report": analyze_rollout(rollout_path)}
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
