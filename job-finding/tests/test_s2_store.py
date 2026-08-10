from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from s2_store import pending, status, upsert  # noqa: E402
from task_config import generate_search_plan, prepare  # noqa: E402
from test_task_config import generic_payload  # noqa: E402


COMPANY_URL = "https://www.zhipin.com/gongsi/example-company.html"
JD_TEXT = "岗位职责\n负责客户需求分析和AI解决方案设计\n任职要求\n本科及以上，具备良好的客户沟通能力\n需要偶尔出差"


def job(index: int) -> dict:
    job_id = f"s2-job-{index}"
    return {
        "job_key": f"id:{job_id}",
        "job_id": job_id,
        "boss_job_url": f"https://www.zhipin.com/job_detail/{job_id}.html",
        "job_title": f"测试岗位{index}",
        "brand_company_name": "示例招聘品牌",
    }


def browser_payload(index: int, *, company_status: str = "acquired") -> dict:
    value = {
        "job_key": job(index)["job_key"],
        "job_id": job(index)["job_id"],
        "requested_url": job(index)["boss_job_url"],
        "detail": {
            "status": "ok",
            "final_url": job(index)["boss_job_url"],
            "selector": ".job-detail-section",
            "jd_text": JD_TEXT,
            "page_job_title": job(index)["job_title"],
            "boss_company_name": "示例公司",
            "company_page_url": COMPANY_URL,
            "failure": None,
        },
        "company": {
            "status": company_status,
            "company_page_url": COMPANY_URL,
        },
    }
    if company_status == "acquired":
        value["company"].update({
            "source_selector": ".job-sec.company-business",
            "fields": {
                "企业名称": "示例科技有限公司",
                "统一社会信用代码": "91310109695753999F",
                "法定代表人": "示例姓名",
                "成立时间": "2020-01-01",
                "注册资本": "1000万人民币",
                "注册地址": "广州市示例路1号",
            },
        })
    return value


def semantic_payload() -> dict:
    return {
        "summary": {
            "core_responsibilities": ["分析客户需求并设计AI解决方案"],
            "hard_requirements": ["本科及以上"],
            "key_capability_and_tool_requirements": ["客户沟通能力"],
            "work_style_and_risks": ["需要偶尔出差"],
            "missing_or_uncertain": ["未说明编码工作占比"],
        },
        "evidence": [
            {"category": "responsibility", "text": "负责客户需求分析和AI解决方案设计"},
            {"category": "requirement", "text": "本科及以上，具备良好的客户沟通能力"},
            {"category": "work_style", "text": "需要偶尔出差"},
        ],
    }


class S2StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        data_dir = self.run_root / "job-research-data"
        config_input = generic_payload()
        config_input["job_target"]["search_keywords"] = [
            {"term": "产品经理", "directions": ["产品与业务"]}
        ]
        config_input["search_scope"]["search_mode"] = "per_city_target"
        config_input["search_scope"]["per_city_target_count"] = 2
        prepare(str(self.run_root), config_input)
        config = load_json(data_dir / "config.json")
        combo = generate_search_plan(config)[0]
        records = []
        for index in (1, 2):
            record = job(index)
            record.update({
                "job_id_aliases": [record["job_id"]],
                "first_recall_seq": index,
                "source_combinations": [combo["combo_key"]],
                "salary": {"raw": "10-20K", "display": "10-20K", "parse_status": "已解析"},
                "experience": "经验不限",
                "degree": "本科",
                "card_city": "示例城市",
                "source_cities": ["示例城市"],
                "source_terms": ["产品经理"],
                "posted_at": "",
            })
            records.append(record)
        atomic_write_json(data_dir / "job-index.json", {
            "schema_version": 1,
            "config_hash": config["config_hash"],
            "collection_mode": "per_city_target",
            "sample_limit": None,
            "records": records,
        })
        atomic_write_json(data_dir / "checkpoint.json", {
            "schema_version": 1,
            "config_hash": config["config_hash"],
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
                    "initial_visible_count": 2,
                    "scroll_rounds": 10,
                    "manual_scroll_rounds": 10,
                    "automated_scroll_rounds": 0,
                    "successful_refresh_rounds": 0,
                    "end_marker_seen": True,
                    "scroll_trace": [],
                    "unique_jobs_after_scroll": 2,
                    "collected_job_count": 2,
                },
            }],
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_summary_and_company_without_full_jd(self) -> None:
        result = upsert(str(self.run_root), {"browser": browser_payload(1), "semantic": semantic_payload()})
        self.assertEqual(1, result["job_details"])
        document = load_json(self.run_root / "job-research-data" / "job-details.json")
        self.assertNotIn("jd_text", str(document))
        detail = next(record for record in document["records"] if record["record_type"] == "job_detail")
        subject = next(record for record in document["records"] if record["record_type"] == "boss_company_subject")
        self.assertEqual("已完成", detail["status"])
        self.assertEqual("示例科技有限公司", subject["business"]["enterprise_name"])

    def test_rejects_evidence_not_found_in_current_jd(self) -> None:
        semantic = semantic_payload()
        semantic["evidence"][0]["text"] = "JD 中不存在的证据"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), {"browser": browser_payload(1), "semantic": semantic})

    def test_persists_detail_failure_with_empty_summary(self) -> None:
        browser = browser_payload(1)
        browser["detail"].update({
            "status": "failed",
            "selector": None,
            "jd_text": "",
            "failure": {"code": "selector_missing", "message": "未找到详情区域"},
        })
        browser["detail"]["company_page_url"] = None
        browser["company"] = {"status": "not_applicable"}
        upsert(str(self.run_root), {"browser": browser, "semantic": None})
        document = load_json(self.run_root / "job-research-data" / "job-details.json")
        detail = document["records"][0]
        self.assertEqual("失败", detail["status"])
        self.assertEqual([], detail["evidence"])
        self.assertTrue(all(not value for value in detail["summary"].values()))

    def test_reuses_one_company_subject_for_two_jobs(self) -> None:
        upsert(str(self.run_root), {"browser": browser_payload(1), "semantic": semantic_payload()})
        upsert(str(self.run_root), {"browser": browser_payload(2, company_status="reused"), "semantic": semantic_payload()})
        document = load_json(self.run_root / "job-research-data" / "job-details.json")
        subjects = [record for record in document["records"] if record["record_type"] == "boss_company_subject"]
        self.assertEqual(1, len(subjects))
        self.assertEqual([job(1)["job_key"], job(2)["job_key"]], subjects[0]["linked_job_keys"])

    def test_marks_missing_enterprise_name_without_failing_job_detail(self) -> None:
        result = upsert(
            str(self.run_root),
            {"browser": browser_payload(1, company_status="not_found"), "semantic": semantic_payload()},
        )
        document = load_json(self.run_root / "job-research-data" / "job-details.json")
        detail = next(record for record in document["records"] if record["record_type"] == "job_detail")
        subject = next(record for record in document["records"] if record["record_type"] == "boss_company_subject")
        self.assertEqual("已完成", detail["status"])
        self.assertEqual("未取得", subject["business"]["status"])
        self.assertEqual("", subject["business"]["enterprise_name"])
        self.assertEqual(1, result["company_subject_status_counts"]["未取得"])
        self.assertEqual(1, result["pending_jobs"])

    def test_rejects_job_url_mismatch(self) -> None:
        browser = browser_payload(1)
        browser["detail"]["final_url"] = job(2)["boss_job_url"]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), {"browser": browser, "semantic": semantic_payload()})

    def test_rejects_job_title_mismatch(self) -> None:
        browser = browser_payload(1)
        browser["detail"]["page_job_title"] = "另一个岗位"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), {"browser": browser, "semantic": semantic_payload()})

    def test_returns_next_unprocessed_job(self) -> None:
        self.assertEqual(job(1)["job_key"], pending(str(self.run_root), 1)["pending"][0]["job_key"])
        upsert(str(self.run_root), {"browser": browser_payload(1), "semantic": semantic_payload()})
        self.assertEqual(job(2)["job_key"], status(str(self.run_root))["next_job_key"])

    def test_rejects_changed_job_index(self) -> None:
        upsert(str(self.run_root), {"browser": browser_payload(1), "semantic": semantic_payload()})
        path = self.run_root / "job-research-data" / "job-index.json"
        index = load_json(path)
        changed = copy.deepcopy(index)
        changed["records"][0]["job_title"] = "发生变化的岗位名"
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            status(str(self.run_root))

    def test_rejects_s2_before_s1_is_complete(self) -> None:
        path = self.run_root / "job-research-data" / "checkpoint.json"
        checkpoint = load_json(path)
        checkpoint["combinations"] = []
        atomic_write_json(path, checkpoint)
        with self.assertRaises(S1Error):
            pending(str(self.run_root), 1)

    def test_rejects_out_of_order_submission(self) -> None:
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), {"browser": browser_payload(2), "semantic": semantic_payload()})


if __name__ == "__main__":
    unittest.main()
