import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "src" / "hooks" / "caveman_gate.py"

LONG = "This sentence has far too many words in it for the ultra level of caveman to allow at all."
SHORT = "Merged. CI green."


class CavemanGateTests(unittest.TestCase):
    def run_gate(self, message, remote="true", level="ultra", gate=None, stop_hook_active=False):
        with tempfile.TemporaryDirectory(prefix="caveman-gate-") as tmp:
            claude_dir = Path(tmp)
            if level is not None:
                (claude_dir / ".caveman-active").write_text(level, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in {"CLAUDE_CODE_REMOTE", "CAVEMAN_GATE"}}
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
            if remote is not None:
                env["CLAUDE_CODE_REMOTE"] = remote
            if gate is not None:
                env["CAVEMAN_GATE"] = gate
            payload = {"last_assistant_message": message, "stop_hook_active": stop_hook_active}
            return subprocess.run(
                [sys.executable, str(GATE)],
                input=json.dumps(payload),
                env=env,
                text=True,
                capture_output=True,
            )

    def test_blocks_long_sentence_in_cloud(self):
        result = self.run_gate(LONG)
        self.assertEqual(result.returncode, 2)
        self.assertIn("CAVEMAN GATE BLOCKED (level=ultra)", result.stderr)
        self.assertIn("(max 15)", result.stderr)

    def test_passes_short_reply(self):
        self.assertEqual(self.run_gate(SHORT).returncode, 0)

    def test_skips_outside_cloud(self):
        result = self.run_gate(LONG, remote=None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")

    def test_force_on_and_off(self):
        self.assertEqual(self.run_gate(LONG, remote=None, gate="1").returncode, 2)
        self.assertEqual(self.run_gate(LONG, gate="0").returncode, 0)

    def test_code_blocks_and_tables_exempt(self):
        message = SHORT + "\n\n```\n" + LONG + "\n```\n\n| " + LONG + " |\n"
        self.assertEqual(self.run_gate(message).returncode, 0)

    def test_off_level_skips(self):
        self.assertEqual(self.run_gate(LONG, level="off").returncode, 0)

    def test_missing_flag_enforces_ultra(self):
        self.assertEqual(self.run_gate(LONG, level=None).returncode, 2)

    def test_second_block_only_warns(self):
        result = self.run_gate(LONG, stop_hook_active=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("CAVEMAN GATE WARNING", result.stderr)


if __name__ == "__main__":
    unittest.main()
