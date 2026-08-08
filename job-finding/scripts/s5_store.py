#!/usr/bin/env python3
"""校验模型评分档位，并确定性写入 job-scores.json。"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from s1_common import S1Error, atomic_write_json, load_json, normalized_text, sha256_text, strict_json_loads
from task_config import validate_config


SCHEMA_VERSION = 3
SCORING_RULE_VERSION = "s5-v2"
GROUP_COEFFICIENTS = {"job_match": Decimal("0.70"), "company_evaluation": Decimal("0.30")}
GROUPS = ("job_match", "company_evaluation")
DIMENSION_STATUS = {"可评分", "不可评分"}
MODEL_FIELDS = {"job_key", "job_match", "company_evaluation"}
MODEL_DIMENSION_FIELDS = {
    "dimension_id", "status", "matched_anchor", "criterion_ids", "evidence_ids", "reason",
}

DIMENSIONS: dict[str, tuple[dict[str, Any], ...]] = {
    "job_match": (
        {
            "id": "target_and_responsibility_fit",
            "name": "目标方向与核心职责匹配",
            "weight": Decimal("4.0"),
            "anchor_set": "fit",
            "evidence_groups": ("job_detail",),
            "base_rule": "判断岗位核心职责与用户目标方向及期望工作特征的匹配程度。",
        },
        {
            "id": "strength_utilization",
            "name": "用户能力与优势发挥",
            "weight": Decimal("3.0"),
            "anchor_set": "fit",
            "evidence_groups": ("job_detail",),
            "base_rule": "判断岗位能否实际使用用户已经确认的知识、能力、经验和职业优势。",
        },
        {
            "id": "work_style_fit",
            "name": "工作方式与偏好匹配",
            "weight": Decimal("2.0"),
            "anchor_set": "fit",
            "evidence_groups": ("job_detail",),
            "base_rule": "判断工作性质、协作方式、工作强度与用户软偏好的匹配程度。",
        },
        {
            "id": "role_growth_fit",
            "name": "岗位成长与职业延续性",
            "weight": Decimal("1.0"),
            "anchor_set": "fit",
            "evidence_groups": ("job_detail",),
            "base_rule": "判断岗位本身是否有助于积累目标能力并延续用户期望方向。",
        },
    ),
    "company_evaluation": (
        {
            "id": "business_and_industry_fit",
            "name": "主营业务与行业匹配",
            "weight": Decimal("2.5"),
            "anchor_set": "fit",
            "evidence_groups": ("basic_profile",),
            "base_rule": "判断公司主营业务、行业和业务方向与用户目标及公司偏好的匹配程度。",
        },
        {
            "id": "scale_and_stability",
            "name": "经营规模与稳定性",
            "weight": Decimal("2.0"),
            "anchor_set": "fit",
            "evidence_groups": ("basic_profile",),
            "base_rule": "根据成立时间、明确披露的员工人数或参保人数、营收、注册资本和实缴资本判断经营基础；未披露字段不按零分处理。",
        },
        {
            "id": "employee_experience_and_culture",
            "name": "员工体验与企业文化",
            "weight": Decimal("3.0"),
            "anchor_set": "fit",
            "evidence_groups": ("employee_reviews",),
            "base_rule": "判断公开员工体验反映的工作强度、管理、薪酬、稳定性和文化。",
        },
        {
            "id": "company_growth_platform",
            "name": "成长平台与培养条件",
            "weight": Decimal("1.5"),
            "anchor_set": "fit",
            "evidence_groups": ("basic_profile", "employee_reviews"),
            "base_rule": "判断公司是否提供用户关注的培训、成长平台和发展环境。",
        },
        {
            "id": "public_risk",
            "name": "公开风险",
            "weight": Decimal("1.0"),
            "anchor_set": "risk",
            "evidence_groups": ("public_risks",),
            "base_rule": "判断已保存公开风险事项对求职决策的影响程度。",
        },
    ),
}

ANCHORS: dict[str, dict[str, Decimal]] = {
    "fit": {
        "strong_match": Decimal("1.00"),
        "match": Decimal("0.80"),
        "partial_match": Decimal("0.50"),
        "weak_match": Decimal("0.20"),
        "mismatch": Decimal("0.00"),
    },
    "risk": {
        "no_material_concern": Decimal("1.00"),
        "minor_concern": Decimal("0.75"),
        "moderate_concern": Decimal("0.40"),
        "major_concern": Decimal("0.00"),
    },
}

ANCHOR_DESCRIPTIONS = {
    "strong_match": "有直接、充分证据表明强匹配",
    "match": "主要条件匹配，未见明显不利证据",
    "partial_match": "同时存在匹配与不足，或只覆盖部分条件",
    "weak_match": "只有较弱、次要或间接匹配",
    "mismatch": "有直接证据表明不匹配",
    "no_material_concern": "规定查询已完成，未发现实质性风险事项",
    "minor_concern": "存在轻微或对求职影响有限的风险信号",
    "moderate_concern": "存在需要用户重点权衡的风险事项",
    "major_concern": "存在可能显著影响求职决策的重大风险事项",
}

RATINGS = (
    (Decimal("8.5"), "强匹配"),
    (Decimal("7.0"), "匹配"),
    (Decimal("5.5"), "可考虑"),
    (Decimal("4.0"), "弱匹配"),
    (Decimal("0.0"), "不推荐"),
)


def _canonical_hash(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))


def _scoring_config() -> dict[str, Any]:
    return {
        "rule_version": SCORING_RULE_VERSION,
        "group_coefficients": {key: float(value) for key, value in GROUP_COEFFICIENTS.items()},
        "dimensions": {
            group: [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "weight": float(item["weight"]),
                    "anchor_set": item["anchor_set"],
                    "evidence_groups": list(item["evidence_groups"]),
                }
                for item in definitions
            ]
            for group, definitions in DIMENSIONS.items()
        },
        "anchors": {
            group: {key: float(value) for key, value in values.items()}
            for group, values in ANCHORS.items()
        },
        "ratings": [{"minimum": float(value), "label": label} for value, label in RATINGS],
    }


def _validate_fixed_scoring_config() -> None:
    if sum(GROUP_COEFFICIENTS.values(), Decimal("0")) != Decimal("1"):
        raise RuntimeError("S5 评分组系数必须合计为 1")
    for group, definitions in DIMENSIONS.items():
        if sum((item["weight"] for item in definitions), Decimal("0")) != Decimal("10"):
            raise RuntimeError(f"S5 {group} 维度权重必须合计为 10")
        identifiers = [item["id"] for item in definitions]
        if len(identifiers) != len(set(identifiers)):
            raise RuntimeError(f"S5 {group} 维度 ID 不能重复")
    for anchor_set, anchors in ANCHORS.items():
        if not anchors or any(value < 0 or value > 1 for value in anchors.values()):
            raise RuntimeError(f"S5 {anchor_set} 锚点系数必须位于 0 到 1")


_validate_fixed_scoring_config()
SCORING_CONFIG_HASH = _canonical_hash(_scoring_config())


def _round(value: Decimal, places: str) -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _normal_list(value: Any, field: str, *, maximum: int, require_nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum or (require_nonempty and not value):
        qualifier = "非空" if require_nonempty else ""
        raise S1Error(f"{field} 必须是最多 {maximum} 项的{qualifier}数组", "invalid_s5_payload")
    result = [normalized_text(item, f"{field}[]") for item in value]
    if len(set(result)) != len(result):
        raise S1Error(f"{field} 含重复内容", "invalid_s5_payload")
    return result


def _criterion(dimension_id: str, source_path: str, value: Any) -> dict[str, Any]:
    content_hash = _canonical_hash(value)
    criterion_id = sha256_text(f"{SCORING_RULE_VERSION}\n{dimension_id}\n{source_path}\n{content_hash}")
    return {
        "criterion_id": criterion_id,
        "source_path": source_path,
        "value": value,
    }


def _has_any(value: Any, terms: tuple[str, ...]) -> bool:
    text = str(value).casefold()
    return any(term.casefold() in text for term in terms)


def _scoring_criteria(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_dimension: dict[str, list[dict[str, Any]]] = {}
    for definitions in DIMENSIONS.values():
        for definition in definitions:
            by_dimension[definition["id"]] = [
                _criterion(definition["id"], f"s5_rule.{definition['id']}", {"description": definition["base_rule"]})
            ]

    def add(dimension_id: str, source_path: str, value: Any) -> None:
        by_dimension[dimension_id].append(_criterion(dimension_id, source_path, value))

    target = config["job_target"]
    candidate = config["candidate_profile"]
    company = config["company_preferences"]

    for index, item in enumerate(target["target_directions"]):
        path = f"job_target.target_directions[{index}]"
        add("target_and_responsibility_fit", path, item)
        add("role_growth_fit", path, item)
        add("business_and_industry_fit", path, item)

    growth_terms = ("growth", "career", "development", "training", "成长", "发展", "培养", "晋升")
    work_terms = ("work", "style", "culture", "collaboration", "工作", "协作", "加班", "出差", "沟通")
    scale_terms = ("scale", "stability", "finance", "规模", "稳定", "营收", "资本")
    business_terms = ("industry", "business", "product", "行业", "业务", "主营", "产品")
    employee_terms = ("employment", "culture", "management", "compensation", "员工", "文化", "管理", "薪酬")
    risk_terms = ("risk", "legal", "compliance", "风险", "司法", "监管", "处罚", "争议")

    for index, item in enumerate(target["desired_work_features"]):
        path = f"job_target.desired_work_features[{index}]"
        scope = item.get("scope", "")
        if _has_any(scope, growth_terms):
            add("role_growth_fit", path, item)
            add("company_growth_platform", path, item)
        elif _has_any(scope, work_terms):
            add("work_style_fit", path, item)
            add("employee_experience_and_culture", path, item)
        else:
            add("target_and_responsibility_fit", path, item)

    for field in ("education", "experiences", "capabilities", "credentials", "career_strengths"):
        for index, item in enumerate(candidate[field]):
            add("strength_utilization", f"candidate_profile.{field}[{index}]", item)

    for index, item in enumerate(target["soft_preferences"]):
        path = f"job_target.soft_preferences[{index}]"
        scope = item.get("scope", "")
        if _has_any(scope, growth_terms):
            add("role_growth_fit", path, item)
            add("company_growth_platform", path, item)
        else:
            add("work_style_fit", path, item)
            add("employee_experience_and_culture", path, item)

    def route_company(path: str, item: dict[str, Any]) -> None:
        category = item.get("category", "")
        if _has_any(category, growth_terms):
            add("company_growth_platform", path, item)
        elif _has_any(category, scale_terms):
            add("scale_and_stability", path, item)
        elif _has_any(category, employee_terms):
            add("employee_experience_and_culture", path, item)
        elif _has_any(category, risk_terms):
            add("public_risk", path, item)
        elif _has_any(category, business_terms):
            add("business_and_industry_fit", path, item)
        else:
            add("business_and_industry_fit", path, item)

    for field in ("preferred_features", "disqualifying_conditions"):
        for index, item in enumerate(company[field]):
            route_company(f"company_preferences.{field}[{index}]", item)
    for index, item in enumerate(company["risk_concerns"]):
        add("public_risk", f"company_preferences.risk_concerns[{index}]", item)

    return by_dimension


def _load_inputs(run_root: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    data_dir = Path(run_root) / "job-research-data"
    config = validate_config(load_json(data_dir / "config.json"))
    job_details = load_json(data_dir / "job-details.json")
    screening = load_json(data_dir / "screening-results.json")
    research = load_json(data_dir / "company-research.json")

    if research.get("config_hash") != config["config_hash"]:
        raise S1Error("S4 与 S0 配置哈希不一致", "stale_s5_config")
    if research.get("input_job_details_sha256") != _canonical_hash(job_details):
        raise S1Error("S4 引用的 job-details.json 已变化", "stale_s5_details")
    if research.get("input_screening_results_sha256") != _canonical_hash(screening):
        raise S1Error("S4 引用的 screening-results.json 已变化", "stale_s5_screening")

    detail_records = job_details.get("records")
    screening_records = screening.get("records")
    company_records = research.get("records")
    if not isinstance(detail_records, list) or not isinstance(screening_records, list) or not isinstance(company_records, list):
        raise S1Error("S5 输入 records 必须是数组", "invalid_s5_input")
    details = {
        item["job_key"]: item for item in detail_records
        if isinstance(item, dict) and item.get("record_type") == "job_detail" and isinstance(item.get("job_key"), str)
    }
    screenings = {
        item["job_key"]: item for item in screening_records
        if isinstance(item, dict) and isinstance(item.get("job_key"), str)
    }
    tasks: list[dict[str, Any]] = []
    seen_jobs: set[str] = set()
    for company_record in company_records:
        if not isinstance(company_record, dict):
            raise S1Error("公司背调记录不是对象", "invalid_s5_input")
        company_key = normalized_text(company_record.get("company_key"), "company_key")
        linked_jobs = company_record.get("linked_job_keys")
        evidence = company_record.get("evidence")
        if not isinstance(linked_jobs, list) or not isinstance(evidence, list):
            raise S1Error("公司背调缺少 linked_job_keys 或 evidence 数组", "invalid_s5_input")
        company_evidence: dict[str, dict[str, Any]] = {}
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
                raise S1Error("公司背调证据结构无效", "invalid_s5_input")
            evidence_id = normalized_text(item["evidence_id"], "evidence_id")
            if evidence_id in company_evidence:
                raise S1Error("公司背调证据 ID 重复", "invalid_s5_input")
            company_evidence[evidence_id] = item
        for raw_job_key in linked_jobs:
            job_key = normalized_text(raw_job_key, "linked_job_keys[]")
            if job_key in seen_jobs or job_key not in details or job_key not in screenings:
                raise S1Error(f"S5 岗位关系无效或重复：{job_key}", "invalid_s5_input")
            if screenings[job_key].get("status") != "初筛通过":
                raise S1Error(f"公司背调关联了未通过初筛的岗位：{job_key}", "invalid_s5_input")
            seen_jobs.add(job_key)
            detail = details[job_key]
            job_evidence = {
                item["evidence_id"]: item for item in detail.get("evidence", [])
                if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
            }
            tasks.append({
                "job_key": job_key,
                "company_key": company_key,
                "detail": detail,
                "screening": screenings[job_key],
                "company": company_record,
                "job_evidence": job_evidence,
                "company_evidence": company_evidence,
            })
    raw_skipped = research.get("skipped_jobs")
    if not isinstance(raw_skipped, list):
        raise S1Error("S4 缺少 skipped_jobs 数组", "invalid_s5_input")
    skipped_jobs: set[str] = set()
    for item in raw_skipped:
        if not isinstance(item, dict):
            raise S1Error("S4 跳过岗位记录不是对象", "invalid_s5_input")
        job_key = normalized_text(item.get("job_key"), "skipped_jobs[].job_key")
        if job_key in skipped_jobs or job_key in seen_jobs or job_key not in details or job_key not in screenings:
            raise S1Error(f"S4 跳过岗位关系无效或重复：{job_key}", "invalid_s5_input")
        if screenings[job_key].get("status") != "初筛通过":
            raise S1Error(f"S4 只能跳过初筛通过但主体不可用的岗位：{job_key}", "invalid_s5_input")
        skipped_jobs.add(job_key)
    passed_jobs = {
        job_key for job_key, result in screenings.items() if result.get("status") == "初筛通过"
    }
    if seen_jobs | skipped_jobs != passed_jobs:
        raise S1Error("S4 尚未覆盖全部初筛通过岗位，禁止启动 S5", "s4_not_complete")
    return config, job_details, screening, research, tasks


def _load_document(
    run_root: str, config: dict[str, Any], details: dict[str, Any], screening: dict[str, Any], research: dict[str, Any],
) -> dict[str, Any]:
    path = Path(run_root) / "job-research-data" / "job-scores.json"
    expected = {
        "schema_version": SCHEMA_VERSION,
        "config_hash": config["config_hash"],
        "input_job_details_sha256": _canonical_hash(details),
        "input_screening_results_sha256": _canonical_hash(screening),
        "input_company_research_sha256": _canonical_hash(research),
        "scoring_rule_version": SCORING_RULE_VERSION,
        "scoring_config_hash": SCORING_CONFIG_HASH,
        "records": [],
    }
    if not path.exists():
        return expected
    document = load_json(path)
    for field in expected.keys() - {"records"}:
        if document.get(field) != expected[field]:
            raise S1Error(f"job-scores.json 的 {field} 已失效", "stale_s5_input")
    return document


def _allowed_criteria(criteria: dict[str, list[dict[str, Any]]], dimension_id: str) -> set[str]:
    return {item["criterion_id"] for item in criteria[dimension_id]}


def _allowed_evidence(task: dict[str, Any], definition: dict[str, Any]) -> set[str]:
    if definition["evidence_groups"] == ("job_detail",):
        return set(task["job_evidence"])
    groups = set(definition["evidence_groups"])
    return {
        evidence_id for evidence_id, item in task["company_evidence"].items()
        if item.get("group") in groups
    }


def _normal_dimension(
    raw: Any, definition: dict[str, Any], task: dict[str, Any], criteria: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != MODEL_DIMENSION_FIELDS:
        raise S1Error("评分维度字段不准确", "invalid_s5_payload")
    dimension_id = normalized_text(raw.get("dimension_id"), "dimension_id")
    if dimension_id != definition["id"]:
        raise S1Error("评分维度缺失、增加或顺序错误", "invalid_s5_dimension")
    status = normalized_text(raw.get("status"), "status")
    if status not in DIMENSION_STATUS:
        raise S1Error(f"评分维度状态无效：{status}", "invalid_s5_status")
    criterion_ids = _normal_list(raw.get("criterion_ids"), "criterion_ids", maximum=30, require_nonempty=True)
    if any(value not in _allowed_criteria(criteria, dimension_id) for value in criterion_ids):
        raise S1Error("评分引用了当前维度之外的用户条件", "invalid_s5_criterion")
    evidence_ids = _normal_list(raw.get("evidence_ids"), "evidence_ids", maximum=40)
    if any(value not in _allowed_evidence(task, definition) for value in evidence_ids):
        raise S1Error("评分引用了当前岗位、公司或维度之外的证据", "invalid_s5_evidence")
    reason = normalized_text(raw.get("reason"), "reason")
    if len(reason) > 500:
        raise S1Error("评分理由不能超过 500 字", "invalid_s5_payload")
    anchor = raw.get("matched_anchor")
    if status == "可评分":
        anchor = normalized_text(anchor, "matched_anchor")
        if anchor not in ANCHORS[definition["anchor_set"]]:
            raise S1Error("评分锚点不属于当前维度", "invalid_s5_anchor")
        if not evidence_ids:
            raise S1Error("可评分维度必须引用至少一条证据", "missing_s5_evidence")
        if dimension_id == "public_risk" and anchor == "no_material_concern":
            risk_group = task["company"].get("public_risks")
            if (
                not isinstance(risk_group, dict)
                or risk_group.get("query_status") != "已完成"
                or not isinstance(risk_group.get("items"), list)
                or risk_group["items"]
            ):
                raise S1Error(
                    "只有公开风险查询已完成且没有风险事项时，才能选择 no_material_concern",
                    "invalid_s5_anchor",
                )
    elif anchor is not None:
        raise S1Error("不可评分维度不能选择评分锚点", "invalid_s5_anchor")
    return {
        "dimension_id": dimension_id,
        "status": status,
        "matched_anchor": anchor,
        "criterion_ids": criterion_ids,
        "evidence_ids": evidence_ids,
        "reason": reason,
    }


def _normal_payload(payload: dict[str, Any], task: dict[str, Any], criteria: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    if set(payload) != MODEL_FIELDS:
        raise S1Error("模型评分只能提交 job_key、job_match 和 company_evaluation", "invalid_s5_payload")
    job_key = normalized_text(payload.get("job_key"), "job_key")
    if job_key != task["job_key"]:
        raise S1Error("模型评分岗位身份不一致", "job_identity_conflict")
    result: dict[str, Any] = {"job_key": job_key}
    for group in GROUPS:
        values = payload.get(group)
        definitions = DIMENSIONS[group]
        if not isinstance(values, list) or len(values) != len(definitions):
            raise S1Error(f"{group} 必须完整提交全部评分维度", "invalid_s5_dimension")
        result[group] = [
            _normal_dimension(raw, definition, task, criteria)
            for raw, definition in zip(values, definitions)
        ]
    return result


def _score_group(group: str, inputs: list[dict[str, Any]]) -> tuple[dict[str, Any], Decimal | None]:
    definitions = DIMENSIONS[group]
    scorable_weight = sum(
        (definition["weight"] for definition, item in zip(definitions, inputs) if item["status"] == "可评分"),
        Decimal("0"),
    )
    coverage = scorable_weight / Decimal("10")
    raw_score: Decimal | None = Decimal("0") if scorable_weight else None
    rows: list[dict[str, Any]] = []
    for definition, item in zip(definitions, inputs):
        scorable = item["status"] == "可评分"
        effective_weight = Decimal("10") * definition["weight"] / scorable_weight if scorable else Decimal("0")
        factor = ANCHORS[definition["anchor_set"]][item["matched_anchor"]] if scorable else None
        contribution = effective_weight * factor if factor is not None else None
        if contribution is not None and raw_score is not None:
            raw_score += contribution
        rows.append({
            "dimension_id": definition["id"],
            "name": definition["name"],
            "original_weight": float(definition["weight"]),
            "effective_weight": _round(effective_weight, "0.000001"),
            "status": item["status"],
            "matched_anchor": item["matched_anchor"],
            "anchor_factor": float(factor) if factor is not None else None,
            "dimension_score": _round(factor * Decimal("10"), "0.1") if factor is not None else None,
            "weighted_contribution": _round(contribution, "0.000001") if contribution is not None else None,
            "criterion_ids": item["criterion_ids"],
            "evidence_ids": item["evidence_ids"],
            "reason": item["reason"],
        })
    return ({
        "status": "可评分" if raw_score is not None else "不可评分",
        "score": _round(raw_score, "0.1") if raw_score is not None else None,
        "evidence_coverage": _round(coverage, "0.0001"),
        "scorable_weight": float(scorable_weight),
        "dimensions": rows,
    }, raw_score)


def _rating(score: Decimal) -> str:
    return next(label for minimum, label in RATINGS if score >= minimum)


def _build_record(
    normalized: dict[str, Any], task: dict[str, Any], *, revision: int = 1,
) -> dict[str, Any]:
    job_group, job_raw = _score_group("job_match", normalized["job_match"])
    company_group, company_raw = _score_group("company_evaluation", normalized["company_evaluation"])
    if job_raw is not None and company_raw is not None:
        overall_raw = job_raw * GROUP_COEFFICIENTS["job_match"] + company_raw * GROUP_COEFFICIENTS["company_evaluation"]
        overall = {"status": "可评分", "score": _round(overall_raw, "0.1"), "rating": _rating(overall_raw)}
    else:
        overall = {"status": "不可评分", "score": None, "rating": None}
    overall_coverage = (
        Decimal(str(job_group["evidence_coverage"])) * GROUP_COEFFICIENTS["job_match"]
        + Decimal(str(company_group["evidence_coverage"])) * GROUP_COEFFICIENTS["company_evaluation"]
    )
    limitations = [
        {"group": group, "dimension_id": item["dimension_id"], "reason": item["reason"]}
        for group in GROUPS for item in normalized[group] if item["status"] == "不可评分"
    ]
    return {
        "job_key": task["job_key"],
        "company_key": task["company_key"],
        "status": "已完成",
        "job_match": job_group,
        "company_evaluation": company_group,
        "overall": overall,
        "evidence_coverage": {
            "job": job_group["evidence_coverage"],
            "company": company_group["evidence_coverage"],
            "overall": _round(overall_coverage, "0.0001"),
        },
        "information_limitations": limitations,
        "inputs_used": {
            "detail_record_hash": _canonical_hash(task["detail"]),
            "screening_record_hash": _canonical_hash(task["screening"]),
            "company_research_record_hash": _canonical_hash(task["company"]),
            "scoring_config_hash": SCORING_CONFIG_HASH,
        },
        "revision": revision,
    }


def validate_document(
    document: dict[str, Any], config: dict[str, Any], details: dict[str, Any], screening: dict[str, Any],
    research: dict[str, Any], tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_fields = {
        "schema_version", "config_hash", "input_job_details_sha256", "input_screening_results_sha256",
        "input_company_research_sha256", "scoring_rule_version", "scoring_config_hash", "records",
    }
    if set(document) != expected_fields or document.get("schema_version") != SCHEMA_VERSION:
        raise S1Error("job-scores.json 顶层结构无效", "invalid_s5_document")
    expected_values = {
        "config_hash": config["config_hash"],
        "input_job_details_sha256": _canonical_hash(details),
        "input_screening_results_sha256": _canonical_hash(screening),
        "input_company_research_sha256": _canonical_hash(research),
        "scoring_rule_version": SCORING_RULE_VERSION,
        "scoring_config_hash": SCORING_CONFIG_HASH,
    }
    for field, value in expected_values.items():
        if document.get(field) != value:
            raise S1Error(f"job-scores.json 的 {field} 不一致", "stale_s5_input")
    records = document.get("records")
    if not isinstance(records, list) or len(records) > len(tasks):
        raise S1Error("job-scores records 无效", "invalid_s5_document")
    criteria = _scoring_criteria(config)
    overall_counts = {"可评分": 0, "不可评分": 0}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or index >= len(tasks):
            raise S1Error("评分记录不是对象或顺序无效", "invalid_s5_document")
        task = tasks[index]
        if record.get("job_key") != task["job_key"]:
            raise S1Error("评分记录顺序无效", "job_order_mismatch")
        revision = record.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise S1Error("评分 revision 无效", "invalid_s5_document")
        try:
            payload = {
                "job_key": record["job_key"],
                "job_match": [
                    {field: item[field] for field in MODEL_DIMENSION_FIELDS}
                    for item in record["job_match"]["dimensions"]
                ],
                "company_evaluation": [
                    {field: item[field] for field in MODEL_DIMENSION_FIELDS}
                    for item in record["company_evaluation"]["dimensions"]
                ],
            }
        except (KeyError, TypeError) as exc:
            raise S1Error("持久化评分结构不完整", "invalid_s5_document") from exc
        expected = _build_record(_normal_payload(payload, task, criteria), task, revision=revision)
        if record != expected:
            raise S1Error("持久化评分不能由输入重新计算", "invalid_s5_math")
        overall_counts[record["overall"]["status"]] += 1
    return {
        "ok": True,
        "scored_jobs": len(records),
        "pending_jobs": len(tasks) - len(records),
        "overall_status_counts": overall_counts,
    }


def _model_rule_view() -> dict[str, Any]:
    return {
        "scoring_rule_version": SCORING_RULE_VERSION,
        "dimensions": {
            group: [
                {
                    "dimension_id": item["id"],
                    "name": item["name"],
                    "base_rule": item["base_rule"],
                    "allowed_anchors": list(ANCHORS[item["anchor_set"]]),
                }
                for item in definitions
            ]
            for group, definitions in DIMENSIONS.items()
        },
        "anchor_descriptions": ANCHOR_DESCRIPTIONS,
    }


def _criteria_view(config: dict[str, Any]) -> dict[str, Any]:
    criteria = _scoring_criteria(config)
    return {
        group: [
            {
                "dimension_id": definition["id"],
                "criteria": criteria[definition["id"]],
            }
            for definition in DIMENSIONS[group]
        ]
        for group in GROUPS
    }


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    company = task["company"]
    return {
        "job_key": task["job_key"],
        "company_key": task["company_key"],
        "job": {
            "summary": task["detail"].get("summary"),
            "evidence": task["detail"].get("evidence"),
            "primary_direction": task["screening"].get("reporting", {}).get("primary_direction"),
            "other_directions": task["screening"].get("reporting", {}).get("other_directions"),
        },
        "company": {
            "enterprise_name": company.get("enterprise_name"),
            "brand_company_names": company.get("brand_company_names"),
            "research_status": company.get("status"),
            "basic_profile": company.get("basic_profile"),
            "public_risks": company.get("public_risks"),
            "employee_reviews": company.get("employee_reviews"),
            "evidence": company.get("evidence"),
        },
    }


def status(run_root: str) -> dict[str, Any]:
    config, details, screening, research, tasks = _load_inputs(run_root)
    document = _load_document(run_root, config, details, screening, research)
    result = validate_document(document, config, details, screening, research, tasks)
    result["next_job_key"] = tasks[len(document["records"])]["job_key"] if len(document["records"]) < len(tasks) else None
    return result


def pending(run_root: str, limit: int) -> dict[str, Any]:
    if limit < 1:
        raise S1Error("limit 必须大于零", "invalid_limit")
    config, details, screening, research, tasks = _load_inputs(run_root)
    document = _load_document(run_root, config, details, screening, research)
    result = validate_document(document, config, details, screening, research, tasks)
    offset = len(document["records"])
    return {
        "ok": True,
        "scoring_rule": _model_rule_view(),
        "scoring_context": _criteria_view(config),
        "pending": [_task_view(task) for task in tasks[offset:offset + limit]],
        "remaining": result["pending_jobs"],
    }


def upsert(run_root: str, payload: dict[str, Any]) -> dict[str, Any]:
    config, details, screening, research, tasks = _load_inputs(run_root)
    document = _load_document(run_root, config, details, screening, research)
    validate_document(document, config, details, screening, research, tasks)
    index = len(document["records"])
    if index >= len(tasks):
        raise S1Error("S5 已完成，没有待评分岗位", "s5_complete")
    task = tasks[index]
    criteria = _scoring_criteria(config)
    normalized = _normal_payload(payload, task, criteria)
    document["records"].append(_build_record(normalized, task))
    validate_document(document, config, details, screening, research, tasks)
    atomic_write_json(Path(run_root) / "job-research-data" / "job-scores.json", document)
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
