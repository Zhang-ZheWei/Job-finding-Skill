#!/usr/bin/env python3
"""从 S0 配置取得下一个 S1 组合，运行浏览器采集器并合并结果。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from s1_common import S1Error, strict_json_loads, validate_search_url
from s1_store import merge_run_documents, next_combination, write_documents
from task_manager import validate_task


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--run-root", required=True)
    root.add_argument("--task-id", required=True)
    root.add_argument("--url")
    root.add_argument("--city")
    mode = root.add_mutually_exclusive_group()
    mode.add_argument("--limit", type=int)
    mode.add_argument("--exhaustive", action="store_true")
    root.add_argument("--node-bin", default=shutil.which("node") or "node")
    root.add_argument("--adapter")
    root.add_argument("--existing-target")
    root.add_argument("--initial-visible-count", type=int)
    root.add_argument("--scroll-rounds", type=int)
    return root


def main() -> int:
    args = parser().parse_args()
    validate_task(args.run_root, args.task_id)
    direct_mode = any(value is not None for value in (args.url, args.city, args.limit)) or args.exhaustive
    if direct_mode:
        if not args.url or not args.city:
            raise S1Error("单 URL 验收模式必须同时提供 --url 和 --city", "invalid_direct_mode")
        search_url, term = validate_search_url(args.url)
        city = args.city
        exhaustive = args.exhaustive
        limit = 20 if args.limit is None and not exhaustive else args.limit
    else:
        state = next_combination(args.run_root)
        combo = state["next_combo"]
        if combo is None:
            if state.get("workflow_state") == "s1_complete":
                print(json.dumps({**state, "ok": True, "status": "s1_complete"}, ensure_ascii=False, sort_keys=True))
                return 0
            raise S1Error("当前采集批次必须先完成 S2 和 S3，再继续下一个搜索组合", "downstream_batch_incomplete")
        search_url, term = validate_search_url(combo["search_url"])
        city = combo["city_label"]
        exhaustive = combo["collection_mode"] == "exhaustive"
        limit = combo["limit"]
    if not exhaustive and (limit is None or limit < 1):
        raise S1Error("限量必须是正整数", "invalid_limit")
    observed = (args.initial_visible_count, args.scroll_rounds)
    if args.existing_target:
        if any(value is None for value in observed):
            raise S1Error(
                "existing target requires initial visible count and scroll rounds",
                "invalid_existing_target",
            )
    elif any(value is not None for value in observed):
        raise S1Error(
            "scroll evidence is only valid with an existing target",
            "invalid_existing_target",
        )
    node_bin = Path(args.node_bin)
    adapter = Path(args.adapter) if args.adapter else Path(__file__).with_name("boss_collect_s1.mjs")

    version = subprocess.run(
        [str(node_bin), "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not version.startswith("v24."):
        raise S1Error(f"Node 24 is required, got {version}", "invalid_node_version")

    command = [
            str(node_bin),
            str(adapter),
            "--url", search_url,
            "--city", city,
            "--term", term,
            "--mode", "exhaustive" if exhaustive else "sample",
            "--limit", str(limit or 0),
    ]
    if args.existing_target:
        command.extend([
            "--existing-target", args.existing_target,
            "--initial-visible-count", str(args.initial_visible_count),
            "--scroll-rounds", str(args.scroll_rounds),
        ])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "browser adapter failed"
        raise S1Error(message, "browser_collection_failed")
    payload = strict_json_loads(completed.stdout)
    if not isinstance(payload, dict):
        raise S1Error("browser adapter output must be an object", "invalid_adapter_output")
    if payload.get("status") == "manual_scroll_required":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    result = write_documents(args.run_root, payload) if direct_mode else merge_run_documents(args.run_root, payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (S1Error, OSError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, S1Error) else "collector_failed"
        message = exc.message if isinstance(exc, S1Error) else str(exc)
        print(json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
