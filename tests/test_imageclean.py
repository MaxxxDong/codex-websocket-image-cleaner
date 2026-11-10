import importlib.util
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "imageclean.py"
SPEC = importlib.util.spec_from_file_location("imageclean", SCRIPT)
imageclean = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imageclean)


class ImageCleanTests(unittest.TestCase):
    def test_unrelated_mount_warnings_are_classified_by_device(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "rollout"
            target.touch()
            device = f"{os.major(target.stat().st_dev)}:{os.minor(target.stat().st_dev)}"
            mounts = "1 0 999:998 / /run/user/120/gvfs rw - fuse.gvfsd-fuse gvfs rw\n"
            mounts += f"2 0 {device} / /same-device rw - ext4 source rw\n"
            warning = "lsof: WARNING: can't stat() fuse.gvfsd-fuse file system /run/user/120/gvfs\n      Output information may be incomplete.\n"
            with mock.patch.object(Path, "read_text", return_value=mounts):
                self.assertEqual(imageclean.lsof_exclusions(target, warning), ["/run/user/120/gvfs"])
                self.assertEqual(imageclean.lsof_exclusions(target, warning + "other warning\n"), [])
                self.assertEqual(imageclean.lsof_exclusions(target, warning.replace("/run/user/120/gvfs", "/same-device")), [])
                self.assertEqual(imageclean.lsof_exclusions(target, warning.replace("/run/user/120/gvfs", "/unknown")), [])
                second = warning.replace("/run/user/120/gvfs", "/tmp/remote share")
                with mock.patch.object(Path, "read_text", return_value=mounts + "3 0 999:997 / /tmp/remote\\040share rw - nfs remote rw\n"):
                    self.assertEqual(imageclean.lsof_exclusions(target, warning + second), ["/run/user/120/gvfs", "/tmp/remote share"])
            with mock.patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertEqual(imageclean.lsof_exclusions(target, warning), [])

    def test_lsof_mount_retry_preserves_error_guards(self):
        result = imageclean.subprocess.CompletedProcess
        for retry, error in [(result([], 1, "", ""), None), (result([], 0, "p123\n", ""), "open"), (result([], 1, "", "other warning"), "lsof failed"), (result([], 2, "", ""), "lsof failed")]:
            with self.subTest(retry=retry), mock.patch.object(imageclean.shutil, "which", return_value="lsof"), mock.patch.object(imageclean, "lsof_exclusions", return_value=["/unrelated"]), mock.patch.object(imageclean.subprocess, "run", side_effect=[result([], 1, "", "warning"), retry]) as run:
                if error:
                    with self.assertRaisesRegex(SystemExit, error):
                        imageclean.assert_rollout_closed(Path("/target"))
                else:
                    imageclean.assert_rollout_closed(Path("/target"))
                self.assertIn("/unrelated", run.call_args.args[0])
                self.assertEqual(run.call_count, 2)

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

    @unittest.skipIf(os.name == "nt", "POSIX lsof guard")
    def test_missing_lsof_refuses_before_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / "rollout.jsonl"
            rollout.write_text('{"type":"event_msg"}\n')
            with mock.patch.object(imageclean.shutil, "which", return_value=None):
                with self.assertRaisesRegex(SystemExit, "install lsof"):
                    imageclean.clean_rollout(rollout, root / "backups", None)
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
            with sqlite3.connect(db) as con:
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
            with sqlite3.connect(db) as con:
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
