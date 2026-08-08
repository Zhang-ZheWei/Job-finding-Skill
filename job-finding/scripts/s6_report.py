#!/usr/bin/env python3
"""从已验证的 S0–S5 JSON 确定性生成一份最终岗位决策报告。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

from s1_common import S1Error, atomic_write_json, load_json, normalize_job_url
from s1_store import validate_run_documents
from s2_store import status as s2_status
from s3_store import status as s3_status
from s4_store import status as s4_status
from s5_store import status as s5_status
from task_config import validate_config


SCHEMA_VERSION = 3
REPORT_CONTRACT_VERSION = "s6-v2"
INPUT_FILES = (
    "config.json",
    "checkpoint.json",
    "job-index.json",
    "job-details.json",
    "screening-results.json",
    "company-research.json",
    "job-scores.json",
)
REPORT_RELATIVE_PATH = "result/岗位决策报告.md"
MANIFEST_RELATIVE_PATH = "job-research-data/report-manifest.json"
EXPECTED_SECTIONS = (
    "## 1. 本次任务概况",
    "## 2. 岗位决策总表",
    "## 3. 岗位详细分析",
    "## 4. 公司背调信息",
    "## 5. 未进入评分的岗位",
)
MAIN_HEADER = (
    "| 排名 | 公司 | 岗位 | 薪资及要求 | 岗位匹配分 | 公司评价分 | "
    "综合分/评级 | 一句话总结 | 信息覆盖率 | 岗位链接 |"
)
SUMMARY_CATEGORIES = {
    "industry": "行业",
    "registered_capital": "注册资本",
    "paid_in_capital": "实缴资本",
    "established_at": "成立日期",
    "employee_count": "员工人数/参保人数",
    "operating_revenue": "营业收入",
    "main_business": "主营业务",
    "official_website": "公司官网",
    "products_services": "产品与服务",
    "size_stage": "规模与阶段",
    "ownership_financing": "所有制与融资",
    "location": "所在地",
    "judicial": "司法风险",
    "administrative_penalty": "行政处罚",
    "regulatory_measure": "监管措施",
    "business_abnormality": "经营异常",
    "employment_dispute": "劳动争议",
    "financial": "财务风险",
    "reputation": "声誉风险",
    "work_intensity": "工作强度",
    "management": "管理",
    "compensation": "薪酬福利",
    "career_growth": "职业成长",
    "stability": "稳定性",
    "culture": "企业文化",
    "other": "其他",
}
ADVANTAGE_LABELS = {
    "target_and_responsibility_fit": "岗位职责与目标方向匹配",
    "strength_utilization": "岗位较能发挥用户优势",
    "work_style_fit": "工作方式与用户偏好较匹配",
    "role_growth_fit": "岗位成长与职业延续性较好",
    "business_and_industry_fit": "公司业务与目标方向匹配",
    "scale_and_stability": "公司规模和稳定性突出",
    "employee_experience_and_culture": "员工体验与企业文化评价较好",
    "company_growth_platform": "公司成长平台较有优势",
    "public_risk": "暂未发现明显公开风险",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise S1Error(f"无法读取文件哈希：{path}", "s6_read_failed") from exc


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _collapse(value: Any, *, maximum: int | None = None) -> str:
    result = " ".join(_text(value).replace("\r", "\n").split())
    if maximum is not None and len(result) > maximum:
        return result[: max(maximum - 1, 1)].rstrip() + "…"
    return result


def _escape(value: Any, *, fallback: str = "未披露") -> str:
    if value is None:
        result = ""
    else:
        result = " ".join(unicodedata.normalize("NFC", str(value)).strip().replace("\r", "\n").split())
    if not result:
        return fallback
    result = html.escape(result, quote=False)
    result = result.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "|"):
        result = result.replace(character, "\\" + character)
    result = result.replace("{", "&#123;").replace("}", "&#125;")
    return result


def _safe_https_url(value: Any) -> str:
    url = _text(value)
    if len(url) > 2000:
        raise S1Error("S6 外部链接过长", "invalid_s6_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise S1Error("S6 外部链接无法解析", "invalid_s6_url") from exc
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise S1Error("S6 外部链接必须是无账号信息的 HTTPS URL", "invalid_s6_url")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise S1Error("S6 外部链接域名无效", "invalid_s6_url") from exc
    netloc = host + (f":{port}" if port is not None else "")
    path = quote(parts.path, safe="/%:@-._~!$&'+,;=")
    query = quote(parts.query, safe="%:@/?-._~!$&'+,;=")
    fragment = quote(parts.fragment, safe="%:@/?-._~!$&'+,;=")
    result = urlunsplit(("https", netloc, path, query, fragment))
    if any(character in result for character in ("(", ")", "\\", "\n", "\r", " ")):
        raise S1Error("S6 外部链接含不安全字符", "invalid_s6_url")
    return result


def _external_link(label: Any, url: Any) -> str:
    return f"[{_escape(label, fallback='链接')}]({_safe_https_url(url)})"


def _anchor(kind: str, identity: str) -> str:
    return f"{kind}-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


def _internal_link(label: Any, anchor: str) -> str:
    if not re.fullmatch(r"[a-z]+-[0-9a-f]{16}", anchor):
        raise S1Error("S6 内部锚点无效", "invalid_s6_anchor")
    return f"[{_escape(label)}](#{anchor})"


def _score(value: Any) -> str:
    if value is None:
        return "不可评分"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise S1Error("S6 分数类型无效", "invalid_s6_score")
    return f"{float(value):.1f}"


def _coverage(value: Any) -> str:
    if value is None:
        return "不可评分"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise S1Error("S6 覆盖率类型无效", "invalid_s6_coverage")
    numeric = float(value)
    if not 0 <= numeric <= 1:
        raise S1Error("S6 覆盖率超出 0 到 1", "invalid_s6_coverage")
    return f"{numeric * 100:.1f}%"


def _list_text(values: Any, *, fallback: str = "无", maximum: int | None = None) -> str:
    if not isinstance(values, list):
        return fallback
    result = [_collapse(value, maximum=maximum) for value in values]
    result = [value for value in result if value]
    return "；".join(result) if result else fallback


def _bullet_lines(values: Any, *, fallback: str = "- 无") -> list[str]:
    if not isinstance(values, list) or not values:
        return [fallback]
    return [f"- {_escape(value)}" for value in values]


def _scorable_dimensions(group: dict[str, Any]) -> list[dict[str, Any]]:
    dimensions = group.get("dimensions")
    if not isinstance(dimensions, list):
        return []
    return [item for item in dimensions if isinstance(item, dict) and item.get("status") == "可评分"]


def _reason_fragment(value: Any, *, maximum: int = 46) -> str:
    result = _collapse(value)
    result = re.sub(r"[，,](?:因此|所以|只能|综合来看|故而).*$", "", result)
    if re.search(r"[，,]但", result):
        negative = re.split(r"[，,]但", result, maxsplit=1)[1].strip()
        if len(negative) >= 8:
            result = negative
    result = result.rstrip("。！？；;,.，")
    if len(result) > maximum:
        result = result[: maximum - 1].rstrip("，,；;。 ") + "…"
    return result


def _summary_advantage(dimensions: list[dict[str, Any]]) -> tuple[str, str] | None:
    strong = [
        item for item in dimensions
        if isinstance(item.get("dimension_score"), (int, float)) and float(item["dimension_score"]) >= 8.0
    ]
    if not strong:
        return None
    best = max(
        strong,
        key=lambda item: (
            float(item["dimension_score"]),
            float(item.get("original_weight", 0)),
        ),
    )
    label = ADVANTAGE_LABELS.get(_text(best.get("dimension_id")), _text(best.get("name")) or "存在突出优势")
    detail = _reason_fragment(best.get("reason"), maximum=56)
    if detail in {"当前结构化证据支持该档位", "现有证据支持该档位", "信息不足"}:
        detail = ""
    return label, detail


def _summary_concerns(job_dimensions: list[dict[str, Any]], company_dimensions: list[dict[str, Any]]) -> list[str]:
    def lowest(values: list[dict[str, Any]]) -> dict[str, Any] | None:
        concerned = [
            item for item in values
            if isinstance(item.get("dimension_score"), (int, float)) and float(item["dimension_score"]) <= 5.0
        ]
        if not concerned:
            return None
        return min(
            concerned,
            key=lambda item: (float(item["dimension_score"]), -float(item.get("original_weight", 0))),
        )

    selected = [lowest(job_dimensions), lowest(company_dimensions)]
    if selected[1] is None:
        risk = next(
            (
                item for item in company_dimensions
                if item.get("dimension_id") == "public_risk"
                and item.get("matched_anchor") in {"major_concern", "moderate_concern", "minor_concern"}
            ),
            None,
        )
        selected[1] = risk
    result: list[str] = []
    for item in selected:
        if item is None:
            continue
        reason = _reason_fragment(item.get("reason"))
        if reason in {"当前结构化证据支持该档位", "现有证据支持该档位", "信息不足"}:
            reason = f"{_text(item.get('name')) or '该维度'}存在明显不足"
        if reason:
            result.append(reason)
    return result


def _one_sentence(score: dict[str, Any]) -> str:
    overall = score.get("overall", {})
    rating = _text(overall.get("rating")) or "不可评分"
    job_group = score.get("job_match", {})
    company_group = score.get("company_evaluation", {})
    job_dimensions = _scorable_dimensions(job_group)
    company_dimensions = _scorable_dimensions(company_group)
    advantage = _summary_advantage(job_dimensions + company_dimensions)
    concerns = _summary_concerns(job_dimensions, company_dimensions)
    advantage_text = ""
    if advantage:
        label, detail = advantage
        advantage_text = f"{label}（{detail}）" if detail else label
    if advantage and concerns:
        result = f"{advantage_text}；但需注意" + "；".join(concerns) + "。"
    elif concerns:
        result = "需重点注意" + "；".join(concerns) + "。"
    elif advantage:
        result = f"{advantage_text}，岗位与公司整体{rating}，未见特别需要提醒的风险。"
    else:
        result = f"岗位与公司整体{rating}，现有信息未显示特别突出的优势或风险。"
    if len(result) > 120:
        result = result[:119].rstrip("，,；;。 ") + "…。"
    return result


def _requirements(job: dict[str, Any]) -> str:
    salary = job.get("salary")
    salary_display = salary.get("display") if isinstance(salary, dict) else salary
    values = [salary_display, job.get("experience"), job.get("degree")]
    return " / ".join(_collapse(value) for value in values if _collapse(value)) or "未披露"


def _input_paths(run_root: Path) -> dict[str, Path]:
    data_dir = run_root / "job-research-data"
    return {name: data_dir / name for name in INPUT_FILES}


def _load_inputs(run_root: Path) -> dict[str, Any]:
    paths = _input_paths(run_root)
    for path in paths.values():
        if not path.is_file() or path.is_symlink():
            raise S1Error(f"S6 输入缺失或不是普通文件：{path}", "missing_s6_input")

    s1 = validate_run_documents(str(run_root))
    s2 = s2_status(str(run_root))
    s3 = s3_status(str(run_root))
    s4 = s4_status(str(run_root))
    s5 = s5_status(str(run_root))
    pending = {
        "S1": s1.get("pending_combinations"),
        "S2": s2.get("pending_jobs"),
        "S3": s3.get("pending_jobs"),
        "S4": s4.get("pending_companies"),
        "S5": s5.get("pending_jobs"),
    }
    unfinished = [stage for stage, count in pending.items() if count != 0]
    if unfinished:
        raise S1Error(f"S6 上游尚未完成：{', '.join(unfinished)}", "s6_upstream_incomplete")

    documents = {name: load_json(path) for name, path in paths.items()}
    documents["config.json"] = validate_config(documents["config.json"])
    documents["_stage_status"] = {"s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5}
    documents["_input_hashes"] = {name: _file_sha256(path) for name, path in paths.items()}
    return documents


def _maps(documents: dict[str, Any]) -> dict[str, dict[str, Any]]:
    def by_job(document: str, *, record_type: str | None = None) -> dict[str, Any]:
        values = documents[document].get("records", [])
        return {
            item["job_key"]: item for item in values
            if isinstance(item, dict)
            and isinstance(item.get("job_key"), str)
            and (record_type is None or item.get("record_type") == record_type)
        }

    return {
        "jobs": by_job("job-index.json"),
        "details": by_job("job-details.json", record_type="job_detail"),
        "screenings": by_job("screening-results.json"),
        "scores": by_job("job-scores.json"),
        "companies": {
            item["company_key"]: item for item in documents["company-research.json"].get("records", [])
            if isinstance(item, dict) and isinstance(item.get("company_key"), str)
        },
        "skipped_company_jobs": {
            item["job_key"]: item for item in documents["company-research.json"].get("skipped_jobs", [])
            if isinstance(item, dict) and isinstance(item.get("job_key"), str)
        },
    }


def _city_order(config: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for item in config.get("search_scope", {}).get("search_urls", []):
        if isinstance(item, dict):
            city = _text(item.get("city_label"))
            if city and city not in result:
                result.append(city)
    return result


def _score_city(score: dict[str, Any], maps: dict[str, dict[str, Any]], allowed: set[str]) -> str:
    screening = maps["screenings"].get(score["job_key"], {})
    city = _text(screening.get("reporting", {}).get("report_city"))
    return city if city in allowed else "其他"


def _sorted_scores(documents: dict[str, Any], maps: dict[str, dict[str, Any]]) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    configured = _city_order(documents["config.json"])
    allowed = set(configured)
    grouped: dict[str, list[dict[str, Any]]] = {city: [] for city in configured}
    for score in documents["job-scores.json"].get("records", []):
        if not isinstance(score, dict) or score.get("job_key") not in maps["jobs"]:
            raise S1Error("S6 评分岗位无法关联 S1", "invalid_s6_relation")
        if score.get("company_key") not in maps["companies"]:
            raise S1Error("S6 评分岗位无法关联公司", "invalid_s6_relation")
        city = _score_city(score, maps, allowed)
        grouped.setdefault(city, []).append(score)
    cities = configured + (["其他"] if grouped.get("其他") else [])
    for city in cities:
        grouped[city].sort(key=lambda score: (
            score.get("overall", {}).get("score") is None,
            -(float(score["overall"]["score"]) if score.get("overall", {}).get("score") is not None else 0.0),
            int(maps["jobs"][score["job_key"]].get("first_recall_seq", 10**12)),
            score["job_key"],
        ))
    return cities, grouped


def _count_status(records: Iterable[dict[str, Any]], field: str = "status") -> dict[str, int]:
    result: dict[str, int] = {}
    for item in records:
        status = _text(item.get(field)) or "未知"
        result[status] = result.get(status, 0) + 1
    return result


def _status_display(counts: dict[str, int]) -> str:
    return "、".join(f"{key} {value}" for key, value in counts.items()) if counts else "无"


def _render_overview(documents: dict[str, Any]) -> list[str]:
    config = documents["config.json"]
    index_records = documents["job-index.json"].get("records", [])
    detail_records = [
        item for item in documents["job-details.json"].get("records", [])
        if isinstance(item, dict) and item.get("record_type") == "job_detail"
    ]
    screening = documents["screening-results.json"].get("records", [])
    companies = documents["company-research.json"].get("records", [])
    skipped_jobs = documents["company-research.json"].get("skipped_jobs", [])
    scores = documents["job-scores.json"].get("records", [])
    cities = _city_order(config)
    keywords = [
        _text(item.get("term")) for item in config.get("job_target", {}).get("search_keywords", [])
        if isinstance(item, dict) and _text(item.get("term"))
    ]
    return [
        "## 1. 本次任务概况",
        "",
        f"- 搜索城市：{_escape('、'.join(cities), fallback='未配置')}",
        f"- 搜索关键词：{_escape('、'.join(keywords), fallback='未配置')}",
        f"- 采集岗位：{len(index_records)} 个",
        f"- 岗位详情：{_escape(_status_display(_count_status(detail_records)))}",
        f"- 初筛结果：{_escape(_status_display(_count_status(screening)))}",
        f"- 公司背调：{len(companies)} 家（{_escape(_status_display(_count_status(companies)))}）",
        f"- 企业主体不可用、跳过背调与评分：{len(skipped_jobs) if isinstance(skipped_jobs, list) else 0} 个岗位",
        f"- 综合评分：{len(scores)} 个岗位",
        "",
        "> 分数只用于排序和解释，不构成淘汰线；网友评价来自公开主观样本，不代表公司全部团队。",
        "",
    ]


def _render_main_tables(
    documents: dict[str, Any], maps: dict[str, dict[str, Any]], cities: list[str], grouped: dict[str, list[dict[str, Any]]],
) -> list[str]:
    lines = ["## 2. 岗位决策总表", ""]
    if not any(grouped.get(city) for city in cities):
        return lines + ["没有进入 S5 的岗位。", ""]
    for city in cities:
        scores = grouped.get(city, [])
        if not scores:
            continue
        lines.extend([f"### {_escape(city)}", "", MAIN_HEADER, "|---:|---|---|---|---:|---:|---|---|---:|---|"])
        for rank, score in enumerate(scores, 1):
            job = maps["jobs"][score["job_key"]]
            company = maps["companies"][score["company_key"]]
            overall = score.get("overall", {})
            overall_display = (
                f"{_score(overall.get('score'))} / {_text(overall.get('rating'))}"
                if overall.get("score") is not None else "不可评分"
            )
            job_anchor = _anchor("job", score["job_key"])
            company_anchor = _anchor("company", score["company_key"])
            boss_url, _ = normalize_job_url(job.get("boss_job_url"))
            row = [
                str(rank),
                _escape(job.get("brand_company_name")),
                _escape(job.get("job_title")),
                _escape(_requirements(job)),
                _internal_link(_score(score.get("job_match", {}).get("score")), job_anchor),
                _internal_link(_score(score.get("company_evaluation", {}).get("score")), company_anchor),
                _escape(overall_display),
                _escape(_one_sentence(score)),
                _escape(_coverage(score.get("evidence_coverage", {}).get("overall"))),
                _external_link("查看岗位", boss_url),
            ]
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return lines


def _render_dimensions(group: dict[str, Any]) -> list[str]:
    lines = [
        "| 维度 | 状态 | 维度分 | 评分理由 |",
        "|---|---|---:|---|",
    ]
    dimensions = group.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        return lines + ["| 未披露 | 不可评分 | 不可评分 | 无 |"]
    for item in dimensions:
        lines.append("| " + " | ".join([
            _escape(item.get("name")),
            _escape(item.get("status")),
            _escape(_score(item.get("dimension_score"))),
            _escape(item.get("reason")),
        ]) + " |")
    return lines


def _render_job_details(
    maps: dict[str, dict[str, Any]], cities: list[str], grouped: dict[str, list[dict[str, Any]]],
) -> list[str]:
    lines = ["## 3. 岗位详细分析", ""]
    scores = [score for city in cities for score in grouped.get(city, [])]
    if not scores:
        return lines + ["没有进入 S5 的岗位。", ""]
    for score in scores:
        job = maps["jobs"][score["job_key"]]
        detail = maps["details"][score["job_key"]]
        screening = maps["screenings"][score["job_key"]]
        summary = detail.get("summary", {})
        boss_url, _ = normalize_job_url(job.get("boss_job_url"))
        lines.extend([
            f"<a id=\"{_anchor('job', score['job_key'])}\"></a>",
            f"### {_escape(job.get('brand_company_name'))}｜{_escape(job.get('job_title'))}",
            "",
            f"- 基本信息：{_escape(_requirements(job))}；{_escape(screening.get('reporting', {}).get('report_city'))}",
            f"- 岗位方向：{_escape(screening.get('reporting', {}).get('primary_direction'))}",
            f"- BOSS 链接：{_external_link('查看岗位详情', boss_url)}",
            f"- 岗位匹配分：{_score(score.get('job_match', {}).get('score'))}",
            f"- 岗位信息覆盖率：{_coverage(score.get('evidence_coverage', {}).get('job'))}",
            "",
            "#### 核心职责",
            "",
            *_bullet_lines(summary.get("core_responsibilities")),
            "",
            "#### 硬性要求",
            "",
            *_bullet_lines(summary.get("hard_requirements")),
            "",
            "#### 能力要求",
            "",
            *_bullet_lines(summary.get("key_capability_and_tool_requirements")),
            "",
            "#### 工作方式与风险",
            "",
            *_bullet_lines(summary.get("work_style_and_risks")),
            "",
            "#### JD 未披露或待确认",
            "",
            *_bullet_lines(summary.get("missing_or_uncertain")),
            "",
            "#### 岗位匹配维度",
            "",
            *_render_dimensions(score.get("job_match", {})),
            "",
        ])
        limitations = [
            item.get("reason") for item in score.get("information_limitations", [])
            if isinstance(item, dict) and item.get("group") == "job_match"
        ]
        lines.extend(["#### 信息限制", "", *_bullet_lines(limitations), ""])
    return lines


def _group_items(group: Any) -> list[dict[str, Any]]:
    if not isinstance(group, dict) or not isinstance(group.get("items"), list):
        return []
    return [item for item in group["items"] if isinstance(item, dict)]


def _render_group_items(title: str, group: dict[str, Any], *, empty_text: str) -> list[str]:
    lines = [f"#### {title}", "", f"查询状态：{_escape(group.get('query_status'))}", ""]
    items = _group_items(group)
    if items:
        for item in items:
            category = SUMMARY_CATEGORIES.get(_text(item.get("category")), _text(item.get("category")) or "其他")
            lines.append(f"- **{_escape(category)}**：{_escape(item.get('summary'))}")
    else:
        lines.append(f"- {_escape(empty_text)}")
    failures = group.get("failure_evidence")
    if isinstance(failures, list) and failures:
        lines.extend(["", "查询失败说明："])
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            platform = _escape(failure.get("platform"))
            reason = _escape(failure.get("reason"))
            url = failure.get("url")
            prefix = _external_link(failure.get("platform"), url) if url else platform
            lines.append(f"- {prefix}：{reason}")
    lines.append("")
    return lines


def _render_company_sources(company: dict[str, Any]) -> list[str]:
    lines = ["#### 来源", ""]
    evidence = company.get("evidence")
    visited = [
        item for item in evidence if isinstance(item, dict) and item.get("access_status") == "已访问"
    ] if isinstance(evidence, list) else []
    if not visited:
        return lines + ["- 没有已访问来源。", ""]
    for item in visited:
        label = f"{_text(item.get('platform'))}｜{_text(item.get('title'))}"
        lines.append(f"- {_external_link(label, item.get('url'))}")
    lines.append("")
    return lines


def _render_company_attempts(company: dict[str, Any]) -> list[str]:
    attempts = company.get("query_attempts")
    if not isinstance(attempts, list):
        return []
    review = [item for item in attempts if isinstance(item, dict) and item.get("group") == "employee_reviews"]
    if not review:
        return []
    lines = ["#### 网友评价平台查询情况", ""]
    for item in review:
        label = f"{_text(item.get('platform'))}｜{_text(item.get('search_term'))}"
        lines.append(
            f"- {_external_link(label, item.get('search_url'))}：{_escape(item.get('result_status'))}；"
            f"{_escape(item.get('note'), fallback='无补充说明')}"
        )
    lines.append("")
    return lines


def _render_companies(
    maps: dict[str, dict[str, Any]], cities: list[str], grouped: dict[str, list[dict[str, Any]]],
) -> list[str]:
    lines = ["## 4. 公司背调信息", ""]
    ordered_scores = [score for city in cities for score in grouped.get(city, [])]
    company_order: list[str] = []
    score_by_company: dict[str, dict[str, Any]] = {}
    for score in ordered_scores:
        key = score["company_key"]
        if key not in company_order:
            company_order.append(key)
            score_by_company[key] = score
    if not company_order:
        return lines + ["没有进入 S5 的公司。", ""]
    for key in company_order:
        company = maps["companies"][key]
        score = score_by_company[key]
        brand_names = company.get("brand_company_names")
        brand = _list_text(brand_names, fallback="未披露")
        lines.extend([
            f"<a id=\"{_anchor('company', key)}\"></a>",
            f"### {_escape(brand)}｜{_escape(company.get('enterprise_name'))}",
            "",
            f"- 真实企业名称：{_escape(company.get('enterprise_name'))}",
            f"- 统一社会信用代码：{_escape(company.get('unified_social_credit_code'))}",
            f"- 公司背调状态：{_escape(company.get('status'))}",
            f"- 公司评价分：{_score(score.get('company_evaluation', {}).get('score'))}",
            f"- 公司信息覆盖率：{_coverage(score.get('evidence_coverage', {}).get('company'))}",
            "",
            *_render_group_items("公司基本信息", company.get("basic_profile", {}), empty_text="未取得可展示的公司基本信息。"),
            *_render_group_items("公开风险", company.get("public_risks", {}), empty_text="规定查询已完成，未发现可保存的公开风险事项。"),
            "#### 员工评价（公开主观样本）",
            "",
            f"查询状态：{_escape(company.get('employee_reviews', {}).get('query_status'))}",
            "",
        ])
        review_items = _group_items(company.get("employee_reviews", {}))
        if review_items:
            for item in review_items:
                category = SUMMARY_CATEGORIES.get(_text(item.get("category")), _text(item.get("category")) or "其他")
                lines.append(f"- **{_escape(category)}**：{_escape(item.get('summary'))}")
        else:
            lines.append("- 规定查询已完成，但没有找到可保存的网友评价事项。")
        failures = company.get("employee_reviews", {}).get("failure_evidence")
        if isinstance(failures, list) and failures:
            lines.extend(["", "查询失败说明："])
            for failure in failures:
                if not isinstance(failure, dict):
                    continue
                prefix = (
                    _external_link(failure.get("platform"), failure.get("url"))
                    if failure.get("url") else _escape(failure.get("platform"))
                )
                lines.append(f"- {prefix}：{_escape(failure.get('reason'))}")
        lines.extend(["", *_render_company_attempts(company), "#### 公司评价维度", "", *_render_dimensions(score.get("company_evaluation", {})), ""])
        limitations = [
            item.get("reason") for item in score.get("information_limitations", [])
            if isinstance(item, dict) and item.get("group") == "company_evaluation"
        ]
        lines.extend(["#### 信息限制", "", *_bullet_lines(limitations), "", *_render_company_sources(company)])
    return lines


def _render_unscored(maps: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["## 5. 未进入评分的岗位", ""]
    scored = set(maps["scores"])
    records = [item for key, item in maps["screenings"].items() if key not in scored]
    records.sort(key=lambda item: (
        _text(item.get("reporting", {}).get("report_city")),
        int(maps["jobs"].get(item.get("job_key"), {}).get("first_recall_seq", 10**12)),
        _text(item.get("job_key")),
    ))
    if not records:
        return lines + ["所有初筛记录都已进入评分。", ""]
    lines.extend([
        "| 公司 | 岗位 | 城市 | 初筛状态 | 原因 | 待确认事项 | 岗位链接 |",
        "|---|---|---|---|---|---|---|",
    ])
    for item in records:
        job = maps["jobs"].get(item.get("job_key"))
        if job is None:
            raise S1Error("S6 未评分岗位无法关联 S1", "invalid_s6_relation")
        skipped = maps["skipped_company_jobs"].get(item.get("job_key"))
        status_text = item.get("status")
        reason_text = item.get("reason")
        verify_items = list(item.get("items_to_verify") or [])
        if skipped is not None:
            status_text = "初筛通过（企业主体不可用，已跳过后续流程）"
            reason_text = skipped.get("reason")
            verify_items.append("如需继续评估，应先人工补充并确认真实企业名称")
        boss_url, _ = normalize_job_url(job.get("boss_job_url"))
        lines.append("| " + " | ".join([
            _escape(job.get("brand_company_name")),
            _escape(job.get("job_title")),
            _escape(item.get("reporting", {}).get("report_city")),
            _escape(status_text),
            _escape(reason_text),
            _escape(_list_text(verify_items)),
            _external_link("查看岗位", boss_url),
        ]) + " |")
    lines.append("")
    return lines


def render_report(documents: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    maps = _maps(documents)
    cities, grouped = _sorted_scores(documents, maps)
    lines = ["# 岗位决策报告", ""]
    lines.extend(_render_overview(documents))
    lines.extend(_render_main_tables(documents, maps, cities, grouped))
    lines.extend(_render_job_details(maps, cities, grouped))
    lines.extend(_render_companies(maps, cities, grouped))
    lines.extend(_render_unscored(maps))
    report = "\n".join(lines).rstrip() + "\n"
    counts = {
        "collected_jobs": len(maps["jobs"]),
        "detail_jobs": len(maps["details"]),
        "screening_records": len(maps["screenings"]),
        "screening_status_counts": _count_status(maps["screenings"].values()),
        "researched_companies": len(maps["companies"]),
        "skipped_company_identity_jobs": len(maps["skipped_company_jobs"]),
        "scored_jobs": len(maps["scores"]),
        "unscored_jobs": len(maps["screenings"]) - len(maps["scores"]),
    }
    validate_report(report, documents, maps, counts)
    return report, counts


def _unescaped_pipes(line: str) -> int:
    count = 0
    backslashes = 0
    for character in line:
        if character == "\\":
            backslashes += 1
            continue
        if character == "|" and backslashes % 2 == 0:
            count += 1
        backslashes = 0
    return count


def validate_report(
    report: str, documents: dict[str, Any], maps: dict[str, dict[str, Any]], counts: dict[str, Any],
) -> None:
    if not report.endswith("\n") or any(report.count(section) != 1 for section in EXPECTED_SECTIONS):
        raise S1Error("S6 报告章节不完整或重复", "invalid_s6_report")
    if "<!--" in report or "<script" in report.casefold() or re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", report):
        raise S1Error("S6 报告含不安全标记或残留占位符", "invalid_s6_report")
    score_records = documents["job-scores.json"].get("records", [])
    for score in score_records:
        job_anchor = _anchor("job", score["job_key"])
        company_anchor = _anchor("company", score["company_key"])
        if report.count(f'<a id="{job_anchor}"></a>') != 1 or report.count(f"](#{job_anchor})") != 1:
            raise S1Error("S6 岗位锚点或主表链接不唯一", "invalid_s6_anchor")
        if report.count(f'<a id="{company_anchor}"></a>') != 1:
            raise S1Error("S6 公司锚点不唯一", "invalid_s6_anchor")
    expected_company_links = _count_status(
        [{"status": item["company_key"]} for item in score_records]
    )
    for company_key, count in expected_company_links.items():
        if report.count(f"](#{_anchor('company', company_key)})") != count:
            raise S1Error("S6 公司分链接数量不一致", "invalid_s6_anchor")
    main_header_count = report.count(MAIN_HEADER)
    expected_city_tables = sum(bool(values) for values in _sorted_scores(documents, maps)[1].values())
    if main_header_count != expected_city_tables:
        raise S1Error("S6 城市主表数量不一致", "invalid_s6_report")
    in_main_tables = False
    for line in report.splitlines():
        if line == "## 2. 岗位决策总表":
            in_main_tables = True
        elif line == "## 3. 岗位详细分析":
            in_main_tables = False
        if in_main_tables and line.startswith("|"):
            if _unescaped_pipes(line) != 11:
                raise S1Error("S6 岗位决策总表列数无效", "invalid_s6_table")
    external_targets = re.findall(r"\]\((https://[^)]+)\)", report)
    for target in external_targets:
        if _safe_https_url(target) != target:
            raise S1Error("S6 报告含非规范外部链接", "invalid_s6_url")
    boss_links = [maps["jobs"][key]["boss_job_url"] for key in maps["screenings"]]
    if any(_safe_https_url(normalize_job_url(url)[0]) not in report for url in boss_links):
        raise S1Error("S6 报告缺少岗位链接", "invalid_s6_report")
    if counts["unscored_jobs"] < 0 or counts["scored_jobs"] != len(score_records):
        raise S1Error("S6 报告数量关系无效", "invalid_s6_counts")


def _manifest(documents: dict[str, Any], report: str, counts: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "report_contract_version": REPORT_CONTRACT_VERSION,
        "config_hash": documents["config.json"]["config_hash"],
        "inputs": documents["_input_hashes"],
        "reports": [{
            "path": REPORT_RELATIVE_PATH,
            "sha256": _sha256_bytes(report.encode("utf-8")),
            "validation_status": "passed",
        }],
        "counts": counts,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    payload = text.encode("utf-8")
    destination = path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise S1Error(f"拒绝替换符号链接：{destination}", "unsafe_path")
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
        )
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _restore(path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write_text(path, previous.decode("utf-8"))


def build(run_root_value: str) -> dict[str, Any]:
    run_root = Path(run_root_value).resolve()
    documents = _load_inputs(run_root)
    report, counts = render_report(documents)
    manifest = _manifest(documents, report, counts)
    report_path = run_root / REPORT_RELATIVE_PATH
    manifest_path = run_root / MANIFEST_RELATIVE_PATH
    previous_report = report_path.read_bytes() if report_path.exists() else None
    _atomic_write_text(report_path, report)
    try:
        atomic_write_json(manifest_path, manifest)
    except Exception:
        _restore(report_path, previous_report)
        raise
    return {
        "ok": True,
        "report": str(report_path),
        "manifest": str(manifest_path),
        "report_sha256": manifest["reports"][0]["sha256"],
        "counts": counts,
    }


def validate(run_root_value: str) -> dict[str, Any]:
    run_root = Path(run_root_value).resolve()
    documents = _load_inputs(run_root)
    report_path = run_root / REPORT_RELATIVE_PATH
    manifest_path = run_root / MANIFEST_RELATIVE_PATH
    try:
        report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise S1Error("S6 最终报告无法读取", "s6_read_failed") from exc
    manifest = load_json(manifest_path)
    expected_report, counts = render_report(documents)
    if report != expected_report:
        raise S1Error("S6 最终报告与当前结构化输入的确定性结果不一致", "stale_s6_report")
    expected = _manifest(documents, report, counts)
    if manifest != expected:
        raise S1Error("S6 manifest 与当前输入或报告不一致", "stale_s6_manifest")
    validate_report(report, documents, _maps(documents), counts)
    return {
        "ok": True,
        "report": str(report_path),
        "manifest": str(manifest_path),
        "report_sha256": expected["reports"][0]["sha256"],
        "counts": counts,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for command in ("build", "validate"):
        item = sub.add_parser(command)
        item.add_argument("--run-root", required=True)
        item.add_argument("--task-id", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    from task_manager import validate_task

    validate_task(args.run_root, args.task_id)
    result = build(args.run_root) if args.command == "build" else validate(args.run_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except S1Error as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False), file=os.sys.stderr)
        raise SystemExit(2) from exc
