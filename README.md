# Codex WebSocket Image Cleaner

**Fix Codex `Reconnecting...`, WebSocket-like stream disconnects, and oversized session rollouts caused by embedded image payloads.**

Codex WebSocket Image Cleaner is a backup-first Codex skill and local repair script for Codex Desktop / Codex CLI threads whose `rollout-*.jsonl` session files have become huge because image data was stored inline as base64.

It targets a specific but common failure mode:

```text
Reconnecting... 1/5
stream disconnected before completion
failed to send websocket request
Codex ran out of room in the model's context window
Invalid image in your last message. Please remove it and try again.
```

Many users first debug VPN, proxies, TUN mode, DNS, WebSocket fallback, or SSE fallback. Those can be real problems. But image-heavy Codex sessions can create another hidden problem: the local session itself becomes too large to hydrate, compact, or transmit reliably. This tool removes embedded image bytes while preserving text history and a full rollback backup.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)

Current release: **v1.1.0**. See [CHANGELOG.md](CHANGELOG.md).

## Why This Matters

Codex stores conversation state in local JSONL rollout files under:

```text
~/.codex/sessions
```

When a thread involves screenshots, generated images, image editing, visual QA, or repeated image inspection, the rollout can contain large inline payloads:

```text
data:image/png;base64,...
input_image
image_url
```

These payloads can dominate the session file. In real cleanups, image payloads accounted for 60-97% of rollout size, and a single thread could grow to 25 MB, 64 MB, 92 MB, or 130 MB.

Once a thread reaches that size, the visible symptom may look like a WebSocket problem even when the underlying trigger is session bloat.

## Public Signals

Public reports show two overlapping issue classes.

### WebSocket and proxy reconnect loops

GitHub issue [`openai/codex#19821`](https://github.com/openai/codex/issues/19821) describes Codex repeatedly showing `Reconnecting... 1/5` through `5/5`, especially behind proxies, before falling back to HTTP/SSE. That is the transport-side problem.

### Huge rollout files with embedded images

GitHub issue [`openai/codex#22004`](https://github.com/openai/codex/issues/22004) reports Codex Desktop crashing when loading very large rollout JSONL files and notes that images are embedded as base64 inline in the rollout. The issue suggests storing image references instead of raw inline image bytes.

### Large context, image, and upload failures

Community reports also connect reconnect or stream errors with large context, big files, images, and failed compaction:

- https://www.reddit.com/r/codex/comments/1t18wtj/codex_desktop_stream_disconnected_before/
- https://www.reddit.com/r/codex/comments/1svdy5h/codex_error_running_remote_compact_task_stream/
- https://www.reddit.com/r/codex/comments/1tkban6/bug/

X/Twitter search shows broad Codex performance and stability discussion, but durable technical details are easier to verify in GitHub issues and local rollout evidence.

## What This Tool Does

The script scans a target Codex rollout JSONL file for:

- `data:image/`
- `input_image`
- `image_url`

When applied, it:

- finds a target rollout from a thread id, `codex://threads/...` URL, or explicit file path
- creates a timestamped backup before any mutation
- writes a rollback script: `restore.ps1`
- converts image-heavy function/tool outputs into textual placeholders
- removes message `input_image` items
- removes leftover `<image>` wrapper text when present
- clears event image arrays
- preserves every `type: "compacted"` line byte-for-byte
- validates every rewritten JSONL line
- refuses to replace the original if active image payloads remain outside compacted lines
- verifies the ordered SHA-256 hashes of all compacted lines

The original rollout backup is preserved.

Compacted records are opaque Codex hydration checkpoints, not ordinary transcript lines. They may retain historical image bytes and marker text. The cleaner reports those retained bytes but never rewrites them: changing `compacted.payload.replacement_history` can make restart hydration fall back to full history and trigger `context_window_exceeded`.

## What It Does Not Do

This is not a general Codex optimizer and not a network proxy fix.

It does not:

- fix every WebSocket reconnect loop
- configure VPN, DNS, TUN mode, or proxy environment variables
- summarize old text conversation history
- delete normal text messages
- edit unrelated SQLite data (only the target thread history byte offsets are rebased)
- remove generated image files from disk
- guarantee that a very long text-only thread fits the model context window

If a small new thread also reconnects, investigate network transport. If a text-only thread hits context limits, use `/clear`, `/compact`, or start a new handoff thread.

## Quick Start

Report only:

```powershell
python scripts/imageclean.py --thread 019e4ae7-0621-7443-9181-8fcddf82995a
```

Apply cleanup:

```powershell
python scripts/imageclean.py --thread 019e4ae7-0621-7443-9181-8fcddf82995a --apply
```

Use a Codex thread URL:

```powershell
python scripts/imageclean.py --thread codex://threads/019e4ae7-0621-7443-9181-8fcddf82995a --apply
```

Use an explicit rollout path:

```powershell
python scripts/imageclean.py --path C:\Users\you\.codex\sessions\2026\05\21\rollout-....jsonl --apply
```

Choose a custom backup root:

```powershell
python scripts/imageclean.py --thread 019e4ae7-0621-7443-9181-8fcddf82995a --backup-root D:\codex-backups --apply
```

## Install As A Codex Skill

Clone the repository and copy the skill folder into your Codex skills directory:

```powershell
git clone https://github.com/MaxxxDong/codex-websocket-image-cleaner.git
Copy-Item -Recurse .\codex-websocket-image-cleaner "$env:USERPROFILE\.codex\skills\codex-websocket-image-cleaner"
```

Restart Codex Desktop after installing a new skill.

## Restore

Every `--apply` creates a backup directory:

```text
C:\Users\<user>\Documents\Codex\codex-backups\rollout-session-imageclean-YYYYMMDD-HHMMSS-<thread>
```

To roll back:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\<user>\Documents\Codex\codex-backups\<backup>\restore.ps1
```

If Codex Desktop has the thread open, restart Codex before testing the cleaned or restored rollout.

## Real Cleanup Examples

Observed Windows Codex Desktop cleanup outcomes:

| Case | Before | After | Main cause |
|---|---:|---:|---|
| image-heavy thread | 130 MB | 4 MB | inline base64 images |
| repeated image inspection | 119 MB | 18 MB | 79 image outputs |
| visual workflow thread | 92 MB | 19 MB | generated images appended after previous cleanup |
| nested history payloads | 72 MB | 12 MB | embedded image payloads inside historical records |
| smaller image session | 25 MB | 0.9 MB | 9 image outputs |

Text-only sessions usually do not benefit much from this tool.

## How It Works

Codex rollout files are JSONL: one JSON object per line. The script uses a conservative rewrite strategy:

1. Resolve the target thread id to a `rollout-*.jsonl` file.
2. Copy the original file to a backup directory.
3. Parse each JSONL line with Python `json`.
4. Copy compacted checkpoint lines byte-for-byte.
5. Rewrite only active image-bearing message/tool payloads.
6. Validate every output line and active payload count.
7. Verify ordered compacted-line SHA-256 hashes.
8. Replace the original only after validation succeeds.

This keeps the repair local, deterministic, and reversible.

## Recommended Triage For Codex Reconnecting

Use this order before blaming the network:

1. Check whether all threads reconnect or only one old thread does.
2. Inspect that thread's rollout size under `~/.codex/sessions`.
3. Count image markers: `data:image/`, `input_image`, `image_url`.
4. If image payloads dominate the file, run this cleaner.
5. If there are no images but many compactions/context errors, use `/clear`, `/compact`, or a new handoff thread.
6. If new/small threads also reconnect, investigate WebSocket, VPN, DNS, TUN, proxy, or service instability.

## Limitations And Risks

- The old visual image content is removed from the chat transcript. Text history remains.
- If you need the exact images visible inside the old thread, export or archive first.
- Nested active payloads are cleaned structurally; compacted checkpoints are never recursively rewritten.
- Codex may append new image payloads later if the thread continues using images.
- This tool depends on the current Codex rollout JSONL format.
- Always keep the backup until the repaired thread has been tested.
- Safe output may remain larger than a theoretical marker-zero rewrite because hydration checkpoints are preserved.

## Repository Contents

```text
codex-websocket-image-cleaner/
├── CHANGELOG.md
├── README.md
├── README.zh-CN.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── scripts/
│   └── imageclean.py
└── tests/
    └── test_imageclean.py
```

## Status

Current version: **v1.1.0**

- report-only mode
- backup-first apply mode
- restore script generation
- JSONL validation
- active image payload validation
- byte-exact compacted checkpoint preservation
- compacted-line hash validation
- Codex skill metadata

Planned improvements:

- batch report over all Codex sessions
- size threshold warnings
- stale `.tmp-imageclean-*` cleanup
- optional HTML/Markdown cleanup report

## Close clients before applying or restoring

Before `--apply` or restore, quit Codex Desktop and stop any CLI/app-server using the target thread. Run the cleaner from an external terminal and reopen Codex only after it finishes. An idle thread can still have an open writer.

On macOS/Linux, replacing an open rollout leaves its writer attached to the old inode. Later conversations can disappear after exit. The cleaner checks open files with `psutil` before backup and replacement. It returns `in_use` (holder PIDs found), `not_found` (scan completed without finding a holder), or `unknown` (incomplete inspection). Apply normally proceeds only for `not_found` (see the explicit override below). The scope is explicitly `current_user_codex`: processes with a different owner are outside scope and counted in `out_of_scope_count`. Unknown ownership or denied access to an in-scope process returns `unknown`. A negative result does not rule out access by another user or root. Run as the user running Codex. Windows negative scans remain `unknown` because psutil may omit handles. These checks are best effort; keep clients stopped throughout cleanup to avoid a race after the final check.

If history has already disappeared, preserve the current rollout and backups before recovery. Restoring the original backup overwrites newer history and cannot recover conversations written only to an unlinked inode. Recovery through an open handle may be possible while the original writer is still alive; after it exits that route is unavailable.

## Repair stale paginated history after older cleanups

Cleanup now rebases the target thread's cached byte offsets in `thread_history_1.sqlite`, with a database backup. Older cleanups could leave the cursor beyond EOF, hiding later conversations that remain in the rollout.

Quit all Codex clients and app-server processes, then run from an external terminal:

```bash
python3 scripts/imageclean.py --thread <thread-id> --repair-history-index --apply
```

This repairs offsets using unchanged record ordinals without rewriting the rollout or deleting items. Keep every client of the history database stopped. After restoring an older rollout, rerun this repair to match its offsets.

## Python dependency

`uv run scripts/imageclean.py ...` installs the inline `psutil` dependency automatically. For direct Python use, run `python3 -m pip install -r requirements.txt` first. Run tests with `uv run --with psutil python -m unittest discover -s tests -v`. The scan is a snapshot and does not prevent another process from opening the file afterward.

## Explicit override

`--apply --force-replace` overrides both `in_use` and `unknown` file-usage results for the rollout and history database, including `--repair-history-index`. Warnings are printed to stderr and the result records `force_replace: true`. Backups, thread identity checks, JSON/checkpoint validation, and concurrent rollout modification checks still apply. It does not bypass OS permissions or SQLite locking errors. Keep clients stopped: forcing replacement of a live file can lose later conversations.

```bash
uv run scripts/imageclean.py --thread <thread-id> --apply --force-replace
```

Occupancy scope uses process names containing `codex` or `chatgpt` (case-insensitive), plus the cleaner PID; zombies are skipped because they have released descriptors. Renamed writers, other applications, and other users are outside this heuristic scope. A negative result is not a system-wide guarantee.
