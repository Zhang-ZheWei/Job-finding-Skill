#!/usr/bin/env python3
"""校验 S1 浏览器结果，并维护可跨组合续跑的岗位索引和检查点。"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from s1_common import (
    S1Error,
    atomic_write_json,
    combination_key,
    decode_salary,
    load_json,
    normalize_job_url,
    normalized_text,
    strict_json_loads,
    validate_search_url,
)
from task_config import generate_search_plan, validate_config


SCHEMA_VERSION = 1
PROHIBITED_FIELDS = {
    "detail", "screening", "company_key", "company_research", "scoring",
    "confidence", "coverage", "jd", "full_jd", "html", "page_text",
}


def _plain_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise S1Error(f"{field} 必须是大于等于 {minimum} 的整数", "invalid_number")
    return value


def _plain_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise S1Error(f"{field} 必须是布尔值", "invalid_scroll_proof")
    return value


def _normalize_scroll_proof(
    value: dict[str, Any],
    *,
    initial_visible: int,
    scroll_rounds: int,
    unique_after_scroll: int,
) -> dict[str, Any]:
    manual_rounds = _plain_int(value.get("manual_scroll_rounds"), "manual_scroll_rounds")
    automated_rounds = _plain_int(value.get("automated_scroll_rounds"), "automated_scroll_rounds")
    successful_rounds = _plain_int(value.get("successful_refresh_rounds"), "successful_refresh_rounds")
    end_marker_seen = _plain_bool(value.get("end_marker_seen"), "end_marker_seen")
    trace = value.get("scroll_trace")
    if scroll_rounds != manual_rounds + automated_rounds:
        raise S1Error("总滚动轮数与人工、自动轮数不一致", "invalid_scroll_proof")
    if not isinstance(trace, list) or len(trace) != automated_rounds:
        raise S1Error("scroll_trace 必须逐轮覆盖全部自动滚动", "invalid_scroll_proof")
    traced_refreshes = 0
    for index, item in enumerate(trace):
        if not isinstance(item, dict):
            raise S1Error("scroll_trace 项必须是对象", "invalid_scroll_proof")
        expected_round = manual_rounds + index + 1
        if _plain_int(item.get("round"), "scroll_trace.round", 1) != expected_round:
            raise S1Error("scroll_trace 轮次不连续", "invalid_scroll_proof")
        before_count = _plain_int(item.get("before_unique_jobs"), "before_unique_jobs")
        after_count = _plain_int(item.get("after_unique_jobs"), "after_unique_jobs")
        added = _plain_int(item.get("added_unique_jobs"), "added_unique_jobs")
        if after_count - before_count != added:
            raise S1Error("scroll_trace 新增岗位数量不一致", "invalid_scroll_proof")
        for field in (
            "before_scroll_y", "after_scroll_y", "before_scroll_height", "after_scroll_height",
        ):
            _plain_int(item.get(field), f"scroll_trace.{field}")
        if not _plain_bool(item.get("effective"), "scroll_trace.effective"):
            raise S1Error("无效滚动不得计入完成证据", "invalid_scroll_proof")
        _plain_bool(item.get("recovery_nudge"), "scroll_trace.recovery_nudge")
        if added > 0:
            traced_refreshes += 1
    if trace and trace[-1]["after_unique_jobs"] != unique_after_scroll:
        raise S1Error("scroll_trace 最终岗位数量与采集结果不一致", "invalid_scroll_proof")
    if successful_rounds < traced_refreshes or successful_rounds > traced_refreshes + manual_rounds:
        raise S1Error("成功刷新轮数无法由自动或人工滚动证明", "invalid_scroll_proof")
    if successful_rounds > 0 and unique_after_scroll <= initial_visible:
        raise S1Error("声明刷新成功但岗位数量没有增长", "invalid_scroll_proof")
    return {
        "manual_scroll_rounds": manual_rounds,
        "automated_scroll_rounds": automated_rounds,
        "successful_refresh_rounds": successful_rounds,
        "end_marker_seen": end_marker_seen,
        "scroll_trace": copy.deepcopy(trace),
    }


def _build_documents(
    payload: dict[str, Any],
    *,
    require_sample_scroll: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    search_url, url_term = validate_search_url(payload.get("search_url"))
    city = normalized_text(payload.get("city"), "city")
    term = normalized_text(payload.get("term", url_term), "term")
    if term != url_term:
        raise S1Error("term 与 URL query 不一致", "term_url_mismatch")
    collection_mode = normalized_text(payload.get("collection_mode", "bounded_sample"), "collection_mode")
    if collection_mode not in {"bounded_sample", "exhaustive"}:
        raise S1Error("collection_mode 无效", "invalid_collection_mode")
    limit = _plain_int(payload.get("limit"), "limit", 0 if collection_mode == "exhaustive" else 1)
    initial_visible = _plain_int(payload.get("initial_visible_count"), "initial_visible_count")
    scroll_rounds = _plain_int(payload.get("scroll_rounds"), "scroll_rounds")
    unique_after_scroll = _plain_int(payload.get("unique_jobs_after_scroll"), "unique_jobs_after_scroll")
    stop_reason = normalized_text(payload.get("stop_reason"), "stop_reason")
    no_new_rounds = _plain_int(payload.get("consecutive_no_new_rounds", 0), "consecutive_no_new_rounds")
    scroll_proof = _normalize_scroll_proof(
        payload,
        initial_visible=initial_visible,
        scroll_rounds=scroll_rounds,
        unique_after_scroll=unique_after_scroll,
    )

    if collection_mode == "bounded_sample":
        if stop_reason != "sample_limit_reached" or unique_after_scroll < limit:
            raise S1Error("采集器没有达到本次限量", "sample_incomplete")
        if require_sample_scroll and (
            initial_visible >= limit or scroll_rounds < 1 or scroll_proof["successful_refresh_rounds"] < 1
        ):
            raise S1Error("限量验收没有证明滚动刷新成功", "scroll_not_proven")
    else:
        if stop_reason != "natural_exhaustion" or no_new_rounds < 10:
            raise S1Error("完整采集缺少连续 10 轮无新增证据", "exhaustion_not_proven")
        if (
            initial_visible >= 15
            and scroll_proof["successful_refresh_rounds"] < 1
            and not scroll_proof["end_marker_seen"]
        ):
            raise S1Error("首屏达到15条但没有证明岗位刷新或页面结束", "scroll_not_proven")

    raw_cards = payload.get("cards")
    if not isinstance(raw_cards, list):
        raise S1Error("cards 必须是数组", "invalid_cards")

    combo_key = combination_key(search_url, term)
    records: list[dict[str, Any]] = []
    id_to_index: dict[str, int] = {}
    url_to_index: dict[str, int] = {}
    for raw_index, raw_card in enumerate(raw_cards):
        if not isinstance(raw_card, dict):
            raise S1Error(f"cards[{raw_index}] 必须是对象", "invalid_card")
        job_url, path_job_id = normalize_job_url(raw_card.get("boss_job_url"))
        explicit_job_id = normalized_text(raw_card.get("job_id", path_job_id), "job_id")
        if explicit_job_id != path_job_id:
            raise S1Error("岗位 ID 与详情 URL 不一致", "job_identity_conflict")
        id_match = id_to_index.get(explicit_job_id)
        url_match = url_to_index.get(job_url)
        if id_match is not None and url_match is not None and id_match != url_match:
            raise S1Error("岗位身份别名指向不同记录", "job_identity_conflict")
        if id_match is not None or url_match is not None:
            continue
        if len(records) >= limit:
            break

        tags = raw_card.get("tags", [])
        if not isinstance(tags, list):
            raise S1Error("岗位标签必须是数组", "invalid_card")
        normal_tags = [normalized_text(tag, "tag", allow_empty=True) for tag in tags]
        record = {
            "job_key": f"id:{explicit_job_id}",
            "job_id": explicit_job_id,
            "job_id_aliases": [explicit_job_id],
            "first_recall_seq": len(records) + 1,
            "source_combinations": [combo_key],
            "job_title": normalized_text(raw_card.get("job_title"), "job_title"),
            "brand_company_name": normalized_text(raw_card.get("brand_company_name"), "brand_company_name"),
            "boss_job_url": job_url,
            "salary": decode_salary(raw_card.get("salary", "")),
            "experience": normal_tags[0] if normal_tags else "",
            "degree": normal_tags[1] if len(normal_tags) > 1 else "",
            "card_city": normalized_text(raw_card.get("card_city", ""), "card_city", allow_empty=True),
            "source_cities": [city],
            "source_terms": [term],
            "posted_at": normalized_text(raw_card.get("posted_at", ""), "posted_at", allow_empty=True),
        }
        records.append(record)
        id_to_index[explicit_job_id] = len(records) - 1
        url_to_index[job_url] = len(records) - 1

    if len(records) != limit:
        raise S1Error(f"预期 {limit} 个唯一岗位，实际 {len(records)} 个", "sample_incomplete")

    job_index = {
        "schema_version": SCHEMA_VERSION,
        "collection_mode": collection_mode,
        "sample_limit": limit if collection_mode == "bounded_sample" else None,
        "records": records,
    }
    checkpoint = {
        "schema_version": SCHEMA_VERSION,
        "collection_mode": collection_mode,
        "combinations": [{
            "combo_key": combo_key,
            "search_url": search_url,
            "city": city,
            "term": term,
            "status": "sample_complete" if collection_mode == "bounded_sample" else "completed",
            "evidence": {
                "sample_limit": limit if collection_mode == "bounded_sample" else None,
                "initial_visible_count": initial_visible,
                "scroll_rounds": scroll_rounds,
                "consecutive_no_new_rounds": no_new_rounds,
                "unique_jobs_after_scroll": unique_after_scroll,
                "collected_job_count": len(records),
                "stop_reason": stop_reason,
                **scroll_proof,
            },
        }],
    }
    if require_sample_scroll or collection_mode == "exhaustive":
        validate_documents(job_index, checkpoint)
    return job_index, checkpoint


def build_documents(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """保留单 URL、20 条滚动验收入口。"""
    return _build_documents(payload, require_sample_scroll=True)


def _validate_records(records: Any) -> None:
    if not isinstance(records, list):
        raise S1Error("job-index.records 必须是数组", "invalid_job_index")
    keys: set[str] = set()
    ids: set[str] = set()
    urls: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise S1Error(f"records[{index}] 必须是对象", "invalid_job_index")
        unexpected = PROHIBITED_FIELDS.intersection(record)
        if unexpected:
            raise S1Error(f"S1 记录包含越界字段：{sorted(unexpected)}", "scope_violation")
        job_url, path_id = normalize_job_url(record.get("boss_job_url"))
        job_id = normalized_text(record.get("job_id"), "job_id")
        job_key = normalized_text(record.get("job_key"), "job_key")
        if job_id != path_id or job_key != f"id:{job_id}":
            raise S1Error("已保存的岗位身份不一致", "job_identity_conflict")
        if job_key in keys or job_id in ids or job_url in urls:
            raise S1Error("已保存岗位身份重复", "duplicate_job")
        keys.add(job_key)
        ids.add(job_id)
        urls.add(job_url)
        if record.get("first_recall_seq") != index + 1:
            raise S1Error("first_recall_seq 必须连续", "invalid_job_index")
        normalized_text(record.get("job_title"), "job_title")
        normalized_text(record.get("brand_company_name"), "brand_company_name")
        for field in ("source_combinations", "source_cities", "source_terms"):
            values = record.get(field)
            if not isinstance(values, list) or not values or len(values) != len(set(values)):
                raise S1Error(f"{field} 必须是非空不重复数组", "invalid_job_index")


def validate_documents(job_index: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    """校验旧的单组合验收文档。"""
    if job_index.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("job-index schema_version 无效", "invalid_schema")
    collection_mode = job_index.get("collection_mode")
    if collection_mode not in {"bounded_sample", "exhaustive"}:
        raise S1Error("collection_mode 无效", "invalid_schema")
    records = job_index.get("records")
    limit = len(records) if collection_mode == "exhaustive" and isinstance(records, list) else _plain_int(job_index.get("sample_limit"), "sample_limit", 1)
    if not isinstance(records, list) or len(records) != limit:
        raise S1Error("岗位数量与限量不一致", "invalid_job_index")
    _validate_records(records)
    combinations = checkpoint.get("combinations")
    expected_status = "sample_complete" if collection_mode == "bounded_sample" else "completed"
    if (
        checkpoint.get("schema_version") != SCHEMA_VERSION
        or not isinstance(combinations, list)
        or len(combinations) != 1
        or combinations[0].get("status") != expected_status
    ):
        raise S1Error("checkpoint 与采集模式不一致", "invalid_checkpoint")
    evidence = combinations[0].get("evidence")
    if not isinstance(evidence, dict) or evidence.get("collected_job_count") != len(records):
        raise S1Error("checkpoint 数量证据不一致", "invalid_checkpoint")
    observed_unique = _plain_int(evidence.get("unique_jobs_after_scroll"), "unique_jobs_after_scroll")
    if (
        (collection_mode == "bounded_sample" and observed_unique < len(records))
        or (collection_mode == "exhaustive" and observed_unique != len(records))
    ):
        raise S1Error("checkpoint 观察数量与保存数量不一致", "invalid_checkpoint")
    proof = _normalize_scroll_proof(
        evidence,
        initial_visible=_plain_int(evidence.get("initial_visible_count"), "initial_visible_count"),
        scroll_rounds=_plain_int(evidence.get("scroll_rounds"), "scroll_rounds"),
        unique_after_scroll=observed_unique,
    )
    if collection_mode == "bounded_sample":
        if (
            evidence.get("initial_visible_count", limit) >= limit
            or evidence.get("scroll_rounds", 0) < 1
            or proof["successful_refresh_rounds"] < 1
        ):
            raise S1Error("checkpoint 没有证明滚动成功", "invalid_checkpoint")
        if evidence.get("stop_reason") != "sample_limit_reached":
            raise S1Error("checkpoint 停止原因无效", "invalid_checkpoint")
    else:
        if evidence.get("stop_reason") != "natural_exhaustion" or evidence.get("consecutive_no_new_rounds", 0) < 10:
            raise S1Error("checkpoint 没有证明自然穷尽", "invalid_checkpoint")
        if (
            evidence.get("initial_visible_count", 0) >= 15
            and proof["successful_refresh_rounds"] < 1
            and not proof["end_marker_seen"]
        ):
            raise S1Error("checkpoint 没有证明首屏之后发生刷新或页面结束", "invalid_checkpoint")
    return {"ok": True, "records": len(records), "status": expected_status}


def _empty_run_documents(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    mode = config["search_scope"]["search_mode"]
    target = config["search_scope"]["per_city_target_count"]
    return (
        {
            "schema_version": SCHEMA_VERSION,
            "config_hash": config["config_hash"],
            "collection_mode": mode,
            "sample_limit": None,
            "records": [],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "config_hash": config["config_hash"],
            "collection_mode": mode,
            "combinations": [],
        },
    )


def _load_config(run_root: str) -> dict[str, Any]:
    return validate_config(load_json(Path(run_root) / "job-research-data" / "config.json"))


def _load_run_documents(run_root: str, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data_dir = Path(run_root) / "job-research-data"
    empty_index, empty_checkpoint = _empty_run_documents(config)
    index_path = data_dir / "job-index.json"
    checkpoint_path = data_dir / "checkpoint.json"
    if checkpoint_path.exists() and not index_path.exists():
        raise S1Error("checkpoint.json 存在但 job-index.json 缺失", "incomplete_s1_state")
    return (
        load_json(index_path) if index_path.exists() else empty_index,
        load_json(checkpoint_path) if checkpoint_path.exists() else empty_checkpoint,
    )


def _screening_passes(run_root: str, config_hash: str) -> dict[str, list[str]]:
    """读取 S3 已确认的通过岗位；目标数只按报告城市统计。"""
    path = Path(run_root) / "job-research-data" / "screening-results.json"
    if not path.exists():
        return {}
    document = load_json(path)
    if document.get("config_hash") != config_hash or not isinstance(document.get("records"), list):
        raise S1Error("screening-results.json 与当前任务不一致", "invalid_target_evidence")
    result: dict[str, list[str]] = {}
    for record in document["records"]:
        if not isinstance(record, dict) or record.get("status") != "初筛通过":
            continue
        reporting = record.get("reporting")
        if not isinstance(reporting, dict):
            raise S1Error("初筛通过岗位缺少报告城市", "invalid_target_evidence")
        city = normalized_text(reporting.get("report_city"), "report_city")
        job_key = normalized_text(record.get("job_key"), "job_key")
        if job_key not in result.setdefault(city, []):
            result[city].append(job_key)
    return result


def _validate_run_documents(
    config: dict[str, Any],
    job_index: dict[str, Any],
    checkpoint: dict[str, Any],
    *,
    allow_unclosed_sources: bool,
    run_root: str,
) -> dict[str, Any]:
    mode = config["search_scope"]["search_mode"]
    for name, document in (("job-index", job_index), ("checkpoint", checkpoint)):
        if document.get("schema_version") != SCHEMA_VERSION:
            raise S1Error(f"{name} schema_version 无效", "invalid_schema")
        if document.get("config_hash") != config["config_hash"]:
            raise S1Error(f"{name} 与 config.json 不属于同一任务", "config_hash_mismatch")
        if document.get("collection_mode") != mode:
            raise S1Error(f"{name} collection_mode 与配置不一致", "invalid_collection_mode")
    records = job_index.get("records")
    _validate_records(records)

    plan = generate_search_plan(config)
    plan_by_key = {item["combo_key"]: item for item in plan}
    plan_order = {item["combo_key"]: index for index, item in enumerate(plan)}
    combinations = checkpoint.get("combinations")
    if not isinstance(combinations, list):
        raise S1Error("checkpoint.combinations 必须是数组", "invalid_checkpoint")
    statuses: dict[str, str] = {}
    previous_order = -1
    for index, entry in enumerate(combinations):
        if not isinstance(entry, dict):
            raise S1Error(f"combinations[{index}] 必须是对象", "invalid_checkpoint")
        key = entry.get("combo_key")
        planned = plan_by_key.get(key)
        if planned is None or key in statuses:
            raise S1Error("checkpoint 含未知或重复组合", "invalid_checkpoint")
        order = plan_order[key]
        if order <= previous_order:
            raise S1Error("checkpoint 组合顺序必须与配置一致", "invalid_checkpoint")
        previous_order = order
        if entry.get("search_url_order") != planned["search_url_order"] or entry.get("keyword_order") != planned["keyword_order"]:
            raise S1Error("checkpoint 组合顺序字段无法由配置复现", "invalid_checkpoint")
        if entry.get("city") != planned["city_label"] or entry.get("term") != planned["term"]:
            raise S1Error("checkpoint 城市或关键词无法由配置复现", "invalid_checkpoint")
        status = entry.get("status")
        if status not in {"completed", "skipped_target"}:
            raise S1Error("checkpoint 只允许保存终态", "invalid_checkpoint")
        if mode == "exhaustive" and status != "completed":
            raise S1Error("完整采集不得跳过组合", "invalid_checkpoint")
        evidence = entry.get("evidence")
        if status == "completed":
            if not isinstance(evidence, dict):
                raise S1Error("已完成组合缺少采集证据", "invalid_checkpoint")
            collected_count = _plain_int(evidence.get("collected_job_count"), "collected_job_count")
            if evidence.get("unique_jobs_after_scroll") != collected_count:
                raise S1Error("组合岗位数量证据不一致", "invalid_checkpoint")
            proof = _normalize_scroll_proof(
                evidence,
                initial_visible=_plain_int(evidence.get("initial_visible_count"), "initial_visible_count"),
                scroll_rounds=_plain_int(evidence.get("scroll_rounds"), "scroll_rounds"),
                unique_after_scroll=collected_count,
            )
            if evidence.get("stop_reason") != "natural_exhaustion" or evidence.get("consecutive_no_new_rounds", 0) < 10:
                raise S1Error("城市目标模式中的已执行组合也必须自然穷尽", "invalid_checkpoint")
            if (
                evidence.get("initial_visible_count", 0) >= 15
                and proof["successful_refresh_rounds"] < 1
                and not proof["end_marker_seen"]
            ):
                raise S1Error("已完成组合没有证明首屏之后发生刷新或页面结束", "invalid_checkpoint")
        elif not isinstance(evidence, dict):
            raise S1Error("按目标跳过组合必须保存 S3 通过数量证据", "invalid_checkpoint")
        statuses[key] = status

    completed = {key for key, status in statuses.items() if status == "completed"}
    for record in records:
        for source in record["source_combinations"]:
            if source not in plan_by_key:
                raise S1Error("岗位引用了非计划组合", "invalid_job_source")
            if not allow_unclosed_sources and source not in completed:
                raise S1Error("岗位来源组合尚未完成", "invalid_job_source")
            if statuses.get(source) == "skipped_target":
                raise S1Error("岗位不能引用跳过组合", "invalid_job_source")

    if mode == "per_city_target":
        target = config["search_scope"]["per_city_target_count"]
        passes = _screening_passes(run_root, config["config_hash"])
        for key, status in statuses.items():
            if status != "skipped_target":
                continue
            city = plan_by_key[key]["city_label"]
            evidence = next(item["evidence"] for item in combinations if item["combo_key"] == key)
            passed_keys = evidence.get("passed_job_keys")
            if (
                evidence.get("target_count") != target
                or evidence.get("screening_passed_count") != len(passed_keys or [])
                or not isinstance(passed_keys, list)
                or len(passed_keys) < target
                or len(set(passed_keys)) != len(passed_keys)
                or any(job_key not in passes.get(city, []) for job_key in passed_keys)
            ):
                raise S1Error("城市未达到 S3 初筛通过目标却跳过了组合", "invalid_target_skip")
    completed_count = sum(status == "completed" for status in statuses.values())
    skipped_count = sum(status == "skipped_target" for status in statuses.values())
    return {
        "ok": True,
        "records": len(records),
        "planned_combinations": len(plan),
        "completed_combinations": completed_count,
        "skipped_combinations": skipped_count,
        "pending_combinations": len(plan) - completed_count - skipped_count,
    }


def validate_run_documents(run_root: str) -> dict[str, Any]:
    config = _load_config(run_root)
    job_index, checkpoint = _load_run_documents(run_root, config)
    return _validate_run_documents(
        config, job_index, checkpoint, allow_unclosed_sources=False, run_root=run_root,
    )


def next_combination(run_root: str) -> dict[str, Any]:
    config = _load_config(run_root)
    job_index, checkpoint = _load_run_documents(run_root, config)
    status = _validate_run_documents(
        config, job_index, checkpoint, allow_unclosed_sources=True, run_root=run_root,
    )
    closed = {entry["combo_key"] for entry in checkpoint["combinations"]}
    workflow_state = "ready_for_s1"
    if config["search_scope"]["search_mode"] == "per_city_target" and any(
        entry["status"] == "completed" for entry in checkpoint["combinations"]
    ):
        if job_index["records"]:
            details_path = Path(run_root) / "job-research-data" / "job-details.json"
            if not details_path.exists():
                return {**status, "config_hash": config["config_hash"], "workflow_state": "awaiting_s2", "next_combo": None}
            from s2_store import status as s2_status

            if s2_status(run_root)["pending_jobs"]:
                return {**status, "config_hash": config["config_hash"], "workflow_state": "awaiting_s2", "next_combo": None}
            screening_path = Path(run_root) / "job-research-data" / "screening-results.json"
            if not screening_path.exists():
                return {**status, "config_hash": config["config_hash"], "workflow_state": "awaiting_s3", "next_combo": None}
            from s3_store import status as s3_status

            if s3_status(run_root)["pending_jobs"]:
                return {**status, "config_hash": config["config_hash"], "workflow_state": "awaiting_s3", "next_combo": None}

        target = config["search_scope"]["per_city_target_count"]
        passes = _screening_passes(run_root, config["config_hash"])
        skipped = False
        for planned in generate_search_plan(config):
            city_passes = passes.get(planned["city_label"], [])
            if planned["combo_key"] in closed or len(city_passes) < target:
                continue
            checkpoint["combinations"].append({
                "combo_key": planned["combo_key"],
                "search_url_order": planned["search_url_order"],
                "keyword_order": planned["keyword_order"],
                "city": planned["city_label"],
                "term": planned["term"],
                "status": "skipped_target",
                "evidence": {
                    "target_count": target,
                    "screening_passed_count": len(city_passes),
                    "passed_job_keys": city_passes,
                },
            })
            closed.add(planned["combo_key"])
            skipped = True
        if skipped:
            order = {item["combo_key"]: index for index, item in enumerate(generate_search_plan(config))}
            checkpoint["combinations"].sort(key=lambda entry: order[entry["combo_key"]])
            atomic_write_json(Path(run_root) / "job-research-data" / "checkpoint.json", checkpoint)
            status = _validate_run_documents(config, job_index, checkpoint, allow_unclosed_sources=False, run_root=run_root)

    next_item = next((item for item in generate_search_plan(config) if item["combo_key"] not in closed), None)
    if next_item is not None:
        next_item = copy.deepcopy(next_item)
        next_item["collection_mode"] = "exhaustive"
        next_item["limit"] = None
    else:
        workflow_state = "s1_complete"
    return {**status, "config_hash": config["config_hash"], "workflow_state": workflow_state, "next_combo": next_item}


def _merge_records(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> int:
    id_index = {record["job_id"]: index for index, record in enumerate(existing)}
    url_index = {record["boss_job_url"]: index for index, record in enumerate(existing)}
    inserted = 0
    for record in incoming:
        id_match = id_index.get(record["job_id"])
        url_match = url_index.get(record["boss_job_url"])
        if id_match is not None and url_match is not None and id_match != url_match:
            raise S1Error("跨组合岗位身份发生冲突", "job_identity_conflict")
        match = id_match if id_match is not None else url_match
        if match is None:
            candidate = copy.deepcopy(record)
            candidate["first_recall_seq"] = len(existing) + 1
            existing.append(candidate)
            id_index[candidate["job_id"]] = len(existing) - 1
            url_index[candidate["boss_job_url"]] = len(existing) - 1
            inserted += 1
            continue
        current = existing[match]
        for field in ("source_combinations", "source_cities", "source_terms", "job_id_aliases"):
            for value in record[field]:
                if value not in current[field]:
                    current[field].append(value)
    return inserted


def merge_run_documents(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    config = _load_config(run_root)
    job_index, checkpoint = _load_run_documents(run_root, config)
    before = next_combination(run_root)
    expected = before["next_combo"]
    if expected is None:
        raise S1Error("S1 已没有待采集组合", "no_pending_combination")
    search_url, term = validate_search_url(payload.get("search_url"))
    if (
        search_url != expected["search_url"]
        or term != expected["term"]
        or normalized_text(payload.get("city"), "city") != expected["city_label"]
    ):
        raise S1Error("浏览器结果不属于当前首个待处理组合", "combination_mismatch")
    if payload.get("collection_mode") != "exhaustive":
        raise S1Error("正式流程中的每个搜索组合都必须自然穷尽", "collection_mode_mismatch")

    incoming_index, incoming_checkpoint = _build_documents(payload, require_sample_scroll=False)
    inserted = _merge_records(job_index["records"], incoming_index["records"])
    evidence = copy.deepcopy(incoming_checkpoint["combinations"][0]["evidence"])
    evidence["new_global_job_count"] = inserted
    checkpoint["combinations"].append({
        "combo_key": expected["combo_key"],
        "search_url_order": expected["search_url_order"],
        "keyword_order": expected["keyword_order"],
        "city": expected["city_label"],
        "term": expected["term"],
        "status": "completed",
        "evidence": evidence,
    })

    order = {item["combo_key"]: index for index, item in enumerate(generate_search_plan(config))}
    checkpoint["combinations"].sort(key=lambda entry: order[entry["combo_key"]])
    result = _validate_run_documents(
        config, job_index, checkpoint, allow_unclosed_sources=False, run_root=run_root,
    )
    data_dir = Path(run_root) / "job-research-data"
    atomic_write_json(data_dir / "job-index.json", job_index)
    atomic_write_json(data_dir / "checkpoint.json", checkpoint)
    return {
        **result,
        "combo_key": expected["combo_key"],
        "city": expected["city_label"],
        "term": expected["term"],
        "new_global_jobs": inserted,
    }


def write_documents(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    """写入独立单组合验收结果；配置驱动运行使用 merge_run_documents。"""
    job_index, checkpoint = build_documents(payload)
    data_dir = Path(run_root) / "job-research-data"
    atomic_write_json(data_dir / "job-index.json", job_index)
    atomic_write_json(data_dir / "checkpoint.json", checkpoint)
    return validate_documents(job_index, checkpoint)


def _read_input(path: str) -> dict[str, Any]:
    value = strict_json_loads(sys.stdin.read()) if path == "-" else load_json(path)
    if not isinstance(value, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write")
    write.add_argument("--run-root", required=True)
    write.add_argument("--task-id", required=True)
    write.add_argument("--input", default="-")
    merge = sub.add_parser("merge")
    merge.add_argument("--run-root", required=True)
    merge.add_argument("--task-id", required=True)
    merge.add_argument("--input", default="-")
    next_parser = sub.add_parser("next")
    next_parser.add_argument("--run-root", required=True)
    next_parser.add_argument("--task-id", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--run-root", required=True)
    validate.add_argument("--task-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    from task_manager import validate_task

    validate_task(args.run_root, args.task_id)
    if args.command == "write":
        result = write_documents(args.run_root, _read_input(args.input))
    elif args.command == "merge":
        result = merge_run_documents(args.run_root, _read_input(args.input))
    elif args.command == "next":
        result = next_combination(args.run_root)
    else:
        result = validate_run_documents(args.run_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except S1Error as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
