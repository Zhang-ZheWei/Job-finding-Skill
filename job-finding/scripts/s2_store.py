#!/usr/bin/env python3
"""校验 S2 临时详情和模型摘要，并原子写入 job-details.json。"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from s1_common import (
    S1Error,
    atomic_write_json,
    load_json,
    normalize_job_url,
    normalized_text,
    sha256_text,
    strict_json_loads,
)
from s1_store import validate_run_documents


SCHEMA_VERSION = 2
SUMMARY_FIELDS = (
    "core_responsibilities",
    "hard_requirements",
    "key_capability_and_tool_requirements",
    "work_style_and_risks",
    "missing_or_uncertain",
)
EVIDENCE_CATEGORIES = {
    "responsibility",
    "requirement",
    "capability",
    "work_style",
    "uncertainty",
}
DETAIL_SELECTORS = {".job-detail-section", ".job-detail"}
PROHIBITED_FIELDS = {"jd_text", "body", "html", "full_text", "full_jd", "page_text"}
USCC = re.compile(r"^[0-9ABCDEFGHJKLMNPQRTUWXY]{18}$")
COMPANY_PATH = re.compile(r"^/gongsi/([A-Za-z0-9_~-]+)\.html$")


def _empty_summary() -> dict[str, list[str]]:
    return {field: [] for field in SUMMARY_FIELDS}


def _collapse(value: Any, field: str, *, allow_empty: bool = False) -> str:
    text = normalized_text(value, field, allow_empty=allow_empty)
    return re.sub(r"\s+", " ", text).strip()


def _canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _job_record_hash(record: dict[str, Any]) -> str:
    """只绑定 S2 实际依赖的 S1 岗位事实，允许后续追加召回来源。"""
    projection = {
        "job_key": record.get("job_key"),
        "job_id": record.get("job_id"),
        "boss_job_url": record.get("boss_job_url"),
        "job_title": record.get("job_title"),
        "brand_company_name": record.get("brand_company_name"),
    }
    return _canonical_hash(projection)


def _assert_no_prohibited(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        invalid = PROHIBITED_FIELDS.intersection(value)
        if invalid:
            raise S1Error(f"{path} 含有禁止落盘字段：{sorted(invalid)}", "scope_violation")
        for key, item in value.items():
            _assert_no_prohibited(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_prohibited(item, f"{path}[{index}]")


def _normalize_company_url(value: Any) -> str:
    raw = normalized_text(value, "company_page_url")
    parsed = urlsplit(urljoin("https://www.zhipin.com", raw))
    if parsed.scheme != "https" or parsed.netloc != "www.zhipin.com" or not COMPANY_PATH.fullmatch(parsed.path):
        raise S1Error(f"BOSS 公司页 URL 无效：{raw}", "invalid_company_url")
    return f"https://www.zhipin.com{parsed.path}"


def _load_job_index(run_root: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    s1_status = validate_run_documents(run_root)
    job_index = load_json(Path(run_root) / "job-research-data" / "job-index.json")
    if job_index.get("collection_mode") == "exhaustive" and s1_status["pending_combinations"]:
        raise S1Error("完整采集模式必须先完成全部 S1 组合", "s1_not_complete")
    records = job_index.get("records")
    if not isinstance(records, list):
        raise S1Error("job-index.json 缺少 records 数组", "invalid_job_index")
    by_key: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise S1Error(f"job-index records[{index}] 不是对象", "invalid_job_index")
        key = normalized_text(record.get("job_key"), "job_key")
        if key in by_key:
            raise S1Error(f"job-index 存在重复 job_key：{key}", "invalid_job_index")
        normalize_job_url(record.get("boss_job_url"))
        by_key[key] = record
    return job_index, by_key, _canonical_hash(job_index)


def _load_details(run_root: str, input_hash: str, config_hash: str) -> dict[str, Any]:
    path = Path(run_root) / "job-research-data" / "job-details.json"
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "config_hash": config_hash,
            "input_job_index_sha256": input_hash,
            "records": [],
        }
    document = load_json(path)
    if document.get("config_hash") != config_hash:
        raise S1Error("job-details.json 与 config.json 不属于同一任务", "config_hash_mismatch")
    if document.get("input_job_index_sha256") != input_hash:
        job_index = load_json(Path(run_root) / "job-research-data" / "job-index.json")
        validate_document(document, job_index, allow_input_hash_mismatch=True)
        document["input_job_index_sha256"] = input_hash
        atomic_write_json(path, document)
    return document


def _normal_summary(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise S1Error("semantic.summary 必须是对象", "invalid_summary")
    if set(value) != set(SUMMARY_FIELDS):
        raise S1Error(f"summary 字段必须准确为：{list(SUMMARY_FIELDS)}", "invalid_summary")
    result: dict[str, list[str]] = {}
    for field in SUMMARY_FIELDS:
        items = value[field]
        if not isinstance(items, list) or len(items) > 12:
            raise S1Error(f"summary.{field} 必须是最多 12 项的数组", "invalid_summary")
        normalized_items = [_collapse(item, f"summary.{field}[]") for item in items]
        if len(set(normalized_items)) != len(normalized_items):
            raise S1Error(f"summary.{field} 含重复内容", "invalid_summary")
        result[field] = normalized_items
    return result


def _normal_evidence(job_key: str, value: Any, jd_text: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise S1Error("semantic.evidence 必须是 1 到 20 项的数组", "invalid_evidence")
    normalized_jd = _collapse(jd_text, "jd_text")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise S1Error(f"evidence[{index}] 必须是对象", "invalid_evidence")
        category = normalized_text(item.get("category"), "evidence.category")
        if category not in EVIDENCE_CATEGORIES:
            raise S1Error(f"evidence.category 无效：{category}", "invalid_evidence")
        text = _collapse(item.get("text"), "evidence.text")
        if len(text) > 300 or text not in normalized_jd:
            raise S1Error("证据必须是当前 JD 中不超过 300 字的原文片段", "evidence_not_in_jd")
        identity = (category, text)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({
            "evidence_id": sha256_text(f"{job_key}\n{category}\n{text}"),
            "category": category,
            "text": text,
        })
    if not result:
        raise S1Error("去重后没有有效证据", "invalid_evidence")
    return result


def _normal_failure(value: Any, fallback_code: str, fallback_message: str) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    return {
        "code": _collapse(value.get("code", fallback_code), "failure.code"),
        "message": _collapse(value.get("message", fallback_message), "failure.message"),
    }


def _business_from_browser(company: dict[str, Any]) -> dict[str, Any]:
    status = company.get("status")
    if status == "acquired":
        fields = company.get("fields")
        if not isinstance(fields, dict):
            raise S1Error("公司工商 fields 必须是对象", "invalid_business")
        enterprise_name = _collapse(fields.get("企业名称"), "企业名称")
        uscc = _collapse(fields.get("统一社会信用代码", ""), "统一社会信用代码", allow_empty=True).upper()
        if uscc and not USCC.fullmatch(uscc):
            raise S1Error(f"统一社会信用代码格式无效：{uscc}", "invalid_uscc")
        return {
            "status": "已取得",
            "source_selector": ".job-sec.company-business",
            "enterprise_name": enterprise_name,
            "unified_social_credit_code": uscc,
            "legal_representative": _collapse(fields.get("法定代表人", ""), "法定代表人", allow_empty=True),
            "established_at": _collapse(fields.get("成立时间", ""), "成立时间", allow_empty=True),
            "registered_capital": _collapse(fields.get("注册资本", ""), "注册资本", allow_empty=True),
            "registered_address": _collapse(fields.get("注册地址", ""), "注册地址", allow_empty=True),
            "failure": None,
        }
    if status == "not_found":
        return {
            "status": "未取得",
            "source_selector": company.get("source_selector"),
            "enterprise_name": "",
            "unified_social_credit_code": "",
            "legal_representative": "",
            "established_at": "",
            "registered_capital": "",
            "registered_address": "",
            "failure": None,
        }
    if status == "failed":
        result = _business_from_browser({"status": "not_found"})
        result["status"] = "查询失败"
        result["failure"] = _normal_failure(company.get("failure"), "company_read_failed", "公司页读取失败")
        return result
    raise S1Error(f"公司读取状态无效：{status}", "invalid_company_status")


def _subject_from_browser(job: dict[str, Any], company_url: str, company: dict[str, Any]) -> dict[str, Any]:
    key = f"boss-company:{sha256_text(company_url)}"
    return {
        "record_type": "boss_company_subject",
        "boss_company_subject_key": key,
        "company_page_url": company_url,
        "company_page_url_hash": sha256_text(company_url),
        "brand_company_names": [normalized_text(job.get("brand_company_name"), "brand_company_name")],
        "business": _business_from_browser(company),
        "linked_job_keys": [normalized_text(job.get("job_key"), "job_key")],
        "conflicts": [],
        "revision": 1,
    }


def _merge_subject(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(existing, ensure_ascii=False))
    before = _canonical_hash(result)
    result["brand_company_names"] = sorted(set(result.get("brand_company_names", [])) | set(incoming["brand_company_names"]))
    result["linked_job_keys"] = sorted(set(result.get("linked_job_keys", [])) | set(incoming["linked_job_keys"]))
    old_business = result.get("business", {})
    new_business = incoming.get("business", {})
    if old_business.get("status") != "已取得" and new_business.get("status") == "已取得":
        result["business"] = new_business
    elif old_business.get("status") in {"已取得", "来源冲突"} and new_business.get("status") == "已取得":
        conflicts = list(result.get("conflicts", []))
        for field in (
            "enterprise_name", "unified_social_credit_code", "legal_representative",
            "established_at", "registered_capital", "registered_address",
        ):
            old_value = old_business.get(field, "")
            new_value = new_business.get(field, "")
            if old_value and new_value and old_value != new_value:
                item = {"field": field, "existing": old_value, "incoming": new_value}
                if item not in conflicts:
                    conflicts.append(item)
        if conflicts or old_business.get("status") == "来源冲突":
            result["conflicts"] = conflicts
            result["business"] = dict(old_business)
            result["business"]["status"] = "来源冲突"
    if _canonical_hash(result) != before:
        result["revision"] = int(existing.get("revision", 1)) + 1
    return result


def upsert(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    job_index, jobs, input_hash = _load_job_index(run_root)
    config_hash = normalized_text(job_index.get("config_hash"), "config_hash")
    document = _load_details(run_root, input_hash, config_hash)
    validate_document(document, job_index)
    browser = payload.get("browser")
    semantic = payload.get("semantic")
    if not isinstance(browser, dict):
        raise S1Error("input.browser 必须是对象", "invalid_browser_payload")
    job_key = normalized_text(browser.get("job_key"), "job_key")
    job = jobs.get(job_key)
    if job is None:
        raise S1Error(f"S1 中不存在岗位：{job_key}", "job_not_found")
    processed = {
        record["job_key"] for record in document["records"]
        if isinstance(record, dict) and record.get("record_type") == "job_detail"
    }
    expected_job_key = next((key for key in jobs if key not in processed), None)
    if job_key != expected_job_key:
        raise S1Error("只能提交 S2 首个待处理岗位", "job_order_mismatch")
    if browser.get("job_id") != job.get("job_id"):
        raise S1Error("浏览器岗位 ID 与 S1 不一致", "job_identity_conflict")

    detail = browser.get("detail")
    if not isinstance(detail, dict):
        raise S1Error("browser.detail 必须是对象", "invalid_browser_payload")
    detail_status = detail.get("status")
    final_url, final_job_id = normalize_job_url(detail.get("final_url"))
    expected_url, expected_job_id = normalize_job_url(job.get("boss_job_url"))
    if final_job_id != expected_job_id or final_url != expected_url:
        raise S1Error("详情页 URL 与 S1 岗位不一致", "job_identity_conflict")

    boss_company_name = _collapse(detail.get("boss_company_name", ""), "boss_company_name", allow_empty=True)
    page_job_title = _collapse(detail.get("page_job_title", ""), "page_job_title", allow_empty=True)
    expected_job_title = _collapse(job.get("job_title"), "job_title")
    if page_job_title and page_job_title != expected_job_title:
        raise S1Error(
            f"详情页岗位名称与 S1 不一致：{page_job_title} != {expected_job_title}",
            "job_title_conflict",
        )
    company_url_raw = detail.get("company_page_url")
    company_url = _normalize_company_url(company_url_raw) if company_url_raw else None
    subject_key = f"boss-company:{sha256_text(company_url)}" if company_url else None

    if detail_status == "ok":
        jd_text = _collapse(detail.get("jd_text"), "jd_text")
        selector = normalized_text(detail.get("selector"), "selector")
        if selector not in DETAIL_SELECTORS:
            raise S1Error(f"详情选择器不允许：{selector}", "invalid_detail_selector")
        if not isinstance(semantic, dict):
            raise S1Error("详情成功时必须提供 semantic 摘要", "missing_semantic_summary")
        summary = _normal_summary(semantic.get("summary"))
        evidence = _normal_evidence(job_key, semantic.get("evidence"), jd_text)
        detail_record = {
            "record_type": "job_detail",
            "job_key": job_key,
            "input_job_record_hash": _job_record_hash(job),
            "job_identity_hash": sha256_text(f"{job_key}\n{expected_url}"),
            "status": "已完成",
            "source": {
                "url": expected_url,
                "selector": selector,
                "content_fingerprint": sha256_text(jd_text),
            },
            "boss_company_name": boss_company_name,
            "boss_company_subject_key": subject_key,
            "summary": summary,
            "evidence": evidence,
            "failure": None,
        }
    elif detail_status in {"failed", "needs_review"}:
        detail_record = {
            "record_type": "job_detail",
            "job_key": job_key,
            "input_job_record_hash": _job_record_hash(job),
            "job_identity_hash": sha256_text(f"{job_key}\n{expected_url}"),
            "status": "失败" if detail_status == "failed" else "需人工复核",
            "source": {"url": expected_url, "selector": None, "content_fingerprint": None},
            "boss_company_name": boss_company_name,
            "boss_company_subject_key": subject_key,
            "summary": _empty_summary(),
            "evidence": [],
            "failure": _normal_failure(detail.get("failure"), "detail_read_failed", "岗位详情读取失败"),
        }
    else:
        raise S1Error(f"岗位详情状态无效：{detail_status}", "invalid_detail_status")

    records = document.get("records")
    if not isinstance(records, list):
        raise S1Error("job-details.json 缺少 records 数组", "invalid_job_details")
    detail_by_key = {
        record["job_key"]: record for record in records
        if isinstance(record, dict) and record.get("record_type") == "job_detail"
    }
    subject_by_key = {
        record["boss_company_subject_key"]: record for record in records
        if isinstance(record, dict) and record.get("record_type") == "boss_company_subject"
    }
    previous_detail = detail_by_key.get(job_key)
    previous_subject_key = previous_detail.get("boss_company_subject_key") if previous_detail else None
    if previous_subject_key and previous_subject_key != subject_key and previous_subject_key in subject_by_key:
        previous_subject = subject_by_key[previous_subject_key]
        previous_subject["linked_job_keys"] = [
            key for key in previous_subject.get("linked_job_keys", []) if key != job_key
        ]
        if previous_subject["linked_job_keys"]:
            previous_subject["revision"] = int(previous_subject.get("revision", 1)) + 1
        else:
            del subject_by_key[previous_subject_key]
    detail_by_key[job_key] = detail_record

    company = browser.get("company", {"status": "not_applicable"})
    if not isinstance(company, dict):
        raise S1Error("browser.company 必须是对象", "invalid_browser_payload")
    if company_url:
        if company.get("status") == "reused":
            if subject_key not in subject_by_key:
                raise S1Error("公司页标记复用，但 job-details 中没有对应主体", "missing_company_subject")
            incoming = json.loads(json.dumps(subject_by_key[subject_key], ensure_ascii=False))
            incoming["brand_company_names"] = [normalized_text(job.get("brand_company_name"), "brand_company_name")]
            incoming["linked_job_keys"] = [job_key]
        else:
            reported_url = _normalize_company_url(company.get("company_page_url"))
            if reported_url != company_url:
                raise S1Error("公司读取结果 URL 与岗位公司链接不一致", "company_identity_conflict")
            incoming = _subject_from_browser(job, company_url, company)
        if subject_key in subject_by_key:
            subject_by_key[subject_key] = _merge_subject(subject_by_key[subject_key], incoming)
        else:
            subject_by_key[subject_key] = incoming
    elif company.get("status") != "not_applicable":
        raise S1Error("岗位没有公司页 URL，却返回了公司读取结果", "company_identity_conflict")

    order = {record["job_key"]: index for index, record in enumerate(job_index["records"])}
    document["records"] = (
        sorted(detail_by_key.values(), key=lambda record: order[record["job_key"]])
        + sorted(subject_by_key.values(), key=lambda record: record["boss_company_subject_key"])
    )
    _assert_no_prohibited(document)
    validate_document(document, job_index)
    atomic_write_json(Path(run_root) / "job-research-data" / "job-details.json", document)
    return status(run_root)


def validate_document(
    document: dict[str, Any], job_index: dict[str, Any], *, allow_input_hash_mismatch: bool = False,
) -> dict[str, Any]:
    _assert_no_prohibited(document)
    if document.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("job-details schema_version 无效", "invalid_job_details")
    if document.get("config_hash") != job_index.get("config_hash"):
        raise S1Error("job-details 与 job-index 配置哈希不一致", "config_hash_mismatch")
    if not allow_input_hash_mismatch and document.get("input_job_index_sha256") != _canonical_hash(job_index):
        raise S1Error("job-details 输入哈希与 job-index 不一致", "stale_s2_input")
    job_records = job_index.get("records")
    records = document.get("records")
    if not isinstance(job_records, list) or not isinstance(records, list):
        raise S1Error("S1 或 S2 records 不是数组", "invalid_job_details")
    jobs = {record["job_key"]: record for record in job_records}
    details: dict[str, dict[str, Any]] = {}
    subjects: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise S1Error("job-details 记录不是对象", "invalid_job_details")
        record_type = record.get("record_type")
        if record_type == "job_detail":
            key = normalized_text(record.get("job_key"), "job_key")
            if key not in jobs or key in details:
                raise S1Error(f"岗位详情引用无效或重复：{key}", "invalid_job_details")
            if record.get("input_job_record_hash") != _job_record_hash(jobs[key]):
                raise S1Error(f"岗位详情引用的 S1 岗位事实已经变化：{key}", "stale_s2_record")
            expected_url, _ = normalize_job_url(jobs[key].get("boss_job_url"))
            source = record.get("source")
            if not isinstance(source, dict) or source.get("url") != expected_url:
                raise S1Error(f"岗位详情来源 URL 不一致：{key}", "invalid_job_details")
            current_status = record.get("status")
            if current_status == "已完成":
                if source.get("selector") not in DETAIL_SELECTORS or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("content_fingerprint", ""))):
                    raise S1Error(f"已完成岗位缺少合法来源：{key}", "invalid_job_details")
                _normal_summary(record.get("summary"))
                evidence = record.get("evidence")
                if not isinstance(evidence, list) or not evidence or record.get("failure") is not None:
                    raise S1Error(f"已完成岗位证据或失败字段无效：{key}", "invalid_job_details")
                evidence_ids: set[str] = set()
                for item in evidence:
                    if not isinstance(item, dict):
                        raise S1Error(f"岗位证据不是对象：{key}", "invalid_job_details")
                    category = normalized_text(item.get("category"), "evidence.category")
                    text = _collapse(item.get("text"), "evidence.text")
                    evidence_id = normalized_text(item.get("evidence_id"), "evidence_id")
                    expected_id = sha256_text(f"{key}\n{category}\n{text}")
                    if category not in EVIDENCE_CATEGORIES or evidence_id != expected_id or evidence_id in evidence_ids:
                        raise S1Error(f"岗位证据身份无效或重复：{key}", "invalid_job_details")
                    evidence_ids.add(evidence_id)
            elif current_status in {"失败", "需人工复核"}:
                if record.get("summary") != _empty_summary() or record.get("evidence") != [] or not isinstance(record.get("failure"), dict):
                    raise S1Error(f"失败岗位必须使用空摘要并保存失败原因：{key}", "invalid_job_details")
            else:
                raise S1Error(f"岗位详情终态无效：{current_status}", "invalid_job_details")
            details[key] = record
        elif record_type == "boss_company_subject":
            key = normalized_text(record.get("boss_company_subject_key"), "boss_company_subject_key")
            company_url = _normalize_company_url(record.get("company_page_url"))
            if key != f"boss-company:{sha256_text(company_url)}" or key in subjects:
                raise S1Error(f"BOSS 公司主体身份无效或重复：{key}", "invalid_company_subject")
            business = record.get("business")
            if not isinstance(business, dict) or business.get("status") not in {"已取得", "未取得", "查询失败", "来源冲突"}:
                raise S1Error(f"公司工商状态无效：{key}", "invalid_company_subject")
            if business.get("status") in {"已取得", "来源冲突"} and not business.get("enterprise_name"):
                raise S1Error(f"已取得工商信息缺少企业名称：{key}", "invalid_company_subject")
            uscc = business.get("unified_social_credit_code", "")
            if uscc and not USCC.fullmatch(str(uscc)):
                raise S1Error(f"公司主体统一社会信用代码无效：{key}", "invalid_company_subject")
            if business.get("status") in {"已取得", "来源冲突"} and business.get("source_selector") != ".job-sec.company-business":
                raise S1Error(f"公司主体来源选择器无效：{key}", "invalid_company_subject")
            if not isinstance(record.get("linked_job_keys"), list) or not record["linked_job_keys"]:
                raise S1Error(f"公司主体没有关联岗位：{key}", "invalid_company_subject")
            subjects[key] = record
        else:
            raise S1Error(f"未知 record_type：{record_type}", "invalid_job_details")

    for job_key, detail in details.items():
        subject_key = detail.get("boss_company_subject_key")
        if subject_key:
            subject = subjects.get(subject_key)
            if subject is None or job_key not in subject.get("linked_job_keys", []):
                raise S1Error(f"岗位与公司主体引用不对称：{job_key}", "invalid_company_reference")
    for subject_key, subject in subjects.items():
        for job_key in subject.get("linked_job_keys", []):
            if job_key not in details or details[job_key].get("boss_company_subject_key") != subject_key:
                raise S1Error(f"公司主体反向引用无效：{subject_key} -> {job_key}", "invalid_company_reference")
    return {
        "ok": True,
        "job_details": len(details),
        "company_subjects": len(subjects),
        "company_subject_status_counts": {
            value: sum(subject.get("business", {}).get("status") == value for subject in subjects.values())
            for value in ("已取得", "未取得", "查询失败", "来源冲突")
        },
        "pending_jobs": len(jobs) - len(details),
    }


def status(run_root: str) -> dict[str, Any]:
    job_index, jobs, input_hash = _load_job_index(run_root)
    document = _load_details(
        run_root,
        input_hash,
        normalized_text(job_index.get("config_hash"), "config_hash"),
    )
    result = validate_document(document, job_index)
    processed = {
        record["job_key"] for record in document["records"]
        if record.get("record_type") == "job_detail"
    }
    result["next_job_key"] = next((key for key in jobs if key not in processed), None)
    return result


def pending(run_root: str, limit: int) -> dict[str, Any]:
    if limit < 1:
        raise S1Error("limit 必须大于零", "invalid_limit")
    job_index, jobs, input_hash = _load_job_index(run_root)
    document = _load_details(
        run_root,
        input_hash,
        normalized_text(job_index.get("config_hash"), "config_hash"),
    )
    validate_document(document, job_index)
    processed = {
        record["job_key"] for record in document["records"]
        if record.get("record_type") == "job_detail"
    }
    values = [
        {"job_key": key, "job_id": job["job_id"], "boss_job_url": job["boss_job_url"], "job_title": job["job_title"]}
        for key, job in jobs.items() if key not in processed
    ][:limit]
    return {"ok": True, "pending": values, "remaining": len(jobs) - len(processed)}


def _read_input(path: str) -> dict[str, Any]:
    result = strict_json_loads(sys.stdin.read()) if path == "-" else load_json(path)
    if not isinstance(result, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return result


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
