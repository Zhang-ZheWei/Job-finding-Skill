from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from task_manager import (  # noqa: E402
    DEFAULT_TASKS_DIR_NAME,
    bind_config,
    create_task,
    default_tasks_root,
    list_tasks,
    resolve_tasks_root,
    validate_task,
)


TZ = timezone(timedelta(hours=8))
TIME_A = datetime(2026, 8, 6, 9, 30, 15, 123456, tzinfo=TZ)
TIME_B = datetime(2026, 8, 6, 10, 45, 20, 654321, tzinfo=TZ)


class TaskManagerTests(unittest.TestCase):
    def test_resolves_macos_default_under_documents_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = resolve_tasks_root(system_name="Darwin", home=home)
            expected = home / "Documents" / DEFAULT_TASKS_DIR_NAME
            self.assertEqual(str(expected.resolve()), result["tasks_root"])
            self.assertEqual("macOS", result["platform"])
            self.assertEqual("default", result["source"])
            self.assertFalse(result["created"])
            self.assertFalse(expected.exists())

    def test_windows_default_uses_known_documents_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            documents = Path(directory) / "OneDrive" / "Documents"
            result = resolve_tasks_root(
                system_name="Windows",
                windows_documents=documents,
            )
            self.assertEqual(
                str((documents / DEFAULT_TASKS_DIR_NAME).resolve()),
                result["tasks_root"],
            )
            self.assertEqual("Windows", result["platform"])
            self.assertFalse(Path(result["tasks_root"]).exists())

    def test_default_root_rejects_unsupported_system(self) -> None:
        with self.assertRaises(S1Error) as caught:
            default_tasks_root(system_name="Linux")
        self.assertEqual("unsupported_default_tasks_root", caught.exception.code)

    def test_custom_root_works_without_supported_default_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "my-tasks"
            result = resolve_tasks_root(str(custom), system_name="Linux")
            self.assertEqual(str(custom.resolve()), result["tasks_root"])
            self.assertEqual("custom", result["source"])
            self.assertFalse(custom.exists())

    def test_cli_resolve_root_does_not_create_directory(self) -> None:
        script = SCRIPT_DIR / "task_manager.py"
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "preview-only"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "resolve-root",
                    "--tasks-root",
                    str(custom),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(str(custom.resolve()), result["tasks_root"])
            self.assertFalse(result["created"])
            self.assertFalse(custom.exists())

    def test_cli_create_and_validate(self) -> None:
        script = SCRIPT_DIR / "task_manager.py"
        with tempfile.TemporaryDirectory() as directory:
            created_process = subprocess.run(
                [sys.executable, str(script), "create", "--tasks-root", directory],
                check=True,
                capture_output=True,
                text=True,
            )
            created = json.loads(created_process.stdout)
            validated_process = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    "--run-root",
                    created["run_root"],
                    "--task-id",
                    created["task_id"],
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            validated = json.loads(validated_process.stdout)
            self.assertTrue(validated["ok"])
            self.assertEqual(created["task_id"], validated["task_id"])
            self.assertEqual(str(Path(directory).resolve()), created["tasks_root"])
            self.assertEqual(str(Path(directory).resolve()), validated["tasks_root"])

    def test_creates_timestamp_task_without_user_label(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = create_task(directory, now=TIME_A)
            self.assertEqual("task-20260806-093015-123456", result["task_id"])
            self.assertEqual(result["task_id"], Path(result["run_root"]).name)
            task = load_json(Path(result["run_root"]) / "job-research-data" / "task.json")
            self.assertEqual(
                {"schema_version", "task_id", "created_at", "config_hash"},
                set(task),
            )
            self.assertIsNone(task["config_hash"])

    def test_binds_config_once_and_allows_same_hash_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory, now=TIME_A)
            config_hash = "a" * 64
            first = bind_config(task["run_root"], task["task_id"], config_hash)
            second = bind_config(task["run_root"], task["task_id"], config_hash)
            self.assertEqual(config_hash, first["config_hash"])
            self.assertEqual(first, second)

    def test_refuses_rebinding_task_to_different_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory, now=TIME_A)
            bind_config(task["run_root"], task["task_id"], "a" * 64)
            with self.assertRaises(S1Error) as caught:
                bind_config(task["run_root"], task["task_id"], "b" * 64)
            self.assertEqual("task_config_conflict", caught.exception.code)

    def test_new_tasks_have_independent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory, now=TIME_A)
            task_b = create_task(directory, now=TIME_B)
            self.assertNotEqual(task_a["task_id"], task_b["task_id"])
            self.assertNotEqual(task_a["run_root"], task_b["run_root"])
            self.assertTrue(Path(task_a["run_root"]).is_dir())
            self.assertTrue(Path(task_b["run_root"]).is_dir())

    def test_same_timestamp_never_reuses_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = create_task(directory, now=TIME_A)
            with self.assertRaisesRegex(S1Error, "拒绝复用"):
                create_task(directory, now=TIME_A)
            self.assertTrue(Path(first["run_root"]).is_dir())

    def test_rejects_task_b_id_with_task_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory, now=TIME_A)
            task_b = create_task(directory, now=TIME_B)
            with self.assertRaises(S1Error) as caught:
                validate_task(task_a["run_root"], task_b["task_id"])
            self.assertEqual("task_id_mismatch", caught.exception.code)

    def test_rejects_tampered_task_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory, now=TIME_A)
            identity_path = Path(task_a["run_root"]) / "job-research-data" / "task.json"
            identity = load_json(identity_path)
            identity["task_id"] = "task-20260806-104520-654321"
            atomic_write_json(identity_path, identity)
            with self.assertRaises(S1Error):
                validate_task(task_a["run_root"], task_a["task_id"])

    def test_rejects_directory_renamed_away_from_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory, now=TIME_A)
            renamed = Path(directory) / "task-20260806-104520-654321"
            Path(task["run_root"]).rename(renamed)
            with self.assertRaises(S1Error):
                validate_task(str(renamed), task["task_id"])

    def test_list_never_auto_selects_even_single_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory, now=TIME_A)
            result = list_tasks(directory)
            self.assertEqual(1, result["task_count"])
            self.assertEqual(task["task_id"], result["tasks"][0]["task_id"])
            self.assertIsNone(result["selected_task_id"])
            self.assertTrue(result["selection_required"])

    def test_list_orders_tasks_but_does_not_select_latest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory, now=TIME_A)
            task_b = create_task(directory, now=TIME_B)
            result = list_tasks(directory)
            self.assertEqual([task_b["task_id"], task_a["task_id"]], [item["task_id"] for item in result["tasks"]])
            self.assertIsNone(result["selected_task_id"])

    def test_list_surfaces_invalid_timestamp_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "task-20260806-093015-123456"
            invalid.mkdir()
            result = list_tasks(directory)
            self.assertEqual(0, result["task_count"])
            self.assertEqual(1, len(result["invalid_tasks"]))

    def test_list_missing_custom_root_does_not_create_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-created-by-list"
            result = list_tasks(str(missing))
            self.assertEqual(str(missing.resolve()), result["tasks_root"])
            self.assertEqual(0, result["task_count"])
            self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
