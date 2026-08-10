#!/usr/bin/env python3
"""创建、列出并校验相互隔离的岗位搜索任务目录。"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import uuid
from ctypes import POINTER, Structure, byref, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from s1_common import S1Error, atomic_write_json, load_json


SCHEMA_VERSION = 1
TASK_ID_PATTERN = re.compile(r"^task-(\d{8})-(\d{6})-(\d{6})$")
CONFIG_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TASK_FIELDS = {"schema_version", "task_id", "created_at", "config_hash"}
DEFAULT_TASKS_DIR_NAME = "Codex岗位搜索任务"
WINDOWS_DOCUMENTS_FOLDER_ID = "fdd39ad0-238f-46af-adb4-6c85480369c7"


class _WindowsGuid(Structure):
    _fields_ = [
        ("data1", c_ulong),
        ("data2", c_ushort),
        ("data3", c_ushort),
        ("data4", c_ubyte * 8),
    ]


def _windows_documents_dir() -> Path:
    """通过 Windows Known Folder API 获取可能被 OneDrive 重定向的文档目录。"""
    try:
        import ctypes

        folder_id = _WindowsGuid.from_buffer_copy(
            uuid.UUID(WINDOWS_DOCUMENTS_FOLDER_ID).bytes_le
        )
        value = c_wchar_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [
            POINTER(_WindowsGuid),
            c_ulong,
            c_void_p,
            POINTER(c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = c_ulong
        result = shell32.SHGetKnownFolderPath(byref(folder_id), 0, None, byref(value))
        if result != 0 or not value.value:
            raise OSError(f"SHGetKnownFolderPath 返回 {result}")
        documents = Path(value.value)
        ole32.CoTaskMemFree(value)
        return documents
    except (AttributeError, OSError, ValueError) as exc:
        raise S1Error(
            "无法取得 Windows 文档目录，请由用户指定任务保存目录",
            "documents_root_unavailable",
        ) from exc


def default_tasks_root(
    *,
    system_name: str | None = None,
    home: Path | None = None,
    windows_documents: Path | None = None,
) -> Path:
    """返回当前系统易于用户查找的默认任务根目录。"""
    detected = system_name or platform.system()
    if detected == "Darwin":
        documents = (home or Path.home()) / "Documents"
    elif detected == "Windows":
        documents = windows_documents or _windows_documents_dir()
    else:
        raise S1Error(
            f"当前系统 {detected or 'unknown'} 没有预设默认目录，请由用户指定",
            "unsupported_default_tasks_root",
        )
    return documents.expanduser() / DEFAULT_TASKS_DIR_NAME


def _platform_label(system_name: str) -> str:
    if system_name == "Darwin":
        return "macOS"
    if system_name == "Windows":
        return "Windows"
    return system_name or "unknown"


def _normal_tasks_root(tasks_root: str, *, create: bool) -> Path:
    if not isinstance(tasks_root, str) or not tasks_root.strip():
        raise S1Error("tasks_root 不能为空", "invalid_tasks_root")
    candidate = Path(tasks_root).expanduser()
    if candidate.exists() and candidate.is_symlink():
        raise S1Error("tasks_root 必须是普通目录且不能是符号链接", "unsafe_tasks_root")
    root = candidate.resolve()
    if root.exists() and not root.is_dir():
        raise S1Error("tasks_root 必须是普通目录且不能是符号链接", "unsafe_tasks_root")
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise S1Error(
                "tasks_root 当前不可写；请先取得该目录的写入授权，授权后仍失败再更换目录",
                "tasks_root_permission_denied",
            ) from exc
        except OSError as exc:
            raise S1Error("无法创建 tasks_root，请由用户指定其他目录", "invalid_tasks_root") from exc
    return root


def resolve_tasks_root(
    tasks_root: str | None = None,
    *,
    system_name: str | None = None,
    home: Path | None = None,
    windows_documents: Path | None = None,
    create: bool = False,
) -> dict[str, Any]:
    """解析默认或用户指定目录；仅在 create=True 时创建目录。"""
    detected = system_name or platform.system()
    source = "custom" if isinstance(tasks_root, str) and tasks_root.strip() else "default"
    proposed = (
        tasks_root
        if source == "custom"
        else str(
            default_tasks_root(
                system_name=detected,
                home=home,
                windows_documents=windows_documents,
            )
        )
    )
    root = _normal_tasks_root(proposed, create=create)
    probe = root if root.exists() else root.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = probe.is_dir() and os.access(probe, os.W_OK)
    return {
        "ok": True,
        "platform": _platform_label(detected),
        "source": source,
        "tasks_root": str(root),
        "exists": root.exists(),
        "writable": writable,
        "created": create,
    }


def _normal_moment(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise S1Error("任务时间必须是 datetime", "invalid_task_time")
    if value.tzinfo is None or value.utcoffset() is None:
        raise S1Error("任务时间必须包含时区", "invalid_task_time")
    return value


def _task_id(moment: datetime) -> str:
    return f"task-{moment.strftime('%Y%m%d-%H%M%S-%f')}"


def _validate_task_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise S1Error("task.json 必须是对象", "invalid_task_identity")
    unexpected = set(value) - TASK_FIELDS
    missing = TASK_FIELDS - set(value)
    if unexpected or missing:
        raise S1Error(
            f"task.json 字段不正确；多余={sorted(unexpected)}，缺少={sorted(missing)}",
            "invalid_task_identity",
        )
    if value["schema_version"] != SCHEMA_VERSION:
        raise S1Error("task.json schema_version 无效", "invalid_task_identity")
    task_id = value["task_id"]
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise S1Error("task_id 不是合法时间戳标识", "invalid_task_id")
    created_at = value["created_at"]
    if not isinstance(created_at, str):
        raise S1Error("created_at 必须是带时区时间", "invalid_task_identity")
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise S1Error("created_at 不是合法 ISO 8601 时间", "invalid_task_identity") from exc
    _normal_moment(moment)
    if _task_id(moment) != task_id:
        raise S1Error("task_id 与 created_at 不一致", "task_time_mismatch")
    config_hash = value["config_hash"]
    if config_hash is not None and (
        not isinstance(config_hash, str) or not CONFIG_HASH_PATTERN.fullmatch(config_hash)
    ):
        raise S1Error("config_hash 必须为 null 或 64 位小写 SHA-256", "invalid_task_identity")
    return value


def create_task(
    tasks_root: str | None = None,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """创建一个仅由当前时间戳命名的新任务目录。"""
    resolved_root = resolve_tasks_root(tasks_root, create=True)
    root = Path(resolved_root["tasks_root"])
    if now is not None and clock is not None:
        raise S1Error("now 与 clock 不能同时提供", "invalid_task_time")
    clock = clock or (lambda: datetime.now().astimezone())
    maximum_attempts = 1 if now is not None else 100

    for _ in range(maximum_attempts):
        moment = _normal_moment(now if now is not None else clock())
        task_id = _task_id(moment)
        run_root = root / task_id
        try:
            run_root.mkdir(mode=0o700)
        except FileExistsError:
            if now is not None:
                break
            continue

        document = {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "created_at": moment.isoformat(timespec="microseconds"),
            "config_hash": None,
        }
        try:
            atomic_write_json(run_root / "job-research-data" / "task.json", document)
        except Exception:
            data_dir = run_root / "job-research-data"
            try:
                data_dir.rmdir()
            except OSError:
                pass
            try:
                run_root.rmdir()
            except OSError:
                pass
            raise
        return {
            "ok": True,
            "task_id": task_id,
            "created_at": document["created_at"],
            "tasks_root": str(root),
            "run_root": str(run_root),
        }

    raise S1Error("当前时间戳对应的任务目录已存在，拒绝复用", "task_id_conflict")


def validate_task(run_root: str, task_id: str) -> dict[str, Any]:
    """校验命令携带的 task_id、目录名和 task.json 三者完全一致。"""
    if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
        raise S1Error("task_id 不是合法时间戳标识", "invalid_task_id")
    path = Path(run_root).expanduser()
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise S1Error("run_root 必须是已存在的普通任务目录", "invalid_run_root")
    path = path.resolve()
    if path.name != task_id:
        raise S1Error("命令中的 task_id 与任务目录不一致", "task_id_mismatch")
    identity_path = path / "job-research-data" / "task.json"
    if identity_path.is_symlink():
        raise S1Error("task.json 不能是符号链接", "unsafe_task_identity")
    document = _validate_task_document(load_json(identity_path))
    if document["task_id"] != task_id:
        raise S1Error("命令中的 task_id 与 task.json 不一致", "task_id_mismatch")
    return {
        "ok": True,
        "task_id": task_id,
        "created_at": document["created_at"],
        "tasks_root": str(path.parent),
        "run_root": str(path),
        "config_hash": document["config_hash"],
        "config_bound": document["config_hash"] is not None,
    }


def bind_config(run_root: str, task_id: str, config_hash: str) -> dict[str, Any]:
    """将 S0 配置哈希一次性绑定到任务身份；相同值允许幂等重试。"""
    if not isinstance(config_hash, str) or not CONFIG_HASH_PATTERN.fullmatch(config_hash):
        raise S1Error("config_hash 必须是 64 位小写 SHA-256", "invalid_config_hash")
    current = validate_task(run_root, task_id)
    existing = current["config_hash"]
    if existing is not None and existing != config_hash:
        raise S1Error("当前任务已经绑定其他 S0 配置，拒绝覆盖", "task_config_conflict")
    if existing is None:
        path = Path(current["run_root"]) / "job-research-data" / "task.json"
        document = _validate_task_document(load_json(path))
        document["config_hash"] = config_hash
        atomic_write_json(path, document)
    return validate_task(run_root, task_id)


def list_tasks(tasks_root: str | None = None) -> dict[str, Any]:
    """列出可选任务，但永远不替用户选择任务。"""
    resolved_root = resolve_tasks_root(tasks_root)
    root = Path(resolved_root["tasks_root"])
    tasks: list[dict[str, Any]] = []
    invalid_tasks: list[dict[str, str]] = []
    paths = root.iterdir() if root.exists() else []
    for path in sorted(paths, key=lambda item: item.name, reverse=True):
        if not path.name.startswith("task-"):
            continue
        try:
            tasks.append(validate_task(str(path), path.name))
        except S1Error as exc:
            invalid_tasks.append({
                "run_root": str(path),
                "error": exc.code,
                "message": exc.message,
            })
    tasks.sort(key=lambda item: item["created_at"], reverse=True)
    return {
        "ok": True,
        "tasks_root": str(root),
        "task_count": len(tasks),
        "tasks": tasks,
        "invalid_tasks": invalid_tasks,
        "selected_task_id": None,
        "selection_required": bool(tasks),
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    resolve_parser = sub.add_parser("resolve-root")
    resolve_parser.add_argument("--tasks-root")
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--tasks-root")
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--tasks-root")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--run-root", required=True)
    validate_parser.add_argument("--task-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "resolve-root":
        result = resolve_tasks_root(args.tasks_root)
    elif args.command == "create":
        result = create_task(args.tasks_root)
    elif args.command == "list":
        result = list_tasks(args.tasks_root)
    else:
        result = validate_task(args.run_root, args.task_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except S1Error as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
