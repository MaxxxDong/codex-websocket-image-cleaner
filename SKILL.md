---
name: codex-websocket-image-cleaner
description: Diagnose and repair Codex threads where WebSocket reconnecting, stream disconnects, slow hydration, context failures, or "Invalid image in your last message" are caused by oversized rollout JSONL files with embedded image payloads. Use for Codex sessions bloated by data:image, input_image, or image_url fields that need backup-first image payload cleanup.
metadata:
  short-description: Fix Codex reconnects caused by image-bloated sessions
---

# Codex WebSocket Image Cleaner

Use this skill when a Codex thread/session rollout is bloated by embedded images and appears as a WebSocket or reconnecting problem:

- `Invalid image in your last message. Please remove it and try again.`
- `failed to send websocket request`
- `stream disconnected before completion`
- repeated `Reconnecting...`
- very large rollout JSONL files caused by `data:image/...base64`

This is a local session repair workflow. It is not an official Codex storage migration. Preserve continuity and reversibility.

Read `README.md` for user-facing documentation and `README.zh-CN.md` for the Chinese version when preparing public explanations.

## Safety Rules

- Default to report-only. Do not mutate a rollout unless the user explicitly asks to clean/apply.
- Only touch the exact thread/session requested by the user.
- Treat `~/.codex` and `%USERPROFILE%` as protected paths.
- Back up the rollout before applying changes.
- Never delete the original backup.
- Do not truncate event history, reorder lines, or edit SQLite records.
- Prefer removing image payloads over replacing them with tiny image data URLs; tiny images can still trigger model-side image validation errors.
- After any apply, validate every JSONL line and assert there are no remaining `data:image/`, `input_image`, or `image_url` markers.

## Workflow

1. Resolve the target rollout from one of:
   - `codex://threads/<thread-id>`
   - a raw thread/session UUID
   - an explicit rollout JSONL path
2. Run a report first:

```powershell
python scripts/imageclean.py --thread 019e356a-cf17-7b11-aab5-88e6e3988a51
```

3. If the user asked to apply, run:

```powershell
python scripts/imageclean.py --thread 019e356a-cf17-7b11-aab5-88e6e3988a51 --apply
```

4. Report:
   - original size
   - new size
   - saved bytes
   - number of textified function image outputs
   - number of removed message `input_image` items
   - backup directory
   - restore script path
   - final marker counts

5. Ask the user to restart Codex App and test the repaired thread if the App had the thread open.

## What Apply Does

The bundled script:

- copies the original rollout to `~/Documents/Codex/codex-backups/rollout-session-imageclean-*`
- writes `manifest.json`
- writes `restore.ps1`
- converts function/tool outputs containing image payloads into textual placeholders
- removes `input_image` items from message content lists
- removes `<image>` / `</image>` wrapper text left beside removed images
- clears event `images` arrays
- validates the rewritten JSONL
- verifies no image markers remain

## Restore

Run the generated restore script from the backup directory:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Administrator\Documents\Codex\codex-backups\<backup>\restore.ps1
```

## When Not To Use

- Do not use this for ordinary local log rotation.
- Do not use this to archive sessions; use a session maintenance workflow instead.
- Do not use this if the user needs the old thread's visual image content inside chat history. In that case, create a handoff and continue in a fresh thread.
