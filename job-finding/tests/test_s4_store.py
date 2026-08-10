from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from s2_store import upsert as upsert_s2  # noqa: E402
from s3_store import upsert as upsert_s3  # noqa: E402
from s4_store import pending, status, upsert  # noqa: E402
from task_config import generate_search_plan, prepare  # noqa: E402
from test_task_config import payload as config_payload  # noqa: E402


COMPANY_URL = "https://www.zhipin.com/gongsi/s4-company.html"
OTHER_COMPANY_URL = "https://www.zhipin.com/gongsi/s4-other.html"


def query_url(platform_key: str, term: str, index: int) -> str:
    encoded = quote(term, safe="")
    values = {
        "aiqicha": f"https://www.aiqicha.com/s?q={encoded}",
        "official_website": "https://example.com/about",
        "zhihu": f"https://www.zhihu.com/search?q={encoded}",
        "xiaohongshu": f"https://www.xiaohongshu.com/search_result?keyword={encoded}",
        "nowcoder": f"https://www.nowcoder.com/search/all/?query={encoded}",
        "maimai": f"https://maimai.cn/web/search_center?type=feed&query={encoded}&sid=test-{index}",
    }
    return values[platform_key]


def job(index: int) -> dict:
    job_id = f"s4-job-{index}"
    return {
        "job_key": f"id:{job_id}",
        "job_id": job_id,
        "boss_job_url": f"https://www.zhipin.com/job_detail/{job_id}.html",
        "job_title": f"测试岗位{index}",
        "brand_company_name": "示例招聘品牌" if index < 3 else "其他品牌",
        "source_cities": ["示例城市"],
        "card_city": "示例城市",
        "salary": {"raw": "10-20K", "display": "10-20K", "parse_status": "已解析"},
        "experience": "经验不限",
        "degree": "本科",
    }


def browser(index: int, company_status: str) -> dict:
    company_url = COMPANY_URL if index < 3 else OTHER_COMPANY_URL
    value = {
        "job_key": job(index)["job_key"],
        "job_id": job(index)["job_id"],
        "requested_url": job(index)["boss_job_url"],
        "detail": {
            "status": "ok",
            "final_url": job(index)["boss_job_url"],
            "selector": ".job-detail-section",
            "jd_text": "负责客户需求分析，设计AI解决方案并协调研发完成交付。",
            "page_job_title": job(index)["job_title"],
            "boss_company_name": job(index)["brand_company_name"],
            "company_page_url": company_url,
            "failure": None,
        },
        "company": {"status": company_status, "company_page_url": company_url},
    }
    if company_status == "acquired":
        value["company"].update({
            "source_selector": ".job-sec.company-business",
            "fields": {
                "企业名称": "示例科技有限公司" if index < 3 else "其他科技有限公司",
                "统一社会信用代码": "91110000123456789A" if index < 3 else "91110000987654321B",
                "法定代表人": "示例代表",
                "成立时间": "2020-01-01",
                "注册资本": "1000万人民币",
                "注册地址": "示例地址",
            },
        })
    return value


def semantic() -> dict:
    return {
        "summary": {
            "core_responsibilities": ["分析客户需求并设计AI解决方案"],
            "hard_requirements": [],
            "key_capability_and_tool_requirements": ["客户沟通和技术协调"],
            "work_style_and_risks": ["需要跨团队推进交付"],
            "missing_or_uncertain": [],
        },
        "evidence": [
            {"category": "responsibility", "text": "负责客户需求分析，设计AI解决方案并协调研发完成交付。"},
        ],
    }


class S4StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        data_dir = self.run_root / "job-research-data"
        config = config_payload()
        config["job_target"]["search_keywords"] = [
            {"term": "解决方案", "directions": ["解决方案"]},
        ]
        config["search_scope"]["search_mode"] = "per_city_target"
        config["search_scope"]["per_city_target_count"] = 3
        prepare(str(self.run_root), config)
        normalized = load_json(data_dir / "config.json")
        combo = generate_search_plan(normalized)[0]
        records = []
        for index in (1, 2, 3):
            record = job(index)
            record.update({
                "job_id_aliases": [record["job_id"]],
                "first_recall_seq": index,
                "source_combinations": [combo["combo_key"]],
                "source_terms": ["解决方案"],
                "posted_at": "",
            })
            records.append(record)
        atomic_write_json(data_dir / "job-index.json", {
            "schema_version": 1,
            "config_hash": normalized["config_hash"],
            "collection_mode": "per_city_target",
            "sample_limit": None,
            "records": records,
        })
        atomic_write_json(data_dir / "checkpoint.json", {
            "schema_version": 1,
            "config_hash": normalized["config_hash"],
            "collection_mode": "per_city_target",
            "combinations": [{
                "combo_key": combo["combo_key"],
                "search_url_order": combo["search_url_order"],
                "keyword_order": combo["keyword_order"],
                "city": combo["city_label"],
                "term": combo["term"],
                "status": "completed",
                "evidence": {
                    "sample_limit": None,
                    "stop_reason": "natural_exhaustion",
                    "consecutive_no_new_rounds": 10,
                    "initial_visible_count": 3,
                    "scroll_rounds": 10,
                    "manual_scroll_rounds": 10,
                    "automated_scroll_rounds": 0,
                    "successful_refresh_rounds": 0,
                    "end_marker_seen": True,
                    "scroll_trace": [],
                    "unique_jobs_after_scroll": 3,
                    "collected_job_count": 3,
                },
            }],
        })
        upsert_s2(str(self.run_root), {"browser": browser(1, "acquired"), "semantic": semantic()})
        upsert_s2(str(self.run_root), {"browser": browser(2, "reused"), "semantic": semantic()})
        upsert_s2(str(self.run_root), {"browser": browser(3, "not_found"), "semantic": semantic()})
        details = load_json(data_dir / "job-details.json")
        evidence = {
            record["job_key"]: record["evidence"][0]["evidence_id"]
            for record in details["records"] if record.get("record_type") == "job_detail"
        }
        for index in (1, 2):
            upsert_s3(str(self.run_root), {
                "job_key": job(index)["job_key"],
                "status": "初筛通过",
                "reason": "岗位存在客户需求分析和解决方案职责",
                "evidence_ids": [evidence[job(index)["job_key"]]],
                "items_to_verify": [],
                "reporting": {"primary_direction": "解决方案", "other_directions": []},
            })
        upsert_s3(str(self.run_root), {
            "job_key": job(3)["job_key"],
            "status": "初筛通过",
            "reason": "岗位存在客户需求分析和解决方案职责",
            "evidence_ids": [evidence[job(3)["job_key"]]],
            "items_to_verify": [],
            "reporting": {"primary_direction": "解决方案", "other_directions": []},
        })
        self.task = pending(str(self.run_root), 1)["pending"][0]
        self.company_key = self.task["company_key"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, *, all_complete: bool = False) -> dict:
        evidence = [
            {
                "source_ref": "aqc1",
                "group": "basic_profile",
                "platform_key": "aiqicha",
                "source_type": "business_information_platform",
                "platform": "爱企查",
                "title": "示例科技有限公司企业信息",
                "url": "https://www.aiqicha.com/company_detail_example",
                "excerpt": "企业信息页面展示示例科技有限公司的登记与业务信息。",
                "access_status": "已访问",
                "content_type": "page",
            },
            {
                "source_ref": "basic1",
                "group": "basic_profile",
                "platform_key": "official_website",
                "source_type": "official_company",
                "platform": "公司官网",
                "title": "公司介绍",
                "url": "https://example.com/about",
                "excerpt": "公司提供企业级人工智能解决方案。",
                "access_status": "已访问",
                "content_type": "page",
            },
            {
                "source_ref": "risk1",
                "group": "public_risks",
                "platform_key": "government",
                "source_type": "government",
                "platform": "政府平台",
                "title": "企业信息查询",
                "url": "https://example.gov.cn/company",
                "excerpt": "查询页面未显示需要保存的公开风险事项。",
                "access_status": "已访问",
                "content_type": "page",
            },
            {
                "source_ref": "review_post",
                "group": "employee_reviews",
                "platform_key": "nowcoder",
                "source_type": "employee_review_platform",
                "platform": "牛客网",
                "title": "示例招聘品牌讨论帖",
                "url": "https://www.nowcoder.com/discuss/example-post",
                "excerpt": "帖子讨论了团队协作和工作节奏。",
                "access_status": "已访问",
                "content_type": "post",
            },
            {
                "source_ref": "review_comment",
                "group": "employee_reviews",
                "platform_key": "nowcoder",
                "source_type": "employee_review_platform",
                "platform": "牛客网",
                "title": "示例招聘品牌讨论帖评论",
                "url": "https://www.nowcoder.com/discuss/example-post?comment=1",
                "excerpt": "评论补充了加班和成长机会的个人体验。",
                "access_status": "已访问",
                "content_type": "comment",
            },
        ]
        query_attempts = []
        failed_key = None
        for index, required in enumerate(self.task["required_queries"]):
            result_status = "未找到相关内容"
            note = "已按关键词检查，没有找到可保存的相关内容"
            if required["platform_key"] in {"aiqicha", "official_website", "nowcoder"}:
                result_status = "找到相关内容"
                note = "已打开相关原始页面并检查内容"
            if not all_complete and required["platform_key"] == "maimai" and failed_key is None:
                result_status = "查询失败"
                note = "页面要求登录，当前无法读取帖子和评论"
                failed_key = required
            query_attempts.append({
                "group": required["group"],
                "platform_key": required["platform_key"],
                "platform": required["platform"],
                "search_term": required["search_term"],
                "search_term_type": required["search_term_type"],
                "result_status": result_status,
                "search_url": query_url(required["platform_key"], required["search_term"], index),
                "content_types_reviewed": required["required_content_types"],
                "note": note,
            })
        employee_reviews = {
            "query_status": "已完成" if all_complete else "部分完成",
            "items": [
                {
                    "category": "work_intensity",
                    "summary": "公开帖子和评论提到工作节奏与加班体验",
                    "evidence_refs": ["review_post", "review_comment"],
                }
            ],
            "source_evidence_refs": ["review_post", "review_comment"],
            "failure_evidence": [
                {"platform": "脉脉", "url": "https://maimai.cn/search", "reason": "页面要求登录"},
            ],
        }
        if all_complete:
            employee_reviews["failure_evidence"] = []
        return {
            "company_key": self.company_key,
            "query_attempts": query_attempts,
            "evidence": evidence,
            "basic_profile": {
                "query_status": "已完成",
                "items": [
                    {"category": "main_business", "summary": "主营企业级人工智能解决方案", "evidence_refs": ["aqc1", "basic1"]},
                    {"category": "registered_capital", "summary": "注册资本1000万元", "evidence_refs": ["aqc1"]},
                ],
                "source_evidence_refs": ["aqc1", "basic1"],
                "failure_evidence": [],
            },
            "public_risks": {
                "query_status": "已完成",
                "items": [],
                "source_evidence_refs": ["risk1"],
                "failure_evidence": [],
            },
            "employee_reviews": employee_reviews,
        }

    def test_pending_only_includes_passed_company_and_deduplicates(self) -> None:
        result = pending(str(self.run_root), 10)
        self.assertEqual(1, result["remaining"])
        task = result["pending"][0]
        self.assertEqual("示例科技有限公司", task["enterprise_name"])
        self.assertEqual([job(1)["job_key"], job(2)["job_key"]], task["linked_job_keys"])
        self.assertEqual(
            {"aiqicha", "official_website", "zhihu", "xiaohongshu", "nowcoder", "maimai"},
            {item["platform_key"] for item in task["required_queries"]},
        )
        self.assertEqual(10, len(task["required_queries"]))
        self.assertEqual(job(3)["job_key"], result["skipped_jobs"][0]["job_key"])
        self.assertEqual("enterprise_name_not_found", result["skipped_jobs"][0]["reason_code"])

    def test_writes_partial_result_with_program_identity(self) -> None:
        result = upsert(str(self.run_root), self.payload())
        self.assertEqual(1, result["status_counts"]["部分完成"])
        self.assertEqual(1, result["skipped_jobs"])
        document = load_json(self.run_root / "job-research-data" / "company-research.json")
        record = document["records"][0]
        self.assertEqual(job(3)["job_key"], document["skipped_jobs"][0]["job_key"])
        self.assertEqual("91110000123456789A", record["unified_social_credit_code"])
        self.assertEqual(64, len(record["evidence"][0]["evidence_id"]))
        self.assertNotIn("source_ref", record["evidence"][0])

    def test_all_completed_can_preserve_empty_result_groups(self) -> None:
        result = upsert(str(self.run_root), self.payload(all_complete=True))
        self.assertEqual(1, result["status_counts"]["已完成"])

    def test_completed_group_requires_visited_source(self) -> None:
        value = self.payload()
        value["public_risks"]["source_evidence_refs"] = []
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_partial_group_requires_failure_evidence(self) -> None:
        value = self.payload()
        value["employee_reviews"]["failure_evidence"] = []
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_cross_group_evidence(self) -> None:
        value = self.payload()
        value["public_risks"]["source_evidence_refs"] = ["basic1"]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_model_confidence(self) -> None:
        value = self.payload()
        value["confidence"] = 0.9
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_s3_must_be_complete(self) -> None:
        path = self.run_root / "job-research-data" / "screening-results.json"
        document = load_json(path)
        document["records"] = document["records"][:-1]
        atomic_write_json(path, document)
        with self.assertRaises(S1Error):
            pending(str(self.run_root), 1)

    def test_changed_screening_invalidates_existing_result(self) -> None:
        upsert(str(self.run_root), self.payload())
        path = self.run_root / "job-research-data" / "screening-results.json"
        changed = copy.deepcopy(load_json(path))
        changed["records"][0]["reason"] = "更新后的有效理由"
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            status(str(self.run_root))

    def test_rejects_wrong_company_order(self) -> None:
        value = self.payload()
        value["company_key"] = "company:" + "0" * 64
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_missing_fixed_query(self) -> None:
        value = self.payload()
        value["query_attempts"].pop()
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_review_query_without_posts_and_comments(self) -> None:
        value = self.payload()
        review = next(item for item in value["query_attempts"] if item["group"] == "employee_reviews")
        review["content_types_reviewed"] = ["post"]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_review_query_on_wrong_platform_host(self) -> None:
        value = self.payload()
        review = next(item for item in value["query_attempts"] if item["group"] == "employee_reviews")
        review["search_url"] = f"https://example.com/search?q={quote(review['search_term'], safe='')}"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_aiqicha_query_on_wrong_platform_host(self) -> None:
        value = self.payload()
        attempt = next(item for item in value["query_attempts"] if item["platform_key"] == "aiqicha")
        attempt["search_url"] = "https://example.com/search?q=示例科技有限公司"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_noncanonical_review_search_url(self) -> None:
        value = self.payload()
        review = next(item for item in value["query_attempts"] if item["platform_key"] == "maimai")
        review["search_url"] = "https://maimai.cn/web/search_center?query=示例科技有限公司"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_aiqicha_risk_statistics_as_evidence(self) -> None:
        value = self.payload()
        risk = next(item for item in value["evidence"] if item["source_ref"] == "risk1")
        risk["platform_key"] = "aiqicha"
        risk["source_type"] = "business_information_platform"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_aiqicha_found_requires_original_source(self) -> None:
        value = self.payload()
        value["evidence"] = [item for item in value["evidence"] if item["source_ref"] != "aqc1"]
        value["basic_profile"]["source_evidence_refs"] = ["basic1"]
        value["basic_profile"]["items"][0]["evidence_refs"] = ["basic1"]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)


if __name__ == "__main__":
    unittest.main()
