#!/usr/bin/env python3
"""校验模型初筛结论，并原子写入 screening-results.json。"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
from pathlib import Path
from typing import Any

from s1_common import S1Error, atomic_write_json, load_json, normalized_text, sha256_text, strict_json_loads
from s2_store import validate_document as validate_s2_document
from task_config import validate_config


SCHEMA_VERSION = 2
STATUSES = {"初筛通过", "可能无关", "淘汰", "无法判断"}
REVIEW_LEVELS = {"无需复核", "建议复核", "必须复核"}
MODEL_FIELDS = {"job_key", "status", "reason", "evidence_ids", "items_to_verify", "reporting"}
PROHIBITED_FIELDS = {"confidence", "coverage", "review_level", "revision", "detail_record_hash", "config_hash"}


def _canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _normal_list(value: Any, field: str, *, maximum: int = 12) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise S1Error(f"{field} 必须是最多 {maximum} 项的数组", "invalid_screening_payload")
    result = [normalized_text(item, f"{field}[]") for item in value]
    if len(set(result)) != len(result):
        raise S1Error(f"{field} 含重复内容", "invalid_screening_payload")
    return result


def _screening_context(config: dict[str, Any]) -> dict[str, Any]:
    """从已校验的 S0 配置生成只读筛选视图，不复制成新的事实文件。"""
    target = config["job_target"]
    return {
        "candidate_profile": config["candidate_profile"],
        "target_work_features": target["desired_work_features"],
        "hard_exclusions": target["hard_exclusions"],
        "soft_preferences": target["soft_preferences"],
        "target_directions": target["target_directions"],
    }


def _load_inputs(run_root: str) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]],
    dict[str, dict[str, Any]], str, str,
]:
    data_dir = Path(run_root) / "job-research-data"
    config = validate_config(load_json(data_dir / "config.json"))
    job_index = load_json(data_dir / "job-index.json")
    job_details = load_json(data_dir / "job-details.json")
    s2_status = validate_s2_document(job_details, job_index)
    if s2_status["pending_jobs"]:
        raise S1Error("S2 尚未完成，禁止启动 S3", "s2_not_complete")
    jobs = job_index.get("records")
    details = job_details.get("records")
    if not isinstance(jobs, list) or not isinstance(details, list):
        raise S1Error("S1 或 S2 records 不是数组", "invalid_s3_input")
    jobs_by_key = {
        record["job_key"]: record for record in jobs
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    details_by_key = {
        record["job_key"]: record for record in details
        if isinstance(record, dict) and record.get("record_type") == "job_detail"
    }
    if job_index.get("config_hash") != config["config_hash"]:
        raise S1Error("S1 与 S0 配置哈希不一致", "config_hash_mismatch")
    return (
        job_index, job_details, config, jobs_by_key, details_by_key,
        _canonical_hash(job_details), config["config_hash"],
    )


def _load_screening(
    run_root: str,
    input_hash: str,
    config_hash: str,
    job_index: dict[str, Any],
    job_details: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    path = Path(run_root) / "job-research-data" / "screening-results.json"
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "config_hash": config_hash,
            "input_job_details_sha256": input_hash,
            "records": [],
        }
    document = load_json(path)
    if document.get("input_job_details_sha256") != input_hash:
        validate_document(
            document, job_index, job_details, config, allow_input_hash_mismatch=True,
        )
        document["input_job_details_sha256"] = input_hash
        atomic_write_json(path, document)
    if document.get("config_hash") != config_hash:
        raise S1Error("config.json 已变化，不能静默复用旧 S3 结果", "stale_s3_config")
    return document


def _report_city(job: dict[str, Any]) -> str:
    source_cities = job.get("source_cities")
    if isinstance(source_cities, list) and source_cities:
        return normalized_text(source_cities[0], "source_cities[0]")
    card_city = job.get("card_city")
    if isinstance(card_city, str) and card_city.strip():
        return normalized_text(card_city, "card_city")
    return "其他"


def _normal_reporting(value: Any, status: str, directions: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"primary_direction", "other_directions"}:
        raise S1Error("reporting 必须只包含 primary_direction 和 other_directions", "invalid_reporting")
    primary = value.get("primary_direction")
    if primary is not None:
        primary = normalized_text(primary, "primary_direction")
        if primary not in directions:
            raise S1Error(f"一级方向不在允许枚举中：{primary}", "invalid_direction")
    if status == "初筛通过" and primary is None:
        raise S1Error("初筛通过必须指定一级方向", "missing_direction")
    other = _normal_list(value.get("other_directions"), "other_directions", maximum=max(len(directions) - 1, 0))
    if any(direction not in directions for direction in other):
        raise S1Error("其他方向包含未允许值", "invalid_direction")
    if primary in other:
        raise S1Error("一级方向不能重复出现在其他方向中", "invalid_direction")
    return {"primary_direction": primary, "other_directions": other}


def _review_level(status: str, evidence_ids: list[str], items_to_verify: list[str], detail_status: str) -> str:
    if detail_status != "已完成" or status == "无法判断":
        return "必须复核"
    if status == "可能无关":
        return "建议复核" if evidence_ids else "必须复核"
    if items_to_verify:
        return "建议复核"
    return "无需复核"


def _normal_model_payload(payload: dict[str, Any], detail: dict[str, Any], directions: set[str]) -> dict[str, Any]:
    invalid = set(payload).intersection(PROHIBITED_FIELDS)
    unexpected = set(payload) - MODEL_FIELDS
    if invalid or unexpected or set(payload) != MODEL_FIELDS:
        raise S1Error(
            f"模型初筛字段无效；禁止字段={sorted(invalid)}，其他字段={sorted(unexpected)}",
            "invalid_screening_payload",
        )
    job_key = normalized_text(payload.get("job_key"), "job_key")
    status = normalized_text(payload.get("status"), "status")
    if status not in STATUSES:
        raise S1Error(f"初筛状态无效：{status}", "invalid_screening_status")
    reason = normalized_text(payload.get("reason"), "reason")
    if len(reason) > 500:
        raise S1Error("reason 不能超过 500 字", "invalid_screening_payload")
    evidence_ids = _normal_list(payload.get("evidence_ids"), "evidence_ids", maximum=20)
    allowed_evidence = {
        item["evidence_id"] for item in detail.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    if any(evidence_id not in allowed_evidence for evidence_id in evidence_ids):
        raise S1Error("初筛引用了不属于当前岗位的证据 ID", "invalid_evidence_reference")
    if status in {"初筛通过", "淘汰"} and not evidence_ids:
        raise S1Error(f"{status} 必须引用至少一条直接证据", "missing_screening_evidence")
    items_to_verify = _normal_list(payload.get("items_to_verify"), "items_to_verify", maximum=12)
    detail_status = detail.get("status")
    if detail_status != "已完成":
        if status != "无法判断" or evidence_ids:
            raise S1Error("详情未完成时只能标记无法判断，且不能引用证据", "detail_gate_failed")
    if status == "无法判断" and not items_to_verify:
        raise S1Error("无法判断必须说明待核实事项", "missing_verification_item")
    reporting = _normal_reporting(payload.get("reporting"), status, directions)
    return {
        "job_key": job_key,
        "status": status,
        "reason": reason,
        "evidence_ids": evidence_ids,
        "items_to_verify": items_to_verify,
        "reporting": reporting,
        "review_level": _review_level(status, evidence_ids, items_to_verify, str(detail_status)),
    }


def upsert(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_index, job_details, config, jobs, details, input_hash, config_hash = _load_inputs(run_root)
    document = _load_screening(
        run_root, input_hash, config_hash, job_index, job_details, config,
    )
    job_key = normalized_text(payload.get("job_key"), "job_key")
    if job_key not in jobs or job_key not in details:
        raise S1Error(f"S1/S2 中没有可筛选岗位：{job_key}", "job_not_found")
    processed = {
        record["job_key"] for record in document["records"]
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    next_job_key = next((key for key in details if key not in processed), None)
    if job_key != next_job_key:
        raise S1Error("S3 只接受首个待筛岗位", "job_order_mismatch")
    directions = {item["name"] for item in config["job_target"]["target_directions"]}
    normalized = _normal_model_payload(payload, details[job_key], directions)
    if normalized["job_key"] != job_key:
        raise S1Error("模型岗位身份不一致", "job_identity_conflict")

    records = document.get("records")
    if not isinstance(records, list):
        raise S1Error("screening-results.json 缺少 records 数组", "invalid_screening_results")
    by_key = {
        record["job_key"]: record for record in records
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    candidate = {
        "job_key": job_key,
        "detail_record_hash": _canonical_hash(details[job_key]),
        "status": normalized["status"],
        "reason": normalized["reason"],
        "evidence_ids": normalized["evidence_ids"],
        "items_to_verify": normalized["items_to_verify"],
        "reporting": {
            "report_city": _report_city(jobs[job_key]),
            **normalized["reporting"],
        },
        "review_level": normalized["review_level"],
        "revision": 1,
    }
    by_key[job_key] = candidate

    order = {record["job_key"]: index for index, record in enumerate(job_index["records"])}
    document["records"] = sorted(by_key.values(), key=lambda record: order[record["job_key"]])
    validate_document(document, job_index, job_details, config)
    atomic_write_json(Path(run_root) / "job-research-data" / "screening-results.json", document)
    return status(run_root)


def validate_document(
    document: dict[str, Any],
    job_index: dict[str, Any],
    job_details: dict[str, Any],
    config: dict[str, Any],
    *,
    allow_input_hash_mismatch: bool = False,
) -> dict[str, Any]:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("screening-results schema_version 无效", "invalid_screening_results")
    if not allow_input_hash_mismatch and document.get("input_job_details_sha256") != _canonical_hash(job_details):
        raise S1Error("screening-results 输入哈希与 job-details 不一致", "stale_s3_input")
    if document.get("config_hash") != config["config_hash"]:
        raise S1Error("screening-results 配置哈希与 config.json 不一致", "stale_s3_config")
    directions = {item["name"] for item in config["job_target"]["target_directions"]}
    jobs = {
        record["job_key"]: record for record in job_index.get("records", [])
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    details = {
        record["job_key"]: record for record in job_details.get("records", [])
        if isinstance(record, dict) and record.get("record_type") == "job_detail"
    }
    records = document.get("records")
    if not isinstance(records, list):
        raise S1Error("screening-results records 不是数组", "invalid_screening_results")
    seen: set[str] = set()
    counts = {value: 0 for value in STATUSES}
    for record in records:
        if not isinstance(record, dict):
            raise S1Error("初筛记录不是对象", "invalid_screening_results")
        expected_fields = {
            "job_key", "detail_record_hash", "status", "reason", "evidence_ids",
            "items_to_verify", "reporting", "review_level", "revision",
        }
        if set(record) != expected_fields:
            raise S1Error("持久化初筛记录字段不准确", "invalid_screening_results")
        key = normalized_text(record.get("job_key"), "job_key")
        if key in seen or key not in jobs or key not in details:
            raise S1Error(f"初筛岗位引用无效或重复：{key}", "invalid_screening_results")
        seen.add(key)
        if record.get("detail_record_hash") != _canonical_hash(details[key]):
            raise S1Error(f"初筛引用的岗位详情已经变化：{key}", "stale_screening_record")
        model_projection = {
            "job_key": key,
            "status": record.get("status"),
            "reason": record.get("reason"),
            "evidence_ids": record.get("evidence_ids"),
            "items_to_verify": record.get("items_to_verify"),
            "reporting": {
                "primary_direction": record.get("reporting", {}).get("primary_direction"),
                "other_directions": record.get("reporting", {}).get("other_directions"),
            },
        }
        normalized = _normal_model_payload(model_projection, details[key], directions)
        reporting = record.get("reporting")
        if not isinstance(reporting, dict) or reporting.get("report_city") != _report_city(jobs[key]):
            raise S1Error(f"初筛报告城市不一致：{key}", "invalid_screening_results")
        if record.get("review_level") not in REVIEW_LEVELS or record.get("review_level") != normalized["review_level"]:
            raise S1Error(f"程序复核等级不一致：{key}", "invalid_review_level")
        revision = record.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise S1Error(f"初筛 revision 无效：{key}", "invalid_screening_results")
        counts[str(record["status"])] += 1
    return {
        "ok": True,
        "screened_jobs": len(seen),
        "pending_jobs": len(details) - len(seen),
        "status_counts": counts,
    }


def status(run_root: str) -> dict[str, Any]:
    job_index, job_details, config, _, details, input_hash, config_hash = _load_inputs(run_root)
    document = _load_screening(
        run_root, input_hash, config_hash, job_index, job_details, config,
    )
    result = validate_document(document, job_index, job_details, config)
    processed = {
        record["job_key"] for record in document["records"]
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    result["next_job_key"] = next((key for key in details if key not in processed), None)
    return result


def pending(run_root: str, limit: int) -> dict[str, Any]:
    if limit < 1:
        raise S1Error("limit 必须大于零", "invalid_limit")
    job_index, job_details, config, jobs, details, input_hash, config_hash = _load_inputs(run_root)
    document = _load_screening(
        run_root, input_hash, config_hash, job_index, job_details, config,
    )
    processed = {
        record["job_key"] for record in document["records"]
        if isinstance(record, dict) and isinstance(record.get("job_key"), str)
    }
    values = []
    for key, detail in details.items():
        if key in processed:
            continue
        job = jobs[key]
        values.append({
            "job_key": key,
            "job_title": job.get("job_title"),
            "brand_company_name": job.get("brand_company_name"),
            "salary": job.get("salary"),
            "experience": job.get("experience"),
            "degree": job.get("degree"),
            "card_city": job.get("card_city"),
            "detail_status": detail.get("status"),
            "summary": detail.get("summary"),
            "evidence": detail.get("evidence"),
            "failure": detail.get("failure"),
        })
        if len(values) >= limit:
            break
    return {
        "ok": True,
        "screening_context": _screening_context(config),
        "pending": values,
        "remaining": len(details) - len(processed),
    }


def _read_input(path: str) -> dict[str, Any]:
    value = strict_json_loads(sys.stdin.read()) if path == "-" else load_json(path)
    if not isinstance(value, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return value


def _read_base64_input(value: str) -> dict[str, Any]:
    try:
        text = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise S1Error("input-base64 不是有效的 UTF-8 JSON", "invalid_input") from exc
    result = strict_json_loads(text)
    if not isinstance(result, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    upsert_parser = sub.add_parser("upsert")
    upsert_parser.add_argument("--run-root", required=True)
    upsert_parser.add_argument("--task-id", required=True)
    upsert_input = upsert_parser.add_mutually_exclusive_group()
    upsert_input.add_argument("--input", default="-")
    upsert_input.add_argument("--input-base64")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--run-root", required=True)
    validate_parser.add_argument("--task-id", required=True)
    pending_parser = sub.add_parser("pending")
    pending_parser.add_argument("--run-root", required=True)
    pending_parser.add_argument("--task-id", required=True)
    pending_parser.add_argument("--limit", type=int, default=1)
    return root


def main() -> int:
    args = parser().parse_args()
    from task_manager import validate_task

    validate_task(args.run_root, args.task_id)
    if args.command == "upsert":
        payload = _read_base64_input(args.input_base64) if args.input_base64 else _read_input(args.input)
        result = upsert(args.run_root, payload)
    elif args.command == "pending":
        result = pending(args.run_root, args.limit)
    else:
        result = status(args.run_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except S1Error as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
