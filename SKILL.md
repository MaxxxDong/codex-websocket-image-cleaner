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
- After shrinking a rollout, rebase the target thread history index byte offsets from unchanged record ordinals. Back up the history database first and require all clients of that database to be stopped. For already cleaned threads use `--repair-history-index --apply`; do not restore an old rollout over newer history.
- Treat `~/.codex` and `%USERPROFILE%` as protected paths.
- Back up the rollout before applying changes.
- Never delete the original backup.
- Do not truncate event history, reorder lines, edit unrelated SQLite records, or rewrite any `type: "compacted"` line.
- Treat compacted lines as opaque hydration checkpoints. Copy them byte-for-byte and verify their ordered SHA-256 hashes before replacing the rollout.
- Prefer removing image payloads over replacing them with tiny image data URLs; tiny images can still trigger model-side image validation errors.
- Remove actual active payloads structurally. Do not replace ordinary text merely because it mentions `input_image` or `image_url`.
- After any apply, validate every JSONL line, assert no active image payloads remain outside compacted lines, and assert every compacted-line hash is unchanged.

## Workflow

1. Resolve the target rollout from one of:
   - `codex://threads/<thread-id>`
   - a raw thread/session UUID
   - an explicit rollout JSONL path
2. Run a report first:

```powershell
python scripts/imageclean.py --thread 019e356a-cf17-7b11-aab5-88e6e3988a51
```

3. Before apply, have the user quit Codex Desktop and stop CLI/app-server processes using this thread. Run from an external terminal; keep clients stopped until cleanup finishes. An idle thread may still hold a writer. POSIX apply requires lsof and refuses open files or failed inspection. If the user asked to apply, run:

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
   - active payload counts
   - number and bytes of compacted lines preserved
   - compacted-line hash verification

5. Reopen Codex App and test continuation, then restart and verify the new conversation persists.

## What Apply Does

The bundled script:

- copies the original rollout to `~/Documents/Codex/codex-backups/rollout-session-imageclean-*`
- writes `manifest.json`
- writes `restore.ps1`
- converts function/tool outputs containing image payloads into textual placeholders
- removes `input_image` items from message content lists
- removes `<image>` / `</image>` wrapper text left beside removed images
- clears event `images` arrays
- preserves every compacted line byte-for-byte, including any historical image bytes inside it
- validates the rewritten JSONL
- verifies no active image payloads remain outside compacted lines
- verifies ordered compacted-line hashes are unchanged

Raw marker counts may remain inside preserved compacted checkpoints or harmless text/schema descriptions. This is intentional. A smaller cleanup that preserves hydration semantics is safer than a marker-zero rewrite that invalidates remote compact state.

## Recovery From Older Cleaner Versions

If a cleaned thread works before restart but fails after restart with `context_window_exceeded`, compare it with the backup. If any compacted line changed, restore the backup first, then rerun the current cleaner. Never repair this symptom by recursively scrubbing `compacted.payload.replacement_history`.

## Restore

Quit clients using the thread before restoring. Preserve the current rollout separately: restore overwrites newer history. Run the generated restore script from the backup directory:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Administrator\Documents\Codex\codex-backups\<backup>\restore.ps1
```

## When Not To Use

- Do not use this for ordinary local log rotation.
- Do not use this to archive sessions; use a session maintenance workflow instead.
- Do not use this if the user needs the old thread's visual image content inside chat history. In that case, create a handoff and continue in a fresh thread.
