#!/usr/bin/env python3
"""Report or remove embedded image payloads from a Codex rollout JSONL file.

The script is intentionally conservative:
- report-only by default
- exact thread/path targeting
- backup before apply
- JSONL validation before and after rewrite
- no SQLite mutation
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any


IMAGE_MARKERS = ("data:image/", "input_image", "image_url")
IMAGE_REMOVED_TEXT = (
    "[image payload removed from local Codex rollout by rollout-session-imageclean; "
    "original rollout is preserved in backup]"
)


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


def line_has_image_payload(line: str) -> bool:
    return any(marker in line for marker in IMAGE_MARKERS)


def report_rollout(path: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
        "line_count": 0,
        "type_counts": {},
        "marker_counts": {marker: 0 for marker in IMAGE_MARKERS},
        "image_related_lines": 0,
        "function_image_outputs": 0,
        "message_input_images": 0,
        "event_images": 0,
        "estimated_size_without_image_strings": None,
    }
    if not path.exists():
        return stats

    type_counts: dict[str, int] = {}
    estimated_saved = 0
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            stats["line_count"] = line_no
            obj = json.loads(line)
            typ = obj.get("type", "?")
            type_counts[typ] = type_counts.get(typ, 0) + 1
            for marker in IMAGE_MARKERS:
                stats["marker_counts"][marker] += line.count(marker)
            if line_has_image_payload(line):
                stats["image_related_lines"] += 1
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if isinstance(payload, dict):
                if payload.get("type") == "function_call_output":
                    raw = json.dumps(payload.get("output"), ensure_ascii=False)
                    if line_has_image_payload(raw):
                        stats["function_image_outputs"] += 1
                        estimated_saved += max(0, len(raw.encode("utf-8")) - len(IMAGE_REMOVED_TEXT.encode("utf-8")))
                if payload.get("type") == "message":
                    stats["message_input_images"] += count_input_images(payload.get("content"))
                if payload.get("type") == "user_message" and isinstance(payload.get("images"), list):
                    stats["event_images"] += len(payload["images"])
    stats["type_counts"] = type_counts
    stats["estimated_size_without_image_strings"] = max(0, int(stats["size"] or 0) - estimated_saved)
    return stats


def count_input_images(value: Any) -> int:
    if isinstance(value, list):
        return sum(count_input_images(item) for item in value)
    if isinstance(value, dict):
        return (1 if value.get("type") == "input_image" else 0) + sum(count_input_images(v) for v in value.values())
    return 0


def clean_content_list(content: Any, stats: dict[str, int]) -> Any:
    if not isinstance(content, list):
        return content
    new_content = []
    changed = False
    for item in content:
        if isinstance(item, dict) and item.get("type") == "input_image":
            stats["removed_input_image"] += 1
            changed = True
            continue
        if isinstance(item, dict) and item.get("type") == "input_text" and item.get("text") in ("<image>", "</image>"):
            stats["removed_image_text_tags"] += 1
            changed = True
            continue
        new_content.append(item)
    if changed:
        new_content.append({"type": "input_text", "text": IMAGE_REMOVED_TEXT})
    return new_content


def walk_clean(value: Any, stats: dict[str, int]) -> Any:
    if isinstance(value, dict):
        if "content" in value:
            value["content"] = clean_content_list(value["content"], stats)
        if value.get("type") == "user_message" and isinstance(value.get("images"), list) and value["images"]:
            stats["cleared_event_images"] += len(value["images"])
            value["images"] = []
        for key in list(value.keys()):
            value[key] = walk_clean(value[key], stats)
        return value
    if isinstance(value, list):
        return [walk_clean(item, stats) for item in value]
    return value


def clean_rollout(path: Path, backup_root: Path, thread_id: str | None) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Rollout not found: {path}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = thread_id[:8] if thread_id else path.stem[-8:]
    backup_dir = backup_root / f"rollout-session-imageclean-{stamp}-{slug}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / path.name
    shutil.copy2(path, backup_path)

    before_size = path.stat().st_size
    stats = {
        "function_outputs_textified": 0,
        "removed_input_image": 0,
        "removed_image_text_tags": 0,
        "cleared_event_images": 0,
        "lines": 0,
    }

    tmp = path.with_name(path.name + f".tmp-imageclean-{os.getpid()}")
    with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8", newline="\n") as dst:
        for line_no, line in enumerate(src, 1):
            stats["lines"] = line_no
            obj = json.loads(line)
            payload = obj.get("payload") if isinstance(obj, dict) else None
            if isinstance(payload, dict) and payload.get("type") == "function_call_output":
                raw = json.dumps(payload.get("output"), ensure_ascii=False)
                if line_has_image_payload(raw):
                    payload["output"] = IMAGE_REMOVED_TEXT
                    stats["function_outputs_textified"] += 1
            obj = walk_clean(obj, stats)
            dst.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    final_markers = validate_no_image_markers(tmp)

    restore_script = backup_dir / "restore.ps1"
    backup_ps = str(backup_path).replace("'", "''")
    path_ps = str(path).replace("'", "''")
    restore_script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"Copy-Item -LiteralPath '{backup_ps}' "
        f"-Destination '{path_ps}' -Force\n"
        "Write-Host 'Restored rollout from rollout-session-imageclean backup.'\n",
        encoding="utf-8",
    )

    manifest = {
        "thread_id": thread_id,
        "target": str(path),
        "backup": str(backup_path),
        "restore_script": str(restore_script),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "old_size": before_size,
        "new_size": tmp.stat().st_size,
        "saved_bytes": before_size - tmp.stat().st_size,
        "stats": stats,
        "final_markers": final_markers,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    shutil.copyfile(tmp, path)
    tmp.unlink()
    validate_no_image_markers(path)
    manifest["new_size"] = path.stat().st_size
    manifest["saved_bytes"] = before_size - path.stat().st_size
    return manifest


def validate_no_image_markers(path: Path) -> dict[str, int]:
    markers = {marker: 0 for marker in IMAGE_MARKERS}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            json.loads(line)
            for marker in IMAGE_MARKERS:
                markers[marker] += line.count(marker)
    if any(markers.values()):
        raise SystemExit(f"Image markers remain in {path}: {markers}")
    return markers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="Codex thread UUID or codex://threads/<uuid>")
    parser.add_argument("--path", help="Explicit rollout JSONL path")
    parser.add_argument("--state-db", help="Codex state_5.sqlite path")
    parser.add_argument("--backup-root", default=str(Path.home() / "Documents" / "Codex" / "codex-backups"))
    parser.add_argument("--apply", action="store_true", help="Apply cleanup. Without this flag, report only.")
    args = parser.parse_args()

    thread_id, rollout_path = resolve_rollout(args.thread, args.path, args.state_db)
    rollout_path = rollout_path.expanduser()
    if not rollout_path.exists():
        raise SystemExit(f"Rollout not found: {rollout_path}")

    if not args.apply:
        result = {"mode": "report", "thread_id": thread_id, "report": report_rollout(rollout_path)}
    else:
        result = {
            "mode": "apply",
            "thread_id": thread_id,
            "result": clean_rollout(rollout_path, Path(args.backup_root).expanduser(), thread_id),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
