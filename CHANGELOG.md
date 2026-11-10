# Changelog

## Unreleased

- Scope psutil inspection to current-user Codex/ChatGPT processes and the cleaner itself; skip exited zombies; report scope and excluded process count, while preserving unknown for unidentified owners and inaccessible in-scope processes.

- Add explicit `--apply --force-replace` override for in_use/unknown occupancy checks, retaining backup and integrity checks and recording the override in results.

- Replace lsof subprocess parsing with psutil inspection returning in_use, not_found, or unknown. Block incomplete scans, including permission failures and Windows negative results; declare the psutil dependency for uv and pip.

- Rebase cached paginated history byte offsets after shrinking a rollout, with a history database backup.
- Add `--repair-history-index --apply` to recover history hidden by older cleanups without rewriting the rollout.
- Refuse POSIX cleanup when rollout/history files are open or handle inspection fails.
- Document offline cleanup and repair requirements and add regression tests.

## v1.1.0 - 2026-08-13

- Preserve every Codex `type: "compacted"` hydration checkpoint byte-for-byte.
- Refuse replacement unless ordered compacted-line SHA-256 hashes remain unchanged.
- Clean only real active image payloads outside compacted records.
- Preserve harmless text that merely mentions `image_url` or `input_image`.
- Add atomic replacement, concurrent-write detection, macOS/Linux restore scripts, and regression tests.
- Document recovery from older cleaner versions that could cause restart-time `context_window_exceeded` failures.
