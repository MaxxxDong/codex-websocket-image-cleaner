# Changelog

## Unreleased

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
