#!/usr/bin/env python3
"""从 S1 岗位索引中选择一个岗位，并调用浏览器适配器读取临时详情。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from s1_common import S1Error, load_json, strict_json_loads
from s2_store import pending
from task_manager import validate_task


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--run-root", required=True)
    root.add_argument("--task-id", required=True)
    root.add_argument("--job-key")
    root.add_argument("--node-bin", default=shutil.which("node") or "node")
    root.add_argument("--adapter")
    return root


def main() -> int:
    args = parser().parse_args()
    validate_task(args.run_root, args.task_id)
    data_dir = Path(args.run_root) / "job-research-data"
    pending_jobs = pending(args.run_root, 1)["pending"]
    if not pending_jobs:
        print(json.dumps({"ok": True, "status": "s2_complete"}, ensure_ascii=False, sort_keys=True))
        return 0
    next_job_key = pending_jobs[0]["job_key"]
    if args.job_key is not None and args.job_key != next_job_key:
        raise S1Error("--job-key 只能等于首个待处理岗位", "job_order_mismatch")
    job_key = next_job_key
    job_index = load_json(data_dir / "job-index.json")
    records = job_index.get("records")
    if not isinstance(records, list):
        raise S1Error("job-index.json 缺少 records 数组", "invalid_job_index")
    matches = [record for record in records if isinstance(record, dict) and record.get("job_key") == job_key]
    if len(matches) != 1:
        raise S1Error(f"无法唯一找到岗位：{job_key}", "job_not_found")
    job = matches[0]

    known_company_urls: list[str] = []
    details_path = data_dir / "job-details.json"
    if details_path.exists():
        details = load_json(details_path)
        for record in details.get("records", []):
            if isinstance(record, dict) and record.get("record_type") == "boss_company_subject":
                url = record.get("company_page_url")
                if isinstance(url, str):
                    known_company_urls.append(url)

    node_bin = Path(args.node_bin)
    version = subprocess.run(
        [str(node_bin), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if not version.startswith("v24."):
        raise S1Error(f"必须使用 Node 24，当前为 {version}", "invalid_node_version")
    adapter = Path(args.adapter) if args.adapter else Path(__file__).with_name("boss_read_s2.mjs")
    completed = subprocess.run(
        [
            str(node_bin),
            str(adapter),
            "--url", str(job.get("boss_job_url", "")),
            "--job-id", str(job.get("job_id", "")),
            "--job-key", job_key,
            "--known-company-urls", json.dumps(sorted(set(known_company_urls)), ensure_ascii=False),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "浏览器适配器失败"
        raise S1Error(message, "browser_detail_failed")
    payload = strict_json_loads(completed.stdout)
    if not isinstance(payload, dict) or payload.get("job_key") != job_key:
        raise S1Error("浏览器结果与请求岗位不一致", "invalid_browser_payload")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (S1Error, OSError, subprocess.SubprocessError) as exc:
        code = exc.code if isinstance(exc, S1Error) else "detail_reader_failed"
        message = exc.message if isinstance(exc, S1Error) else str(exc)
        print(json.dumps({"ok": False, "error": code, "message": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
