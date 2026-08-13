import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "imageclean.py"
SPEC = importlib.util.spec_from_file_location("imageclean", SCRIPT)
imageclean = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imageclean)


class ImageCleanTests(unittest.TestCase):
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
