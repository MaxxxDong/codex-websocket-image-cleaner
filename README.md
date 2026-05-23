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
- validates every rewritten JSONL line
- refuses to replace the original if image markers remain

The original rollout backup is preserved.

## What It Does Not Do

This is not a general Codex optimizer and not a network proxy fix.

It does not:

- fix every WebSocket reconnect loop
- configure VPN, DNS, TUN mode, or proxy environment variables
- summarize old text conversation history
- delete normal text messages
- edit SQLite databases
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
git clone https://github.com/<owner>/codex-websocket-image-cleaner.git
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
4. Rewrite only image-bearing message/tool payloads.
5. Serialize compact JSONL.
6. Validate every output line.
7. Count forbidden image markers.
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
- Some nested historical payloads can require a recursive JSON-aware cleanup pass.
- Codex may append new image payloads later if the thread continues using images.
- This tool depends on the current Codex rollout JSONL format.
- Always keep the backup until the repaired thread has been tested.

## Repository Contents

```text
codex-websocket-image-cleaner/
├── README.md
├── README.zh-CN.md
├── SKILL.md
├── agents/
│   └── openai.yaml
└── scripts/
    └── imageclean.py
```

## Status

Current version:

- report-only mode
- backup-first apply mode
- restore script generation
- JSONL validation
- image marker validation
- Codex skill metadata

Planned improvements:

- built-in recursive cleanup mode
- batch report over all Codex sessions
- size threshold warnings
- stale `.tmp-imageclean-*` cleanup
- optional HTML/Markdown cleanup report
