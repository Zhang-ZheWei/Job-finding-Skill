#!/usr/bin/env python3
"""校验公司网络背调结果，并原子写入 company-research.json。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from s1_common import S1Error, atomic_write_json, load_json, normalized_text, sha256_text, strict_json_loads
from s2_store import validate_document as validate_s2_document
from s1_store import validate_run_documents
from s3_store import validate_document as validate_s3_document
from task_config import validate_config


SCHEMA_VERSION = 3
GROUPS = ("basic_profile", "public_risks", "employee_reviews")
QUERY_STATUSES = {"已完成", "部分完成", "查询失败"}
RESEARCH_STATUSES = {"已完成", "部分完成", "查询失败"}
ACCESS_STATUSES = {"已访问", "访问受限"}
ATTEMPT_STATUSES = {"找到相关内容", "未找到相关内容", "查询失败"}
CONTENT_TYPES = {"page", "post", "comment"}
PLATFORM_LABELS = {
    "aiqicha": "爱企查",
    "official_website": "公司官网",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
    "nowcoder": "牛客网",
    "maimai": "脉脉",
    "government": "政府或监管平台",
    "court": "法院平台",
    "other": "其他来源",
}
AIQICHA_HOSTS = {"www.aiqicha.com", "aiqicha.com", "aiqicha.baidu.com"}
REVIEW_PLATFORMS = ("zhihu", "xiaohongshu", "nowcoder", "maimai")
REVIEW_PLATFORM_HOSTS = {
    "zhihu": {"www.zhihu.com"},
    "xiaohongshu": {"www.xiaohongshu.com"},
    "nowcoder": {"www.nowcoder.com"},
    "maimai": {"maimai.cn", "www.maimai.cn"},
}
SOURCE_TYPES = {
    "official_company", "business_information_platform", "government", "court", "authoritative_media",
    "recruitment_platform", "employee_review_platform", "social_platform", "other",
}
MODEL_FIELDS = {"company_key", "query_attempts", "evidence", *GROUPS}
PROHIBITED_FIELDS = {
    "confidence", "coverage", "risk_score", "company_score", "revision", "status",
    "config_hash", "input_hash", "enterprise_name", "unified_social_credit_code",
}
SOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
CATEGORY_VALUES = {
    "basic_profile": {
        "official_website", "main_business", "products_services", "industry",
        "registered_capital", "paid_in_capital", "established_at", "employee_count",
        "operating_revenue", "size_stage", "ownership_financing", "location", "other",
    },
    "public_risks": {
        "judicial", "administrative_penalty", "regulatory_measure", "business_abnormality",
        "employment_dispute", "financial", "reputation", "other",
    },
    "employee_reviews": {
        "work_intensity", "management", "compensation", "career_growth",
        "stability", "culture", "other",
    },
}
PERSISTED_RECORD_FIELDS = {
    "record_type", "company_key", "enterprise_name", "unified_social_credit_code",
    "boss_company_subject_keys", "brand_company_names", "linked_job_keys", "report_cities",
    "search_terms", "query_attempts", "basic_profile", "public_risks", "employee_reviews",
    "evidence", "status", "revision",
}
SKIPPED_JOB_FIELDS = {
    "job_key", "boss_company_subject_key", "company_identity_status", "reason_code", "reason",
}


def _canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _normal_identity_text(value: Any, field: str) -> str:
    return unicodedata.normalize("NFKC", normalized_text(value, field))


def _normal_list(value: Any, field: str, *, maximum: int = 40) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise S1Error(f"{field} 必须是最多 {maximum} 项的数组", "invalid_company_research")
    result = [normalized_text(item, f"{field}[]") for item in value]
    if len(set(result)) != len(result):
        raise S1Error(f"{field} 含重复内容", "invalid_company_research")
    return result


def _https_url(value: Any, field: str) -> str:
    url = normalized_text(value, field)
    if len(url) > 2000:
        raise S1Error(f"{field} 过长", "invalid_source_url")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise S1Error(f"{field} 无法解析", "invalid_source_url") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise S1Error(f"{field} 必须是无账号信息的 HTTPS URL", "invalid_source_url")
    return url


def _company_key(enterprise_name: str, credit_code: str) -> str:
    identity = f"credit:{credit_code}" if credit_code else f"name:{_normal_identity_text(enterprise_name, 'enterprise_name')}"
    return f"company:{sha256_text(identity)}"


def _required_queries(task: dict[str, Any]) -> list[dict[str, Any]]:
    enterprise = task["enterprise_name"]
    queries = [
        {
            "group": "basic_profile", "platform_key": "aiqicha", "platform": PLATFORM_LABELS["aiqicha"],
            "search_term": enterprise, "search_term_type": "enterprise_name", "required_content_types": [],
        },
        {
            "group": "basic_profile", "platform_key": "official_website", "platform": PLATFORM_LABELS["official_website"],
            "search_term": enterprise, "search_term_type": "enterprise_name", "required_content_types": [],
        },
    ]
    for platform_key in REVIEW_PLATFORMS:
        for search_term in task["search_terms"]:
            queries.append({
                "group": "employee_reviews",
                "platform_key": platform_key,
                "platform": PLATFORM_LABELS[platform_key],
                "search_term": search_term["term"],
                "search_term_type": search_term["term_type"],
                "required_content_types": ["post", "comment"],
            })
    return queries


def _load_inputs(run_root: str) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any],
    list[dict[str, Any]], list[dict[str, Any]], str, str,
]:
    data_dir = Path(run_root) / "job-research-data"
    s1_status = validate_run_documents(run_root)
    if s1_status["pending_combinations"]:
        raise S1Error("S1 搜索计划尚未结束，禁止启动 S4", "s1_not_complete")
    config = validate_config(load_json(data_dir / "config.json"))
    job_index = load_json(data_dir / "job-index.json")
    job_details = load_json(data_dir / "job-details.json")
    screening = load_json(data_dir / "screening-results.json")
    validate_s2_document(job_details, job_index)
    s3_status = validate_s3_document(screening, job_index, job_details, config)
    if s3_status["pending_jobs"]:
        raise S1Error("S3 尚未完成，禁止启动 S4", "s3_not_complete")
    tasks, skipped_jobs = _build_company_tasks(job_index, job_details, screening)
    return (
        config, job_details, screening, job_index, tasks, skipped_jobs,
        _canonical_hash(job_details), _canonical_hash(screening),
    )


def _build_company_tasks(
    job_index: dict[str, Any], job_details: dict[str, Any], screening: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    jobs = {
        item["job_key"]: item for item in job_index.get("records", [])
        if isinstance(item, dict) and isinstance(item.get("job_key"), str)
    }
    details = {
        item["job_key"]: item for item in job_details.get("records", [])
        if isinstance(item, dict) and item.get("record_type") == "job_detail"
    }
    subjects = {
        item["boss_company_subject_key"]: item for item in job_details.get("records", [])
        if isinstance(item, dict) and item.get("record_type") == "boss_company_subject"
    }
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    skipped_jobs: list[dict[str, Any]] = []
    for result in screening.get("records", []):
        if not isinstance(result, dict) or result.get("status") != "初筛通过":
            continue
        job_key = normalized_text(result.get("job_key"), "job_key")
        detail = details.get(job_key)
        job = jobs.get(job_key)
        if detail is None or job is None:
            raise S1Error(f"初筛通过岗位缺少 S1/S2 记录：{job_key}", "invalid_s4_input")
        subject_key = detail.get("boss_company_subject_key")
        subject = subjects.get(subject_key)
        business = subject.get("business") if isinstance(subject, dict) else None
        if not isinstance(business, dict) or business.get("status") != "已取得":
            identity_status = business.get("status") if isinstance(business, dict) else (
                "主体记录缺失" if subject_key else "无公司主体链接"
            )
            reason_by_status = {
                "未取得": ("enterprise_name_not_found", "BOSS 公司页未取得可信企业名称"),
                "查询失败": ("company_identity_query_failed", "BOSS 公司页工商主体读取失败"),
                "来源冲突": ("company_identity_conflict", "BOSS 公司页工商主体信息存在冲突"),
                "主体记录缺失": ("company_subject_record_missing", "岗位引用的 BOSS 公司主体记录缺失"),
                "无公司主体链接": ("company_subject_link_missing", "岗位详情没有可用的 BOSS 公司主体链接"),
            }
            reason_code, reason = reason_by_status.get(
                identity_status,
                ("company_identity_unavailable", "岗位没有可用于公司背调的可信企业主体"),
            )
            skipped_jobs.append({
                "job_key": job_key,
                "boss_company_subject_key": subject_key if isinstance(subject_key, str) else None,
                "company_identity_status": identity_status,
                "reason_code": reason_code,
                "reason": reason,
            })
            continue
        enterprise_name = _normal_identity_text(business.get("enterprise_name"), "enterprise_name")
        credit_code = _normal_identity_text(business.get("unified_social_credit_code") or "", "unified_social_credit_code") if business.get("unified_social_credit_code") else ""
        key = _company_key(enterprise_name, credit_code)
        if key not in by_key:
            by_key[key] = {
                "company_key": key,
                "enterprise_name": enterprise_name,
                "unified_social_credit_code": credit_code,
                "boss_company_subject_keys": [],
                "brand_company_names": [],
                "linked_job_keys": [],
                "report_cities": [],
                "search_terms": [{"term": enterprise_name, "term_type": "enterprise_name"}],
            }
            order.append(key)
        task = by_key[key]
        if task["enterprise_name"] != enterprise_name:
            raise S1Error("同一信用代码对应不同企业名称", "company_identity_conflict")
        for value, field in (
            (subject_key, "boss_company_subject_keys"),
            (job_key, "linked_job_keys"),
        ):
            if value not in task[field]:
                task[field].append(value)
        for brand in subject.get("brand_company_names", []):
            if brand not in task["brand_company_names"]:
                task["brand_company_names"].append(brand)
            if brand != enterprise_name and not any(item["term"] == brand for item in task["search_terms"]):
                task["search_terms"].append({"term": brand, "term_type": "brand_name"})
        cities = job.get("source_cities") if isinstance(job.get("source_cities"), list) else []
        for city in cities:
            normal_city = normalized_text(city, "source_cities[]")
            if normal_city not in task["report_cities"]:
                task["report_cities"].append(normal_city)
    tasks = [by_key[key] for key in order]
    for task in tasks:
        task["required_queries"] = _required_queries(task)
    return tasks, skipped_jobs


def _load_document(
    run_root: str, config_hash: str, details_hash: str, screening_hash: str,
    skipped_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    path = Path(run_root) / "job-research-data" / "company-research.json"
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "config_hash": config_hash,
            "input_job_details_sha256": details_hash,
            "input_screening_results_sha256": screening_hash,
            "skipped_jobs": skipped_jobs,
            "records": [],
        }
    document = load_json(path)
    if document.get("config_hash") != config_hash:
        raise S1Error("config.json 已变化，不能静默复用旧 S4 结果", "stale_s4_config")
    if document.get("input_job_details_sha256") != details_hash:
        raise S1Error("job-details.json 已变化，不能静默复用旧 S4 结果", "stale_s4_details")
    if document.get("input_screening_results_sha256") != screening_hash:
        raise S1Error("screening-results.json 已变化，不能静默复用旧 S4 结果", "stale_s4_screening")
    if document.get("skipped_jobs") != skipped_jobs:
        raise S1Error("S4 跳过岗位清单与当前企业主体状态不一致", "stale_s4_skipped_jobs")
    return document


def _normal_attempts(value: Any, required_queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise S1Error("query_attempts 必须是最多 100 项的数组", "invalid_query_attempts")
    expected = {
        (item["group"], item["platform_key"], item["search_term"], item["search_term_type"]): item
        for item in required_queries
    }
    seen: set[tuple[str, str, str, str]] = set()
    result = []
    fields = {
        "group", "platform_key", "platform", "search_term", "search_term_type",
        "result_status", "search_url", "content_types_reviewed", "note",
    }
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != fields:
            raise S1Error(f"query_attempts[{index}] 字段不准确", "invalid_query_attempts")
        group = normalized_text(item.get("group"), "query_attempt.group")
        platform_key = normalized_text(item.get("platform_key"), "query_attempt.platform_key")
        platform = normalized_text(item.get("platform"), "query_attempt.platform")
        search_term = normalized_text(item.get("search_term"), "query_attempt.search_term")
        search_term_type = normalized_text(item.get("search_term_type"), "query_attempt.search_term_type")
        key = (group, platform_key, search_term, search_term_type)
        required = expected.get(key)
        if required is None or key in seen:
            raise S1Error("查询记录不在固定清单中或重复", "invalid_query_attempts")
        if platform != required["platform"] or search_term_type not in {"enterprise_name", "brand_name"}:
            raise S1Error("查询平台或关键词类型不一致", "invalid_query_attempts")
        result_status = normalized_text(item.get("result_status"), "query_attempt.result_status")
        if result_status not in ATTEMPT_STATUSES:
            raise S1Error("查询结果状态无效", "invalid_query_attempts")
        content_types = _normal_list(item.get("content_types_reviewed"), "content_types_reviewed", maximum=3)
        if set(content_types) != set(required["required_content_types"]):
            raise S1Error("没有按要求检查帖子和评论", "invalid_query_attempts")
        note = item.get("note")
        if not isinstance(note, str):
            raise S1Error("query_attempt.note 必须是字符串", "invalid_query_attempts")
        note = " ".join(note.split())
        if len(note) > 500 or (result_status == "查询失败" and not note):
            raise S1Error("查询失败必须说明原因，且说明不能过长", "invalid_query_attempts")
        search_url = _https_url(item.get("search_url"), "query_attempt.search_url")
        if platform_key == "aiqicha" and urlsplit(search_url).hostname not in AIQICHA_HOSTS:
            raise S1Error("爱企查查询 URL 与平台不一致", "invalid_query_attempts")
        if platform_key in REVIEW_PLATFORMS:
            parts = urlsplit(search_url)
            if parts.hostname not in REVIEW_PLATFORM_HOSTS[platform_key]:
                raise S1Error("网友评价查询 URL 与平台不一致", "invalid_query_attempts")
            try:
                search_url.encode("ascii")
            except UnicodeEncodeError as exc:
                raise S1Error("网友评价查询 URL 必须使用浏览器返回的规范编码地址", "invalid_query_attempts") from exc
            if search_term not in unquote(search_url):
                raise S1Error("网友评价查询 URL 未包含对应搜索词", "invalid_query_attempts")
        result.append({
            "group": group,
            "platform_key": platform_key,
            "platform": platform,
            "search_term": search_term,
            "search_term_type": search_term_type,
            "result_status": result_status,
            "search_url": search_url,
            "content_types_reviewed": content_types,
            "note": note,
        })
        seen.add(key)
    missing = set(expected) - seen
    if missing:
        raise S1Error(f"固定查询清单未完成：缺少 {len(missing)} 项", "missing_query_attempts")
    return result


def _expected_group_status(group: str, attempts: list[dict[str, Any]]) -> str | None:
    relevant = [item for item in attempts if item["group"] == group]
    if not relevant:
        return None
    if group == "basic_profile":
        aiqicha = next(item for item in relevant if item["platform_key"] == "aiqicha")
        official = next(item for item in relevant if item["platform_key"] == "official_website")
        if aiqicha["result_status"] == "找到相关内容" and official["result_status"] != "查询失败":
            return "已完成"
        if aiqicha["result_status"] == "查询失败" and official["result_status"] != "找到相关内容":
            return "查询失败"
        if aiqicha["result_status"] == "未找到相关内容" and official["result_status"] != "找到相关内容":
            return "查询失败"
        return "部分完成"
    failed = sum(item["result_status"] == "查询失败" for item in relevant)
    if failed == 0:
        return "已完成"
    if failed == len(relevant):
        return "查询失败"
    return "部分完成"


def _required_source_platforms(group: str, attempts: list[dict[str, Any]]) -> set[str]:
    return {
        item["platform_key"] for item in attempts
        if item["group"] == group and item["result_status"] == "找到相关内容"
    }


def _normal_evidence(value: Any, company_key: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > 80:
        raise S1Error("evidence 必须是最多 80 项的数组", "invalid_company_evidence")
    records: list[dict[str, Any]] = []
    by_ref: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(value):
        fields = {
            "source_ref", "group", "platform_key", "source_type", "platform", "title",
            "url", "excerpt", "access_status", "content_type",
        }
        if not isinstance(item, dict) or set(item) != fields:
            raise S1Error(f"evidence[{index}] 字段不准确", "invalid_company_evidence")
        ref = normalized_text(item.get("source_ref"), "source_ref")
        if not SOURCE_REF.fullmatch(ref) or ref in by_ref:
            raise S1Error(f"source_ref 无效或重复：{ref}", "invalid_company_evidence")
        group = normalized_text(item.get("group"), "group")
        platform_key = normalized_text(item.get("platform_key"), "platform_key")
        source_type = normalized_text(item.get("source_type"), "source_type")
        access_status = normalized_text(item.get("access_status"), "access_status")
        content_type = normalized_text(item.get("content_type"), "content_type")
        if (
            group not in GROUPS or platform_key not in PLATFORM_LABELS or source_type not in SOURCE_TYPES
            or access_status not in ACCESS_STATUSES or content_type not in CONTENT_TYPES
        ):
            raise S1Error("来源枚举值无效", "invalid_company_evidence")
        if group == "employee_reviews" and (platform_key not in REVIEW_PLATFORMS or content_type not in {"post", "comment"}):
            raise S1Error("网友评价必须来自规定平台的帖子或评论", "invalid_company_evidence")
        if group == "public_risks" and platform_key == "aiqicha":
            raise S1Error("爱企查风险统计不能作为公开风险证据", "invalid_company_evidence")
        platform = normalized_text(item.get("platform"), "platform")
        title = normalized_text(item.get("title"), "title")
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str):
            raise S1Error("excerpt 必须是字符串", "invalid_company_evidence")
        excerpt = " ".join(excerpt.split())
        if len(title) > 300 or len(platform) > 100 or len(excerpt) > 500:
            raise S1Error("来源标题、平台或摘要过长", "invalid_company_evidence")
        if access_status == "已访问" and not excerpt:
            raise S1Error("已访问来源必须有有限证据摘要", "invalid_company_evidence")
        url = _https_url(item.get("url"), "evidence.url")
        content = {
            "group": group,
            "platform_key": platform_key,
            "source_type": source_type,
            "platform": platform,
            "title": title,
            "url": url,
            "excerpt": excerpt,
            "access_status": access_status,
            "content_type": content_type,
        }
        record = {"evidence_id": sha256_text(f"{company_key}\n{_canonical_hash(content)}"), **content}
        by_ref[ref] = record
        records.append(record)
    return records, by_ref


def _normal_failure(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 20:
        raise S1Error(f"{field} 必须是最多 20 项的数组", "invalid_query_group")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"platform", "url", "reason"}:
            raise S1Error(f"{field}[{index}] 字段不准确", "invalid_query_group")
        url = item.get("url")
        result.append({
            "platform": normalized_text(item.get("platform"), "failure.platform"),
            "url": _https_url(url, "failure.url") if url else None,
            "reason": normalized_text(item.get("reason"), "failure.reason"),
        })
    return result


def _normal_group(
    value: Any, group: str, evidence_by_ref: dict[str, dict[str, Any]], attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    fields = {"query_status", "items", "source_evidence_refs", "failure_evidence"}
    if not isinstance(value, dict) or set(value) != fields:
        raise S1Error(f"{group} 字段不准确", "invalid_query_group")
    query_status = normalized_text(value.get("query_status"), f"{group}.query_status")
    if query_status not in QUERY_STATUSES:
        raise S1Error(f"{group}.query_status 无效", "invalid_query_status")
    expected_status = _expected_group_status(group, attempts)
    if expected_status is not None and query_status != expected_status:
        raise S1Error(f"{group}.query_status 与固定查询记录不一致", "invalid_query_status")
    source_refs = _normal_list(value.get("source_evidence_refs"), f"{group}.source_evidence_refs", maximum=40)
    source_ids = []
    for ref in source_refs:
        evidence = evidence_by_ref.get(ref)
        if evidence is None or evidence["group"] != group:
            raise S1Error(f"{group} 引用了不存在或跨组来源：{ref}", "invalid_company_evidence")
        source_ids.append(evidence["evidence_id"])
    required_platforms = _required_source_platforms(group, attempts)
    actual_platforms = {
        evidence_by_ref[ref]["platform_key"] for ref in source_refs
        if evidence_by_ref[ref]["access_status"] == "已访问"
    }
    if not required_platforms.issubset(actual_platforms):
        raise S1Error(f"{group} 找到内容的平台缺少原始来源", "missing_company_evidence")
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > 40:
        raise S1Error(f"{group}.items 必须是最多 40 项的数组", "invalid_query_group")
    items = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict) or set(item) != {"category", "summary", "evidence_refs"}:
            raise S1Error(f"{group}.items[{index}] 字段不准确", "invalid_query_group")
        category = normalized_text(item.get("category"), "category")
        if category not in CATEGORY_VALUES[group]:
            raise S1Error(f"{group} 事项分类无效：{category}", "invalid_query_group")
        summary = normalized_text(item.get("summary"), "summary")
        if len(summary) > 500:
            raise S1Error("事项摘要不能超过 500 字", "invalid_query_group")
        refs = _normal_list(item.get("evidence_refs"), "evidence_refs", maximum=12)
        if not refs:
            raise S1Error("每个事项必须引用来源", "missing_company_evidence")
        ids = []
        for ref in refs:
            evidence = evidence_by_ref.get(ref)
            if evidence is None or evidence["group"] != group or evidence["access_status"] != "已访问":
                raise S1Error("事项引用了无效、跨组或未访问来源", "invalid_company_evidence")
            if group == "basic_profile" and evidence["platform_key"] not in {"aiqicha", "official_website"}:
                raise S1Error("公司基本信息只能引用爱企查或公司官网", "invalid_company_evidence")
            if evidence["evidence_id"] not in source_ids:
                raise S1Error("事项来源必须同时列入组级来源", "invalid_company_evidence")
            ids.append(evidence["evidence_id"])
        items.append({"category": category, "summary": summary, "evidence_ids": ids})
    failures = _normal_failure(value.get("failure_evidence"), f"{group}.failure_evidence")
    if query_status == "已完成":
        if failures:
            raise S1Error("已完成查询不能有失败证据", "invalid_query_group")
        if expected_status is None and (
            not source_ids or not any(evidence_by_ref[ref]["access_status"] == "已访问" for ref in source_refs)
        ):
            raise S1Error("已完成查询至少需要一个已访问来源", "invalid_query_group")
    elif query_status == "部分完成":
        if not failures:
            raise S1Error("部分完成必须说明失败平台", "invalid_query_group")
    elif items or not failures:
        raise S1Error("查询失败必须无事项并提供失败证据", "invalid_query_group")
    return {
        "query_status": query_status,
        "items": items,
        "source_evidence_ids": source_ids,
        "failure_evidence": failures,
    }


def _derive_status(groups: list[dict[str, Any]]) -> str:
    statuses = [group["query_status"] for group in groups]
    if all(status == "已完成" for status in statuses):
        return "已完成"
    if all(status == "查询失败" for status in statuses):
        return "查询失败"
    return "部分完成"


def _normal_payload(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    prohibited = set(payload).intersection(PROHIBITED_FIELDS)
    if set(payload) != MODEL_FIELDS or prohibited:
        raise S1Error(f"S4 模型字段无效；禁止字段={sorted(prohibited)}", "invalid_company_research")
    company_key = normalized_text(payload.get("company_key"), "company_key")
    if company_key != task["company_key"]:
        raise S1Error("公司身份与首个待处理任务不一致", "company_identity_conflict")
    attempts = _normal_attempts(payload.get("query_attempts"), task["required_queries"])
    evidence, by_ref = _normal_evidence(payload.get("evidence"), company_key)
    groups = {group: _normal_group(payload.get(group), group, by_ref, attempts) for group in GROUPS}
    referenced = {
        evidence_id for group in groups.values() for evidence_id in group["source_evidence_ids"]
    }
    if any(item["evidence_id"] not in referenced for item in evidence):
        raise S1Error("存在未被任何查询组引用的来源", "unused_company_evidence")
    return {
        "record_type": "company_research",
        **{key: value for key, value in task.items() if key != "required_queries"},
        "query_attempts": attempts,
        **groups,
        "evidence": evidence,
        "status": _derive_status(list(groups.values())),
        "revision": 1,
    }


def _validate_persisted_group(
    group: Any, name: str, evidence: dict[str, dict[str, Any]], attempts: list[dict[str, Any]],
) -> None:
    fields = {"query_status", "items", "source_evidence_ids", "failure_evidence"}
    if not isinstance(group, dict) or set(group) != fields or group.get("query_status") not in QUERY_STATUSES:
        raise S1Error(f"持久化 {name} 结构无效", "invalid_company_research")
    expected_status = _expected_group_status(name, attempts)
    if expected_status is not None and group.get("query_status") != expected_status:
        raise S1Error(f"持久化 {name} 状态与查询记录不一致", "invalid_query_status")
    source_ids = _normal_list(group.get("source_evidence_ids"), f"{name}.source_evidence_ids")
    if any(key not in evidence or evidence[key]["group"] != name for key in source_ids):
        raise S1Error(f"{name} 持久化来源引用无效", "invalid_company_evidence")
    required_platforms = _required_source_platforms(name, attempts)
    actual_platforms = {
        evidence[key]["platform_key"] for key in source_ids if evidence[key]["access_status"] == "已访问"
    }
    if not required_platforms.issubset(actual_platforms):
        raise S1Error(f"{name} 找到内容的平台缺少持久化来源", "missing_company_evidence")
    items = group.get("items")
    if not isinstance(items, list):
        raise S1Error(f"{name}.items 不是数组", "invalid_company_research")
    for item in items:
        if not isinstance(item, dict) or set(item) != {"category", "summary", "evidence_ids"}:
            raise S1Error(f"{name} 持久化事项无效", "invalid_company_research")
        if item.get("category") not in CATEGORY_VALUES[name]:
            raise S1Error(f"{name} 持久化事项分类无效", "invalid_company_research")
        ids = _normal_list(item.get("evidence_ids"), f"{name}.evidence_ids", maximum=12)
        if not ids or any(key not in source_ids or evidence[key]["access_status"] != "已访问" for key in ids):
            raise S1Error(f"{name} 持久化事项来源无效", "invalid_company_evidence")
        if name == "basic_profile" and any(evidence[key]["platform_key"] not in {"aiqicha", "official_website"} for key in ids):
            raise S1Error("持久化公司基本信息来源无效", "invalid_company_evidence")
        normalized_text(item.get("summary"), f"{name}.summary")
    failures = _normal_failure(group.get("failure_evidence"), f"{name}.failure_evidence")
    if group["query_status"] == "已完成":
        if failures:
            raise S1Error(f"{name} 已完成状态不能有失败证据", "invalid_query_group")
        if expected_status is None and (
            not source_ids or not any(evidence[key]["access_status"] == "已访问" for key in source_ids)
        ):
            raise S1Error(f"{name} 已完成状态缺少有效来源", "invalid_query_group")
    elif group["query_status"] == "部分完成":
        if not failures:
            raise S1Error(f"{name} 部分完成状态缺少失败证据", "invalid_query_group")
    elif items or not failures:
        raise S1Error(f"{name} 查询失败状态不完整", "invalid_query_group")


def validate_document(
    document: dict[str, Any], config: dict[str, Any], job_details: dict[str, Any],
    screening: dict[str, Any], tasks: list[dict[str, Any]], skipped_jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    top_fields = {
        "schema_version", "config_hash", "input_job_details_sha256",
        "input_screening_results_sha256", "skipped_jobs", "records",
    }
    if set(document) != top_fields or document.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("company-research 顶层结构无效", "invalid_company_research")
    if document.get("config_hash") != config["config_hash"]:
        raise S1Error("company-research 配置哈希不一致", "stale_s4_config")
    if document.get("input_job_details_sha256") != _canonical_hash(job_details):
        raise S1Error("company-research S2 输入哈希不一致", "stale_s4_details")
    if document.get("input_screening_results_sha256") != _canonical_hash(screening):
        raise S1Error("company-research S3 输入哈希不一致", "stale_s4_screening")
    if document.get("skipped_jobs") != skipped_jobs:
        raise S1Error("company-research 跳过岗位清单无效", "invalid_s4_skipped_jobs")
    for item in skipped_jobs:
        if not isinstance(item, dict) or set(item) != SKIPPED_JOB_FIELDS:
            raise S1Error("company-research 跳过岗位字段无效", "invalid_s4_skipped_jobs")
    task_by_key = {task["company_key"]: task for task in tasks}
    records = document.get("records")
    if not isinstance(records, list):
        raise S1Error("company-research records 不是数组", "invalid_company_research")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != PERSISTED_RECORD_FIELDS:
            raise S1Error("公司背调记录字段不准确", "invalid_company_research")
        key = normalized_text(record.get("company_key"), "company_key")
        task = task_by_key.get(key)
        if task is None or key in seen:
            raise S1Error("公司背调身份无效或重复", "invalid_company_research")
        if index >= len(tasks) or tasks[index]["company_key"] != key:
            raise S1Error("公司背调记录顺序无效", "job_order_mismatch")
        seen.add(key)
        for field in (
            "company_key", "enterprise_name", "unified_social_credit_code",
            "boss_company_subject_keys", "brand_company_names", "linked_job_keys", "report_cities", "search_terms",
        ):
            if record.get(field) != task[field]:
                raise S1Error(f"公司背调身份字段变化：{field}", "company_identity_conflict")
        raw_evidence = record.get("evidence")
        if not isinstance(raw_evidence, list):
            raise S1Error("持久化 evidence 不是数组", "invalid_company_evidence")
        evidence: dict[str, dict[str, Any]] = {}
        for item in raw_evidence:
            expected_fields = {
                "evidence_id", "group", "platform_key", "source_type", "platform", "title",
                "url", "excerpt", "access_status", "content_type",
            }
            if not isinstance(item, dict) or set(item) != expected_fields:
                raise S1Error("持久化来源字段不准确", "invalid_company_evidence")
            evidence_id = normalized_text(item.get("evidence_id"), "evidence_id")
            content = {field: item[field] for field in expected_fields - {"evidence_id"}}
            if evidence_id != sha256_text(f"{key}\n{_canonical_hash(content)}") or evidence_id in evidence:
                raise S1Error("持久化来源 ID 无效或重复", "invalid_company_evidence")
            _https_url(item.get("url"), "evidence.url")
            if (
                item.get("group") not in GROUPS or item.get("platform_key") not in PLATFORM_LABELS
                or item.get("source_type") not in SOURCE_TYPES or item.get("access_status") not in ACCESS_STATUSES
                or item.get("content_type") not in CONTENT_TYPES
            ):
                raise S1Error("持久化来源枚举无效", "invalid_company_evidence")
            if item.get("group") == "employee_reviews" and (
                item.get("platform_key") not in REVIEW_PLATFORMS or item.get("content_type") not in {"post", "comment"}
            ):
                raise S1Error("持久化网友评价来源无效", "invalid_company_evidence")
            evidence[evidence_id] = item
        attempts = _normal_attempts(record.get("query_attempts"), task["required_queries"])
        if attempts != record.get("query_attempts"):
            raise S1Error("持久化查询记录未规范化", "invalid_query_attempts")
        groups = [record[name] for name in GROUPS]
        for name in GROUPS:
            _validate_persisted_group(record.get(name), name, evidence, attempts)
        if record.get("status") not in RESEARCH_STATUSES or record.get("status") != _derive_status(groups):
            raise S1Error("公司背调阶段状态无效", "invalid_company_research")
        revision = record.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise S1Error("公司背调 revision 无效", "invalid_company_research")
    return {
        "ok": True,
        "researched_companies": len(seen),
        "pending_companies": len(tasks) - len(seen),
        "skipped_jobs": len(skipped_jobs),
        "skipped_job_keys": [item["job_key"] for item in skipped_jobs],
        "status_counts": {status: sum(record["status"] == status for record in records) for status in RESEARCH_STATUSES},
    }


def status(run_root: str) -> dict[str, Any]:
    config, details, screening, _, tasks, skipped_jobs, details_hash, screening_hash = _load_inputs(run_root)
    document = _load_document(run_root, config["config_hash"], details_hash, screening_hash, skipped_jobs)
    result = validate_document(document, config, details, screening, tasks, skipped_jobs)
    processed = {record["company_key"] for record in document["records"]}
    result["next_company_key"] = next((task["company_key"] for task in tasks if task["company_key"] not in processed), None)
    return result


def pending(run_root: str, limit: int) -> dict[str, Any]:
    if limit < 1:
        raise S1Error("limit 必须大于零", "invalid_limit")
    config, details, screening, _, tasks, skipped_jobs, details_hash, screening_hash = _load_inputs(run_root)
    document = _load_document(run_root, config["config_hash"], details_hash, screening_hash, skipped_jobs)
    validate_document(document, config, details, screening, tasks, skipped_jobs)
    processed = {record["company_key"] for record in document["records"]}
    values = [task for task in tasks if task["company_key"] not in processed][:limit]
    return {
        "ok": True,
        "company_preferences": config["company_preferences"],
        "pending": values,
        "remaining": len(tasks) - len(processed),
        "skipped_jobs": skipped_jobs,
    }


def upsert(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    config, details, screening, _, tasks, skipped_jobs, details_hash, screening_hash = _load_inputs(run_root)
    document = _load_document(run_root, config["config_hash"], details_hash, screening_hash, skipped_jobs)
    validate_document(document, config, details, screening, tasks, skipped_jobs)
    processed = {record["company_key"] for record in document["records"]}
    task = next((item for item in tasks if item["company_key"] not in processed), None)
    if task is None:
        raise S1Error("S4 已完成，没有待处理公司", "s4_complete")
    if payload.get("company_key") != task["company_key"]:
        raise S1Error("S4 只接受首个待背调公司", "company_order_mismatch")
    document["records"].append(_normal_payload(payload, task))
    validate_document(document, config, details, screening, tasks, skipped_jobs)
    atomic_write_json(Path(run_root) / "job-research-data" / "company-research.json", document)
    return status(run_root)


def _read_input(path: str) -> dict[str, Any]:
    value = strict_json_loads(sys.stdin.read()) if path == "-" else load_json(path)
    if not isinstance(value, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    pending_parser = sub.add_parser("pending")
    pending_parser.add_argument("--run-root", required=True)
    pending_parser.add_argument("--task-id", required=True)
    pending_parser.add_argument("--limit", type=int, default=1)
    upsert_parser = sub.add_parser("upsert")
    upsert_parser.add_argument("--run-root", required=True)
    upsert_parser.add_argument("--task-id", required=True)
    upsert_parser.add_argument("--input", default="-")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--run-root", required=True)
    validate_parser.add_argument("--task-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    from task_manager import validate_task

    validate_task(args.run_root, args.task_id)
    if args.command == "pending":
        result = pending(args.run_root, args.limit)
    elif args.command == "upsert":
        result = upsert(args.run_root, _read_input(args.input))
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
