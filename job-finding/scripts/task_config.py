#!/usr/bin/env python3
"""规范化 S0 已确认的岗位搜索配置，并安全写入 config.json。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_plus, urlsplit

from s1_common import S1Error, atomic_write_json, combination_key, load_json, normalized_text, sha256_text, strict_json_loads
from task_manager import bind_config, validate_task


SCHEMA_VERSION = 2
TOP_LEVEL_FIELDS = {
    "schema_version",
    "config_hash",
    "information_sources",
    "candidate_profile",
    "job_target",
    "company_preferences",
    "search_scope",
}
SOURCE_TYPES = {"resume", "career_profile", "user_statement"}
EXPERIENCE_TYPES = {
    "employment", "internship", "project", "research", "leadership",
    "military", "volunteer", "other",
}
CAPABILITY_CATEGORIES = {
    "technical", "product_business", "solution_delivery",
    "communication_management", "domain_knowledge", "language", "other",
}
CREDENTIAL_TYPES = {"certificate", "award", "patent", "publication", "language", "other"}
WORK_FEATURE_SCOPES = {"responsibility", "capability_use", "work_style", "growth", "industry", "other"}
PREFERENCE_SCOPES = WORK_FEATURE_SCOPES | {"location", "compensation", "company_type"}
EXCLUSION_SCOPES = {
    "core_responsibility", "qualification", "work_style", "industry",
    "location", "compensation", "company_type", "other",
}
COMPANY_CATEGORIES = {"industry", "size", "stage", "business_model", "culture", "stability", "growth", "other"}
RISK_CATEGORIES = {"legal", "employment", "financial", "reputation", "management", "other"}
BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
PARTIAL_DATE = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2]))?$")


def _canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _plain_int(value: Any, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise S1Error(f"{field} 必须是大于等于 {minimum} 的整数", "invalid_number")
    return value


def _strict_decode(raw: str, field: str) -> str:
    if BAD_PERCENT.search(raw):
        raise S1Error(f"{field} 包含无效百分号编码", "invalid_percent_encoding")
    try:
        return unquote_plus(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise S1Error(f"{field} 不是有效 UTF-8 URL 编码", "invalid_percent_encoding") from exc


def _analyse_url(value: Any) -> dict[str, Any]:
    url = normalized_text(value, "search_url")
    if CONTROL_OR_SPACE.search(url):
        raise S1Error("BOSS URL 含空格或控制字符", "invalid_search_url")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise S1Error(f"BOSS URL 无法解析：{exc}", "invalid_search_url") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.zhipin.com"
        or parsed.path != "/web/geek/jobs"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise S1Error("必须使用 https://www.zhipin.com/web/geek/jobs", "invalid_search_url")
    without_fragment = url.split("#", 1)[0]
    if "?" not in without_fragment:
        raise S1Error("BOSS URL 必须包含城市筛选参数", "invalid_search_url")
    prefix, raw_query = without_fragment.split("?", 1)
    segments = []
    cities = []
    query_count = 0
    for index, segment in enumerate(raw_query.split("&")):
        raw_name, separator, raw_value = segment.partition("=")
        name = _strict_decode(raw_name, f"query[{index}].name")
        decoded_value = _strict_decode(raw_value, f"query[{index}].value")
        is_query = name == "query"
        if name == "city":
            cities.append(decoded_value)
        if is_query:
            query_count += 1
        segments.append({
            "raw_name": raw_name,
            "separator": separator,
            "raw_value": raw_value,
            "is_query": is_query,
        })
    if len(cities) != 1 or not cities[0].strip():
        raise S1Error("BOSS URL 必须包含且只包含一个非空 city 参数", "invalid_city_parameter")
    if query_count > 1:
        raise S1Error("BOSS URL 不能包含多个 query 参数", "invalid_query_parameter")
    return {
        "prefix": prefix,
        "segments": segments,
        "city": normalized_text(cities[0], "city"),
        "url": without_fragment,
    }


def _replace_query(url: str, encoded_term: str) -> str:
    analysed = _analyse_url(url)
    rendered = []
    found = False
    for segment in analysed["segments"]:
        if segment["is_query"]:
            rendered.append(f"{segment['raw_name']}={encoded_term}")
            found = True
        else:
            rendered.append(segment["raw_name"] + segment["separator"] + segment["raw_value"])
    if not found:
        rendered.append(f"query={encoded_term}")
    return analysed["prefix"] + "?" + "&".join(rendered)


def inspect_search_url(value: Any, city_label: Any, order: int = 0) -> dict[str, Any]:
    analysed = _analyse_url(value)
    return {
        "url": analysed["url"],
        "city_label": normalized_text(city_label, "city_label"),
        "city": analysed["city"],
        "order": _plain_int(order, "order"),
        "search_base": _replace_query(analysed["url"], ""),
    }


def build_search_url(search_base: str, term: str) -> str:
    normal_term = normalized_text(term, "term")
    encoded = quote(normal_term, safe="", encoding="utf-8", errors="strict")
    return _replace_query(search_base, encoded)


def _check_object(value: Any, field: str, allowed: set[str], required: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise S1Error(f"{field} 必须是对象", "invalid_config")
    unexpected = set(value) - allowed
    missing = (required or allowed) - set(value)
    if unexpected or missing:
        raise S1Error(
            f"{field} 字段不正确；多余={sorted(unexpected)}，缺少={sorted(missing)}",
            "invalid_config",
        )
    return value


def _string_list(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum: int = 40,
    max_length: int = 500,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise S1Error(f"{field} 必须是最多 {maximum} 项的数组", "invalid_config")
    result = []
    for item in value:
        text = normalized_text(item, f"{field}[]")
        if len(text) > max_length:
            raise S1Error(f"{field} 单项不能超过 {max_length} 字", "invalid_config")
        if text not in result:
            result.append(text)
    if required and not result:
        raise S1Error(f"{field} 不能为空", "invalid_config")
    return result


def _optional_text(value: Any, field: str, max_length: int = 500) -> str | None:
    if value is None:
        return None
    text = normalized_text(value, field)
    if len(text) > max_length:
        raise S1Error(f"{field} 不能超过 {max_length} 字", "invalid_config")
    return text


def _normal_date(value: Any, field: str) -> str | None:
    text = _optional_text(value, field, 7)
    if text is not None and not PARTIAL_DATE.fullmatch(text):
        raise S1Error(f"{field} 必须是 YYYY、YYYY-MM 或 null", "invalid_date")
    return text


def _normal_enum(value: Any, field: str, allowed: set[str]) -> str:
    text = normalized_text(value, field)
    if text not in allowed:
        raise S1Error(f"{field} 只能是：{sorted(allowed)}", "invalid_config")
    return text


def _file_hash(reference: str) -> str | None:
    path = Path(reference)
    if not path.is_absolute() or not path.exists():
        return None
    if not path.is_file():
        raise S1Error(f"信息来源不是文件：{reference}", "invalid_source_reference")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise S1Error(f"无法读取信息来源：{reference}", "invalid_source_reference") from exc
    return digest.hexdigest()


def _normal_information_sources(value: Any) -> tuple[dict[str, Any], set[str]]:
    value = _check_object(value, "information_sources", {"resume_status", "items"})
    resume_status = _normal_enum(value.get("resume_status"), "information_sources.resume_status", {"provided", "declined"})
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > 30:
        raise S1Error("information_sources.items 必须是最多 30 项的数组", "invalid_sources")
    items = []
    source_ids: set[str] = set()
    resume_count = 0
    for index, raw in enumerate(raw_items):
        raw = _check_object(
            raw,
            f"information_sources.items[{index}]",
            {"source_id", "source_type", "reference", "content_hash"},
            {"source_id", "source_type", "reference"},
        )
        source_id = normalized_text(raw.get("source_id"), "source_id")
        if not SOURCE_ID.fullmatch(source_id) or source_id in source_ids:
            raise S1Error(f"source_id 无效或重复：{source_id}", "invalid_source_id")
        source_type = _normal_enum(raw.get("source_type"), "source_type", SOURCE_TYPES)
        reference = normalized_text(raw.get("reference"), "source.reference")
        if len(reference) > 1000:
            raise S1Error("source.reference 不能超过 1000 字", "invalid_source_reference")
        supplied_hash = raw.get("content_hash")
        if supplied_hash is not None and (not isinstance(supplied_hash, str) or not CONTENT_HASH.fullmatch(supplied_hash)):
            raise S1Error("source.content_hash 必须是 64 位小写 SHA-256 或 null", "invalid_source_hash")
        if source_type == "user_statement":
            if supplied_hash is not None:
                raise S1Error("用户陈述不保存内容哈希", "invalid_source_hash")
            content_hash = None
        else:
            computed_hash = _file_hash(reference)
            if computed_hash is not None and supplied_hash is not None and supplied_hash != computed_hash:
                raise S1Error(f"信息来源内容已变化：{reference}", "source_hash_mismatch")
            content_hash = computed_hash or supplied_hash
        if source_type == "resume":
            resume_count += 1
        source_ids.add(source_id)
        items.append({
            "source_id": source_id,
            "source_type": source_type,
            "reference": reference,
            "content_hash": content_hash,
        })
    if resume_status == "provided" and resume_count < 1:
        raise S1Error("已提供简历时必须有 resume 来源", "invalid_resume_status")
    if resume_status == "declined" and resume_count:
        raise S1Error("用户拒绝提供简历时不能保留 resume 来源", "invalid_resume_status")
    return {"resume_status": resume_status, "items": items}, source_ids


def _normal_source_ids(value: Any, field: str, source_ids: set[str]) -> list[str]:
    result = _string_list(value, field, required=True, maximum=10, max_length=100)
    unknown = [item for item in result if item not in source_ids]
    if unknown:
        raise S1Error(f"{field} 引用了不存在的信息来源：{unknown}", "invalid_source_reference")
    return result


def _normal_education(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    fields = {
        "institution", "institution_attributes", "degree_level", "major",
        "start_date", "end_date", "is_current", "academic_highlights", "source_ids",
    }
    value = _check_object(value, field, fields)
    is_current = value.get("is_current")
    if not isinstance(is_current, bool):
        raise S1Error(f"{field}.is_current 必须是布尔值", "invalid_config")
    end_date = _normal_date(value.get("end_date"), f"{field}.end_date")
    if is_current and end_date is not None:
        raise S1Error(f"{field} 仍在读时 end_date 必须为 null", "invalid_date")
    return {
        "institution": normalized_text(value.get("institution"), f"{field}.institution"),
        "institution_attributes": _string_list(value.get("institution_attributes"), f"{field}.institution_attributes", maximum=12, max_length=100),
        "degree_level": normalized_text(value.get("degree_level"), f"{field}.degree_level"),
        "major": normalized_text(value.get("major"), f"{field}.major"),
        "start_date": _normal_date(value.get("start_date"), f"{field}.start_date"),
        "end_date": end_date,
        "is_current": is_current,
        "academic_highlights": _string_list(value.get("academic_highlights"), f"{field}.academic_highlights", maximum=20),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_experience(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    fields = {
        "experience_type", "organization", "name", "role", "start_date", "end_date",
        "is_current", "domains", "responsibilities", "achievements", "source_ids",
    }
    value = _check_object(value, field, fields)
    is_current = value.get("is_current")
    if not isinstance(is_current, bool):
        raise S1Error(f"{field}.is_current 必须是布尔值", "invalid_config")
    end_date = _normal_date(value.get("end_date"), f"{field}.end_date")
    if is_current and end_date is not None:
        raise S1Error(f"{field} 仍在进行时 end_date 必须为 null", "invalid_date")
    responsibilities = _string_list(value.get("responsibilities"), f"{field}.responsibilities", maximum=30)
    achievements = _string_list(value.get("achievements"), f"{field}.achievements", maximum=30)
    if not responsibilities and not achievements:
        raise S1Error(f"{field} 至少需要一项职责或成果", "invalid_candidate_profile")
    return {
        "experience_type": _normal_enum(value.get("experience_type"), f"{field}.experience_type", EXPERIENCE_TYPES),
        "organization": _optional_text(value.get("organization"), f"{field}.organization"),
        "name": normalized_text(value.get("name"), f"{field}.name"),
        "role": _optional_text(value.get("role"), f"{field}.role"),
        "start_date": _normal_date(value.get("start_date"), f"{field}.start_date"),
        "end_date": end_date,
        "is_current": is_current,
        "domains": _string_list(value.get("domains"), f"{field}.domains", maximum=20, max_length=100),
        "responsibilities": responsibilities,
        "achievements": achievements,
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_capability(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"category", "name", "evidence", "source_ids"})
    return {
        "category": _normal_enum(value.get("category"), f"{field}.category", CAPABILITY_CATEGORIES),
        "name": normalized_text(value.get("name"), f"{field}.name"),
        "evidence": _string_list(value.get("evidence"), f"{field}.evidence", maximum=20),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_credential(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"credential_type", "name", "issuer", "issue_date", "details", "source_ids"})
    return {
        "credential_type": _normal_enum(value.get("credential_type"), f"{field}.credential_type", CREDENTIAL_TYPES),
        "name": normalized_text(value.get("name"), f"{field}.name"),
        "issuer": _optional_text(value.get("issuer"), f"{field}.issuer"),
        "issue_date": _normal_date(value.get("issue_date"), f"{field}.issue_date"),
        "details": _optional_text(value.get("details"), f"{field}.details"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_strength(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"statement", "evidence", "source_ids"})
    return {
        "statement": normalized_text(value.get("statement"), f"{field}.statement"),
        "evidence": _string_list(value.get("evidence"), f"{field}.evidence", maximum=20),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_eligibility(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"fact_type", "value", "source_ids"})
    return {
        "fact_type": normalized_text(value.get("fact_type"), f"{field}.fact_type"),
        "value": normalized_text(value.get("value"), f"{field}.value"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_object_list(value: Any, field: str, normalizer: Any, source_ids: set[str], maximum: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise S1Error(f"{field} 必须是最多 {maximum} 项的数组", "invalid_config")
    return [normalizer(item, f"{field}[{index}]", source_ids) for index, item in enumerate(value)]


def _normal_candidate_profile(value: Any, source_ids: set[str]) -> dict[str, Any]:
    fields = {"basis", "education", "experiences", "capabilities", "credentials", "career_strengths", "eligibility_facts"}
    value = _check_object(value, "candidate_profile", fields)
    basis = _normal_enum(value.get("basis"), "candidate_profile.basis", {"personal", "generic"})
    result = {
        "basis": basis,
        "education": _normal_object_list(value.get("education"), "candidate_profile.education", _normal_education, source_ids, 20),
        "experiences": _normal_object_list(value.get("experiences"), "candidate_profile.experiences", _normal_experience, source_ids, 60),
        "capabilities": _normal_object_list(value.get("capabilities"), "candidate_profile.capabilities", _normal_capability, source_ids, 80),
        "credentials": _normal_object_list(value.get("credentials"), "candidate_profile.credentials", _normal_credential, source_ids, 40),
        "career_strengths": _normal_object_list(value.get("career_strengths"), "candidate_profile.career_strengths", _normal_strength, source_ids, 30),
        "eligibility_facts": _normal_object_list(value.get("eligibility_facts"), "candidate_profile.eligibility_facts", _normal_eligibility, source_ids, 20),
    }
    fact_count = sum(len(result[field]) for field in fields - {"basis"})
    if basis == "personal" and fact_count == 0:
        raise S1Error("个人画像至少需要一条结构化事实", "invalid_candidate_profile")
    if basis == "generic" and fact_count:
        raise S1Error("通用筛选不能包含个人画像事实", "invalid_candidate_profile")
    return result


def _normal_direction(value: Any, field: str, source_ids: set[str], order: int) -> dict[str, Any]:
    value = _check_object(value, field, {"name", "description", "positive_signals", "order", "source_ids"}, {"name", "description", "positive_signals", "source_ids"})
    if "order" in value and value["order"] != order:
        raise S1Error(f"{field}.order 无法复现", "config_normalization_mismatch")
    return {
        "name": normalized_text(value.get("name"), f"{field}.name"),
        "description": normalized_text(value.get("description"), f"{field}.description"),
        "positive_signals": _string_list(value.get("positive_signals"), f"{field}.positive_signals", required=True, maximum=30),
        "order": order,
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_desired_feature(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"scope", "feature", "priority", "source_ids"})
    return {
        "scope": _normal_enum(value.get("scope"), f"{field}.scope", WORK_FEATURE_SCOPES),
        "feature": normalized_text(value.get("feature"), f"{field}.feature"),
        "priority": _normal_enum(value.get("priority"), f"{field}.priority", {"required", "preferred"}),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_hard_exclusion(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"scope", "rule", "source_ids"})
    return {
        "scope": _normal_enum(value.get("scope"), f"{field}.scope", EXCLUSION_SCOPES),
        "rule": normalized_text(value.get("rule"), f"{field}.rule"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_soft_preference(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"scope", "preference", "source_ids"})
    return {
        "scope": _normal_enum(value.get("scope"), f"{field}.scope", PREFERENCE_SCOPES),
        "preference": normalized_text(value.get("preference"), f"{field}.preference"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_job_target(value: Any, source_ids: set[str]) -> tuple[dict[str, Any], set[str]]:
    fields = {"target_directions", "search_keywords", "desired_work_features", "hard_exclusions", "soft_preferences"}
    value = _check_object(value, "job_target", fields)
    raw_directions = value.get("target_directions")
    if not isinstance(raw_directions, list) or not raw_directions or len(raw_directions) > 30:
        raise S1Error("job_target.target_directions 必须是 1 至 30 项数组", "invalid_direction")
    directions = [_normal_direction(item, f"job_target.target_directions[{index}]", source_ids, index) for index, item in enumerate(raw_directions)]
    direction_names = [item["name"] for item in directions]
    if len(set(direction_names)) != len(direction_names):
        raise S1Error("岗位方向名称不能重复", "duplicate_direction")

    raw_keywords = value.get("search_keywords")
    if not isinstance(raw_keywords, list) or not raw_keywords or len(raw_keywords) > 100:
        raise S1Error("job_target.search_keywords 必须是 1 至 100 项数组", "invalid_keywords")
    keywords = []
    seen_terms: set[str] = set()
    for index, raw in enumerate(raw_keywords):
        raw = _check_object(raw, f"search_keywords[{index}]", {"term", "directions", "order"}, {"term", "directions"})
        term = normalized_text(raw.get("term"), "search_keywords[].term")
        if len(term) > 100 or term in seen_terms:
            raise S1Error(f"关键词过长或重复：{term}", "duplicate_keyword")
        keyword_directions = _string_list(raw.get("directions"), "search_keywords[].directions", required=True, maximum=30, max_length=100)
        if any(direction not in direction_names for direction in keyword_directions):
            raise S1Error(f"关键词 {term} 引用了未确认方向", "invalid_keyword_direction")
        if "order" in raw and raw["order"] != index:
            raise S1Error("关键词顺序无法复现", "config_normalization_mismatch")
        seen_terms.add(term)
        keywords.append({"term": term, "directions": keyword_directions, "order": index})

    desired = _normal_object_list(value.get("desired_work_features"), "job_target.desired_work_features", _normal_desired_feature, source_ids, 40)
    if not desired:
        raise S1Error("至少需要一项目标工作特征", "invalid_job_target")
    result = {
        "target_directions": directions,
        "search_keywords": keywords,
        "desired_work_features": desired,
        "hard_exclusions": _normal_object_list(value.get("hard_exclusions"), "job_target.hard_exclusions", _normal_hard_exclusion, source_ids, 40),
        "soft_preferences": _normal_object_list(value.get("soft_preferences"), "job_target.soft_preferences", _normal_soft_preference, source_ids, 40),
    }
    return result, set(direction_names)


def _normal_company_preferred(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"category", "feature", "source_ids"})
    return {
        "category": _normal_enum(value.get("category"), f"{field}.category", COMPANY_CATEGORIES),
        "feature": normalized_text(value.get("feature"), f"{field}.feature"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_company_disqualifier(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"category", "condition", "source_ids"})
    return {
        "category": _normal_enum(value.get("category"), f"{field}.category", COMPANY_CATEGORIES),
        "condition": normalized_text(value.get("condition"), f"{field}.condition"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_risk_concern(value: Any, field: str, source_ids: set[str]) -> dict[str, Any]:
    value = _check_object(value, field, {"category", "concern", "source_ids"})
    return {
        "category": _normal_enum(value.get("category"), f"{field}.category", RISK_CATEGORIES),
        "concern": normalized_text(value.get("concern"), f"{field}.concern"),
        "source_ids": _normal_source_ids(value.get("source_ids"), f"{field}.source_ids", source_ids),
    }


def _normal_company_preferences(value: Any, source_ids: set[str]) -> dict[str, Any]:
    fields = {"preferred_features", "disqualifying_conditions", "risk_concerns"}
    value = _check_object(value, "company_preferences", fields)
    return {
        "preferred_features": _normal_object_list(value.get("preferred_features"), "company_preferences.preferred_features", _normal_company_preferred, source_ids, 30),
        "disqualifying_conditions": _normal_object_list(value.get("disqualifying_conditions"), "company_preferences.disqualifying_conditions", _normal_company_disqualifier, source_ids, 30),
        "risk_concerns": _normal_object_list(value.get("risk_concerns"), "company_preferences.risk_concerns", _normal_risk_concern, source_ids, 30),
    }


def _normal_search_scope(value: Any) -> dict[str, Any]:
    fields = {"search_mode", "per_city_target_count", "search_urls"}
    value = _check_object(value, "search_scope", fields)
    mode = _normal_enum(value.get("search_mode"), "search_scope.search_mode", {"exhaustive", "per_city_target"})
    target = value.get("per_city_target_count")
    if mode == "exhaustive":
        if target is not None:
            raise S1Error("exhaustive 模式的目标数量必须为 null", "invalid_target_count")
    else:
        target = _plain_int(target, "search_scope.per_city_target_count", 1)

    raw_urls = value.get("search_urls")
    if not isinstance(raw_urls, list) or not raw_urls or len(raw_urls) > 20:
        raise S1Error("search_scope.search_urls 必须是 1 至 20 项数组", "invalid_search_urls")
    urls = []
    labels: dict[str, str] = {}
    cities: dict[str, str] = {}
    bases = set()
    for position, item in enumerate(raw_urls):
        item = _check_object(item, f"search_urls[{position}]", {"url", "city_label", "city", "order", "search_base"}, {"url", "city_label"})
        normal = inspect_search_url(item.get("url"), item.get("city_label"), position)
        for derived in ("city", "order", "search_base"):
            if derived in item and item[derived] != normal[derived]:
                raise S1Error(f"search_urls[{position}].{derived} 无法复现", "config_normalization_mismatch")
        label, city = normal["city_label"], normal["city"]
        if label in labels and labels[label] != city:
            raise S1Error("同一城市名称对应多个 BOSS city 参数", "ambiguous_city_mapping")
        if city in cities and cities[city] != label:
            raise S1Error("同一 BOSS city 参数对应多个城市名称", "ambiguous_city_mapping")
        if normal["search_base"] in bases:
            raise S1Error("存在重复城市筛选 URL", "duplicate_search_url")
        labels[label], cities[city] = city, label
        bases.add(normal["search_base"])
        urls.append(normal)
    return {"search_mode": mode, "per_city_target_count": target, "search_urls": urls}


def normalize_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise S1Error("配置必须是 JSON 对象", "invalid_config")
    unexpected = set(value) - TOP_LEVEL_FIELDS
    required = TOP_LEVEL_FIELDS - {"config_hash"}
    missing = required - set(value)
    if unexpected or missing:
        raise S1Error(f"配置字段不正确；多余={sorted(unexpected)}，缺少={sorted(missing)}", "invalid_config")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("config schema_version 无效", "invalid_schema")

    information_sources, source_ids = _normal_information_sources(value.get("information_sources"))
    candidate_profile = _normal_candidate_profile(value.get("candidate_profile"), source_ids)
    job_target, _ = _normal_job_target(value.get("job_target"), source_ids)
    company_preferences = _normal_company_preferences(value.get("company_preferences"), source_ids)
    search_scope = _normal_search_scope(value.get("search_scope"))

    result = {
        "schema_version": SCHEMA_VERSION,
        "information_sources": information_sources,
        "candidate_profile": candidate_profile,
        "job_target": job_target,
        "company_preferences": company_preferences,
        "search_scope": search_scope,
    }
    computed = _canonical_hash(result)
    supplied = value.get("config_hash")
    if supplied is not None and supplied != computed:
        raise S1Error("config_hash 与规范化配置不一致", "config_hash_mismatch")
    return {**result, "config_hash": computed}


def validate_config(value: Any) -> dict[str, Any]:
    normal = normalize_config(value)
    if normal != value:
        raise S1Error("config.json 不是规范化格式", "config_normalization_mismatch")
    return normal


def generate_search_plan(value: Any) -> list[dict[str, Any]]:
    """从规范化 S0 配置生成稳定的“城市 URL × 关键词”S1 组合。"""
    config = validate_config(value)
    plan = []
    seen_keys: set[str] = set()
    for search_entry in config["search_scope"]["search_urls"]:
        for keyword in config["job_target"]["search_keywords"]:
            search_url = build_search_url(search_entry["search_base"], keyword["term"])
            key = combination_key(search_url, keyword["term"])
            if key in seen_keys:
                raise S1Error("S1 生成了重复组合键", "duplicate_combination")
            seen_keys.add(key)
            plan.append({
                "combo_key": key,
                "search_url_order": search_entry["order"],
                "keyword_order": keyword["order"],
                "city_label": search_entry["city_label"],
                "city": search_entry["city"],
                "term": keyword["term"],
                "directions": copy.deepcopy(keyword["directions"]),
                "search_url": search_url,
            })
    return plan


def prepare(run_root: str, payload: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
    if task_id is not None:
        validate_task(run_root, task_id)
    normal = normalize_config(copy.deepcopy(payload))
    path = Path(run_root) / "job-research-data" / "config.json"
    if path.exists():
        existing = validate_config(load_json(path))
        if existing != normal:
            raise S1Error("当前任务已经存在不同配置，拒绝覆盖或混合", "config_conflict")
    if task_id is not None:
        bind_config(run_root, task_id, normal["config_hash"])
    if not path.exists():
        atomic_write_json(path, normal)
    return {
        "ok": True,
        "config_hash": normal["config_hash"],
        "city_count": len(normal["search_scope"]["search_urls"]),
        "keyword_count": len(normal["job_target"]["search_keywords"]),
        "combination_count": len(normal["search_scope"]["search_urls"]) * len(normal["job_target"]["search_keywords"]),
    }


def validate_run(run_root: str, task_id: str | None = None) -> dict[str, Any]:
    task = validate_task(run_root, task_id) if task_id is not None else None
    path = Path(run_root) / "job-research-data" / "config.json"
    config = validate_config(load_json(path))
    if task is not None:
        if task["config_hash"] != config["config_hash"]:
            raise S1Error("task.json 与 config.json 的配置哈希不一致", "task_config_mismatch")
    return {
        "ok": True,
        "config_hash": config["config_hash"],
        "city_count": len(config["search_scope"]["search_urls"]),
        "keyword_count": len(config["job_target"]["search_keywords"]),
        "combination_count": len(config["search_scope"]["search_urls"]) * len(config["job_target"]["search_keywords"]),
    }


def _read_input(path: str) -> dict[str, Any]:
    value = strict_json_loads(sys.stdin.read()) if path == "-" else load_json(path)
    if not isinstance(value, dict):
        raise S1Error("输入必须是 JSON 对象", "invalid_input")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect-url")
    inspect_parser.add_argument("--url", required=True)
    inspect_parser.add_argument("--city-label", required=True)
    inspect_parser.add_argument("--order", type=int, default=0)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--run-root", required=True)
    prepare_parser.add_argument("--task-id", required=True)
    prepare_parser.add_argument("--input", default="-")
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--run-root", required=True)
    validate_parser.add_argument("--task-id", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--run-root", required=True)
    plan_parser.add_argument("--task-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "inspect-url":
        result = {"ok": True, **inspect_search_url(args.url, args.city_label, args.order)}
    elif args.command == "prepare":
        result = prepare(args.run_root, _read_input(args.input), args.task_id)
    elif args.command == "validate":
        result = validate_run(args.run_root, args.task_id)
    else:
        validate_run(args.run_root, args.task_id)
        config = validate_config(load_json(Path(args.run_root) / "job-research-data" / "config.json"))
        result = {
            "ok": True,
            "config_hash": config["config_hash"],
            "combinations": generate_search_plan(config),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except S1Error as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
