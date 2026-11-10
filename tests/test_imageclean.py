import importlib.util
import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "imageclean.py"
SPEC = importlib.util.spec_from_file_location("imageclean", SCRIPT)
imageclean = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imageclean)


class ImageCleanTests(unittest.TestCase):
    def setUp(self):
        # Integration tests deliberately scope visibility to this process;
        # inaccessible system processes must not make them environment-dependent.
        self.processes = mock.patch.object(imageclean.psutil, "process_iter", side_effect=lambda: iter([imageclean.psutil.Process()]))
        self.processes.start()
        self.addCleanup(self.processes.stop)

    def test_file_usage_three_states_and_in_use_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout"
            path.touch()
            denied = mock.Mock(pid=123)
            denied.open_files.side_effect = imageclean.psutil.AccessDenied(123)
            holder = mock.Mock(pid=456)
            holder.open_files.return_value = [mock.Mock(path=str(path))]
            empty = mock.Mock(pid=789)
            empty.open_files.return_value = []
            gone = mock.Mock(pid=999)
            gone.open_files.side_effect = imageclean.psutil.NoSuchProcess(999)
            for process in (denied, holder, empty, gone):
                process.uids.return_value = (os.getuid(),) * 3
                process.name.return_value = "codex"
            for processes, expected in [([empty, gone], "not_found"), ([denied], "unknown"), ([denied, holder], "in_use")]:
                with self.subTest(expected=expected), mock.patch.object(imageclean.psutil, "process_iter", return_value=iter(processes)):
                    result = imageclean.inspect_file_usage(path)
                    self.assertEqual(result["status"], expected)
                    if expected == "in_use":
                        self.assertEqual(result["pids"], [456])

    def test_file_usage_matches_hardlink_and_missing_target_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "rollout"
            target.touch()
            alias = Path(directory) / "alias"
            os.link(target, alias)
            process = mock.Mock(pid=42)
            process.uids.return_value = (os.getuid(),) * 3
            process.name.return_value = "codex"
            process.open_files.return_value = [mock.Mock(path=str(alias))]
            with mock.patch.object(imageclean.psutil, "process_iter", return_value=iter([process])):
                self.assertEqual(imageclean.inspect_file_usage(target)["status"], "in_use")
            self.assertEqual(imageclean.inspect_file_usage(Path(directory) / "missing")["status"], "unknown")

    def test_force_replace_bypasses_only_usage_guard_and_preserves_backup(self):
        for status in ("in_use", "unknown"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "rollout.jsonl"
                original = '{"type":"event_msg","payload":"data:image/png;base64,AAAA"}\n'
                path.write_text(original)
                result = {"status": status, "pids": [123] if status == "in_use" else [], "errors": ["AccessDenied"]}
                with mock.patch.object(imageclean, "inspect_file_usage", return_value=result), mock.patch.object(imageclean.sys, "stderr", new_callable=io.StringIO) as stderr:
                    manifest = imageclean.clean_rollout(path, root / "backups", None, root / "absent.sqlite", force_replace=True)
                    self.assertIn("--force-replace", stderr.getvalue())
                    self.assertIn(status, stderr.getvalue())
                self.assertNotIn("data:image/", path.read_text())
                self.assertEqual(Path(manifest["backup"]).read_text(), original)
                self.assertTrue(manifest["force_replace"])

    def test_force_replace_still_rejects_concurrent_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout.jsonl"
            path.write_text('{"type":"event_msg"}\n')
            validate = imageclean.validate_rollout
            def concurrent_append(*args):
                result = validate(*args)
                with path.open("a") as writer:
                    writer.write('{"type":"event_msg","payload":"new"}\n')
                return result
            with mock.patch.object(imageclean, "validate_rollout", side_effect=concurrent_append):
                with self.assertRaisesRegex(SystemExit, "changed during cleanup"):
                    imageclean.clean_rollout(path, root / "backups", None, root / "absent.sqlite", force_replace=True)
            self.assertIn('"new"', path.read_text())

    @unittest.skipIf(os.name == "nt", "POSIX UID scope")
    def test_other_users_are_outside_scope_but_unknown_owners_block(self):
        with tempfile.NamedTemporaryFile() as file:
            foreign = mock.Mock(pid=1)
            foreign.uids.return_value = (os.getuid() + 1,) * 3
            foreign.open_files.side_effect = imageclean.psutil.AccessDenied(1)
            with mock.patch.object(imageclean.psutil, "process_iter", return_value=iter([foreign])):
                result = imageclean.inspect_file_usage(Path(file.name))
            self.assertEqual(result["status"], "not_found")
            self.assertEqual(result["scope"], "current_user_codex")
            self.assertEqual(result["out_of_scope_count"], 1)
            foreign.open_files.assert_not_called()
            foreign.uids.side_effect = imageclean.psutil.AccessDenied(1)
            with mock.patch.object(imageclean.psutil, "process_iter", return_value=iter([foreign])):
                self.assertEqual(imageclean.inspect_file_usage(Path(file.name))["status"], "unknown")

    @unittest.skipIf(os.name == "nt", "POSIX UID scope")
    def test_unrelated_services_and_zombies_do_not_require_fd_access(self):
        with tempfile.NamedTemporaryFile() as file:
            service = mock.Mock(pid=1)
            zombie = mock.Mock(pid=2)
            for process in (service, zombie):
                process.uids.return_value = (os.getuid(),) * 3
                process.open_files.side_effect = imageclean.psutil.AccessDenied(process.pid)
            service.name.return_value = "systemd"
            zombie.status.return_value = imageclean.psutil.STATUS_ZOMBIE
            with mock.patch.object(imageclean.psutil, "process_iter", return_value=iter([service, zombie])):
                self.assertEqual(imageclean.inspect_file_usage(Path(file.name))["status"], "not_found")
            service.open_files.assert_not_called()
            zombie.open_files.assert_not_called()

    def test_windows_empty_scan_is_unknown(self):
        with tempfile.NamedTemporaryFile() as file, mock.patch.object(imageclean.sys, "platform", "win32"), mock.patch.object(imageclean.psutil, "process_iter", return_value=iter([])):
            self.assertEqual(imageclean.inspect_file_usage(Path(file.name))["status"], "unknown")

    def test_inspection_failure_blocks_apply_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout.jsonl"
            path.write_text('{"type":"event_msg"}\n')
            with mock.patch.object(imageclean.psutil, "process_iter", side_effect=OSError("inspection failed")):
                with self.assertRaisesRegex(SystemExit, "Cannot verify"):
                    imageclean.clean_rollout(path, root / "backups", None)
            self.assertFalse((root / "backups").exists())

    @unittest.skipIf(os.name == "nt", "POSIX open-inode regression")
    def test_apply_refuses_open_rollout_and_preserves_future_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            original = json.dumps({"type": "event_msg", "payload": "data:image/png;base64,AAAA"}) + "\n"
            rollout.write_text(original)
            with rollout.open("a") as writer:
                with self.assertRaisesRegex(SystemExit, "open|Close"):
                    imageclean.clean_rollout(rollout, root / "backups", "thread-open")
                writer.write('{"type":"event_msg","payload":"after cleanup"}\n')
            self.assertEqual(rollout.read_text(), original + '{"type":"event_msg","payload":"after cleanup"}\n')
            self.assertFalse((root / "backups").exists())

    @unittest.skipIf(os.name == "nt", "POSIX open-inode regression")
    def test_writer_opened_during_cleanup_prevents_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            original = '{"type":"event_msg","payload":"data:image/png;base64,AAAA"}\n'
            rollout.write_text(original)
            validate = imageclean.validate_rollout
            writers = []

            def open_writer_after_validation(*args):
                result = validate(*args)
                writers.append(rollout.open("a"))
                return result

            try:
                with mock.patch.object(imageclean, "validate_rollout", side_effect=open_writer_after_validation):
                    with self.assertRaisesRegex(SystemExit, "open"):
                        imageclean.clean_rollout(rollout, root / "backups", None)
                writers[0].write('{"type":"event_msg","payload":"new"}\n')
            finally:
                for writer in writers:
                    writer.close()
            self.assertTrue(rollout.read_text().startswith(original))
            self.assertIn('"new"', rollout.read_text())
            self.assertFalse(list(root.glob("*.tmp-imageclean-*")))

    def test_repair_history_rebases_offsets_without_changing_other_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            records = [b'{"type":"session_meta"}\n', b'{"type":"event_msg"}\n', b'{"type":"response_item"}\n']
            rollout.write_bytes(b"".join(records))
            db = root / "thread_history_1.sqlite"
            with closing(sqlite3.connect(db)) as con:
                con.executescript("""
                    CREATE TABLE thread_turns (thread_id TEXT, rollout_ordinal INTEGER,
                        rollout_byte_offset INTEGER, rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER);
                    CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_ordinal INTEGER,
                        next_rollout_byte_offset INTEGER);
                    INSERT INTO thread_turns VALUES ('target',1,9000,1,10000),('other',1,9000,1,10000);
                    INSERT INTO thread_history_projection_state VALUES ('target',2,10000),('other',2,10000);
                """)
            con.close()
            imageclean.repair_history_index(rollout, db, "target", root / "backups")
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute("SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns WHERE thread_id='target'").fetchone(), (len(records[0]), len(records[0])+len(records[1])))
                self.assertEqual(con.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id='target'").fetchone()[0],len(records[0])+len(records[1]))
                self.assertEqual(con.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id='other'").fetchone()[0],10000)
            self.assertEqual(rollout.read_bytes(), b"".join(records))
            self.assertEqual(len(list((root / "backups").glob("*/thread_history_1.sqlite"))), 1)

    def test_apply_rebases_index_and_rolls_back_on_invalid_ordinal(self):
        for next_ordinal in (1, 999):
            with self.subTest(next_ordinal=next_ordinal), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rollout = root / "rollout.jsonl"
                original = '{"type":"event_msg","payload":"data:image/png;base64,AAAA"}\n'
                rollout.write_text(original)
                db = root / "history.sqlite"
                con = sqlite3.connect(db)
                con.executescript("""
                    CREATE TABLE thread_turns (thread_id TEXT, rollout_ordinal INTEGER,
                        rollout_byte_offset INTEGER, rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER);
                    CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_ordinal INTEGER,
                        next_rollout_byte_offset INTEGER);
                """)
                con.execute("INSERT INTO thread_history_projection_state VALUES ('target',?,9000)", (next_ordinal,))
                con.commit()
                con.close()
                if next_ordinal == 999:
                    with self.assertRaisesRegex(SystemExit, "outside rollout"):
                        imageclean.clean_rollout(rollout, root / "backups", "target", db)
                    self.assertEqual(rollout.read_text(), original)
                    self.assertEqual(len(list((root / "backups").glob("*/restore.sh"))), 1)
                else:
                    result = imageclean.clean_rollout(rollout, root / "backups", "target", db)
                    self.assertEqual(result["history_index_repair"]["next_rollout_byte_offset"], rollout.stat().st_size)
                    self.assertNotIn("data:image/", rollout.read_text())

    def test_apply_rejects_mismatched_thread_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text('{"type":"session_meta","payload":{"id":"actual"}}\n')
            with self.assertRaisesRegex(SystemExit, "does not match"):
                imageclean.clean_rollout(rollout, root / "backups", "wrong")
            self.assertFalse((root / "backups").exists())

    def test_apply_cleans_active_records_and_preserves_compacted_line_exactly(self):
        active = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect this"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                ],
            },
        }
        compacted = {
            "type": "compacted",
            "payload": {
                "replacement_history": [active["payload"]],
                "encrypted_content": "opaque-state",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            compacted_line = json.dumps(compacted, separators=(",", ":")) + "\n"
            rollout.write_text(json.dumps(active, separators=(",", ":")) + "\n" + compacted_line)

            result = imageclean.clean_rollout(rollout, root / "backups", "thread-1")
            lines = rollout.read_text().splitlines(keepends=True)

            self.assertNotIn("data:image/", lines[0])
            self.assertNotIn('"type":"input_image"', lines[0])
            self.assertEqual(lines[1], compacted_line)
            self.assertEqual(result["compacted_lines_preserved"], 1)

    def test_apply_removes_data_uri_but_preserves_innocent_marker_text(self):
        screenshot = {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "result": {"screenshot": {"url": "data:image/jpeg;base64,BBBB"}},
                "note": "Documentation mentions image_url and input_image fields.",
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text(json.dumps(screenshot, separators=(",", ":")) + "\n")

            imageclean.clean_rollout(rollout, root / "backups", "thread-2")
            cleaned = rollout.read_text()

            self.assertNotIn("data:image/", cleaned)
            self.assertIn("image_url and input_image fields", cleaned)


if __name__ == "__main__":
    unittest.main()
