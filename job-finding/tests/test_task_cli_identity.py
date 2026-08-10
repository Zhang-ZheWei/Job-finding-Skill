from __future__ import annotations

import copy
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
from test_task_config import payload  # noqa: E402


class TaskCliIdentityTests(unittest.TestCase):
    def test_two_tasks_keep_different_s0_and_s1_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory)
            task_b = create_task(directory)
            config_a = payload()
            config_b = copy.deepcopy(config_a)
            config_b["job_target"]["search_keywords"][0]["term"] = "业务分析"
            result_a = prepare(task_a["run_root"], config_a, task_a["task_id"])
            result_b = prepare(task_b["run_root"], config_b, task_b["task_id"])
            self.assertNotEqual(result_a["config_hash"], result_b["config_hash"])

            outputs = []
            for task in (task_a, task_b):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT_DIR / "s1_store.py"),
                        "next",
                        "--run-root",
                        task["run_root"],
                        "--task-id",
                        task["task_id"],
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                outputs.append(json.loads(completed.stdout))
            self.assertEqual("产品经理", outputs[0]["next_combo"]["term"])
            self.assertEqual("业务分析", outputs[1]["next_combo"]["term"])

    def test_every_stage_rejects_task_b_id_with_task_a_directory(self) -> None:
        cases = [
            ("task_config.py", ["validate"]),
            ("s1_store.py", ["next"]),
            ("collect_s1.py", []),
            ("s2_store.py", ["pending"]),
            ("read_s2.py", []),
            ("s3_store.py", ["pending"]),
            ("s4_store.py", ["pending"]),
            ("s5_store.py", ["pending"]),
            ("s6_report.py", ["build"]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory)
            task_b = create_task(directory)
            for script_name, prefix in cases:
                with self.subTest(script=script_name):
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT_DIR / script_name),
                            *prefix,
                            "--run-root",
                            task_a["run_root"],
                            "--task-id",
                            task_b["task_id"],
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(2, completed.returncode)
                    self.assertIn("task_id_mismatch", completed.stderr)

    def test_every_task_command_requires_task_id(self) -> None:
        cases = [
            ("task_config.py", "validate"),
            ("s1_store.py", "next"),
            ("s2_store.py", "pending"),
            ("s3_store.py", "pending"),
            ("s4_store.py", "pending"),
            ("s5_store.py", "pending"),
            ("s6_report.py", "build"),
        ]
        for script_name, command in cases:
            with self.subTest(script=script_name, command=command):
                completed = subprocess.run(
                    [sys.executable, str(SCRIPT_DIR / script_name), command, "--run-root", "/tmp/not-used"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn("--task-id", completed.stderr)

    def test_browser_entry_commands_require_task_id(self) -> None:
        for script_name in ("collect_s1.py", "read_s2.py"):
            with self.subTest(script=script_name):
                completed = subprocess.run(
                    [sys.executable, str(SCRIPT_DIR / script_name), "--run-root", "/tmp/not-used"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn("--task-id", completed.stderr)

    def test_stateless_url_inspection_does_not_require_task_id(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "task_config.py"),
                "inspect-url",
                "--url",
                "https://www.zhipin.com/web/geek/jobs?city=101010100&query=test",
                "--city-label",
                "测试城市",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn('"ok": true', completed.stdout)


if __name__ == "__main__":
    unittest.main()
