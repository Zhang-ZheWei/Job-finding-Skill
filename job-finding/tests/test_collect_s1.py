from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

from task_config import prepare  # noqa: E402
from task_manager import create_task  # noqa: E402
from test_task_config import payload as config_payload  # noqa: E402


class CollectS1Tests(unittest.TestCase):
    def _fixture(self, directory: str) -> tuple[dict, Path, Path]:
        task = create_task(directory)
        prepare(task["run_root"], config_payload(), task["task_id"])
        root = Path(directory)
        adapter = root / "fake-adapter.mjs"
        adapter.write_text("// fake adapter\n", encoding="utf-8")
        fake_node = root / "fake-node"
        manual_result = {
            "ok": False,
            "status": "manual_scroll_required",
            "target_id": "test-target",
            "initial_visible_count": 15,
            "observed_unique_jobs": 15,
        }
        fake_node.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf '%s\\n' 'v24.0.0'\n"
            "else\n"
            f"  printf '%s\\n' '{json.dumps(manual_result, ensure_ascii=False)}'\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_node.chmod(0o700)
        return task, adapter, fake_node

    def _run(self, task: dict, adapter: Path, fake_node: Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "collect_s1.py"),
                "--run-root", task["run_root"],
                "--task-id", task["task_id"],
                "--adapter", str(adapter),
                "--node-bin", str(fake_node),
                *extra,
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_manual_scroll_required_does_not_write_completed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, adapter, fake_node = self._fixture(directory)
            completed = self._run(task, adapter, fake_node, [])
            self.assertEqual(0, completed.returncode)
            self.assertEqual("manual_scroll_required", json.loads(completed.stdout)["status"])
            data_dir = Path(task["run_root"]) / "job-research-data"
            self.assertFalse((data_dir / "job-index.json").exists())
            self.assertFalse((data_dir / "checkpoint.json").exists())

    def test_exhaustive_task_accepts_confirmed_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, adapter, fake_node = self._fixture(directory)
            completed = self._run(task, adapter, fake_node, [
                "--existing-target", "test-target",
                "--initial-visible-count", "15",
                "--scroll-rounds", "1",
            ])
            self.assertEqual(0, completed.returncode)
            self.assertEqual("manual_scroll_required", json.loads(completed.stdout)["status"])
            self.assertNotIn("invalid_existing_target", completed.stderr)


if __name__ == "__main__":
    unittest.main()
