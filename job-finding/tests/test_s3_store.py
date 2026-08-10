from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from s2_store import upsert as upsert_s2  # noqa: E402
from s3_store import pending, status, upsert  # noqa: E402
from task_config import generate_search_plan, normalize_config, prepare  # noqa: E402
from test_task_config import payload as config_payload  # noqa: E402


def job(index: int) -> dict:
    job_id = f"s3-job-{index}"
    return {
        "job_key": f"id:{job_id}",
        "job_id": job_id,
        "boss_job_url": f"https://www.zhipin.com/job_detail/{job_id}.html",
        "job_title": f"测试岗位{index}",
        "brand_company_name": "示例品牌",
        "source_cities": ["广州"],
        "card_city": "广州",
        "salary": {"raw": "10-20K", "display": "10-20K", "parse_status": "已解析"},
        "experience": "经验不限",
        "degree": "本科",
    }


def detail_browser(index: int, *, failed: bool = False) -> dict:
    jd_text = "负责客户需求分析，设计AI解决方案并协调研发完成POC交付。要求本科及以上学历。"
    return {
        "job_key": job(index)["job_key"],
        "job_id": job(index)["job_id"],
        "requested_url": job(index)["boss_job_url"],
        "detail": {
            "status": "failed" if failed else "ok",
            "final_url": job(index)["boss_job_url"],
            "selector": None if failed else ".job-detail-section",
            "jd_text": "" if failed else jd_text,
            "page_job_title": job(index)["job_title"],
            "boss_company_name": "",
            "company_page_url": None,
            "failure": {"code": "selector_missing", "message": "未找到详情"} if failed else None,
        },
        "company": {"status": "not_applicable"},
    }


def detail_semantic() -> dict:
    return {
        "summary": {
            "core_responsibilities": ["分析客户需求并设计AI解决方案"],
            "hard_requirements": ["本科及以上学历"],
            "key_capability_and_tool_requirements": ["客户沟通和技术协调"],
            "work_style_and_risks": ["需要跨团队推进POC交付"],
            "missing_or_uncertain": ["未说明编码占比"],
        },
        "evidence": [
            {"category": "responsibility", "text": "负责客户需求分析，设计AI解决方案并协调研发完成POC交付。"},
            {"category": "requirement", "text": "要求本科及以上学历。"},
        ],
    }


class S3StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        data_dir = self.run_root / "job-research-data"
        config_input = config_payload()
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
        upsert_s2(str(self.run_root), {"browser": detail_browser(1), "semantic": detail_semantic()})
        upsert_s2(str(self.run_root), {"browser": detail_browser(2, failed=True), "semantic": None})
        details = load_json(data_dir / "job-details.json")
        detail = next(record for record in details["records"] if record.get("job_key") == job(1)["job_key"])
        self.evidence_id = detail["evidence"][0]["evidence_id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, **changes: object) -> dict:
        value = {
            "job_key": job(1)["job_key"],
            "status": "初筛通过",
            "reason": "存在客户需求分析、AI方案设计和POC交付职责",
            "evidence_ids": [self.evidence_id],
            "items_to_verify": ["编码工作占比"],
            "reporting": {
                "primary_direction": "解决方案",
                "other_directions": ["产品与业务"],
            },
        }
        value.update(changes)
        return value

    def test_writes_pass_with_program_fields(self) -> None:
        result = upsert(str(self.run_root), self.payload())
        self.assertEqual(1, result["screened_jobs"])
        document = load_json(self.run_root / "job-research-data" / "screening-results.json")
        record = document["records"][0]
        self.assertEqual("建议复核", record["review_level"])
        self.assertEqual("广州", record["reporting"]["report_city"])
        self.assertEqual(1, record["revision"])
        self.assertEqual(64, len(record["detail_record_hash"]))

    def test_reject_requires_direct_evidence(self) -> None:
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), self.payload(status="淘汰", evidence_ids=[]))

    def test_rejects_foreign_evidence_id(self) -> None:
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), self.payload(evidence_ids=["not-this-job"]))

    def test_rejects_direction_outside_enum(self) -> None:
        value = self.payload()
        value["reporting"]["primary_direction"] = "纯销售"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_model_confidence(self) -> None:
        value = self.payload()
        value["confidence"] = 0.9
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_failed_detail_can_only_be_unable_to_decide(self) -> None:
        upsert(str(self.run_root), self.payload())
        value = self.payload(job_key=job(2)["job_key"])
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)
        value.update({
            "status": "无法判断",
            "evidence_ids": [],
            "items_to_verify": ["重新读取岗位详情"],
            "reporting": {"primary_direction": None, "other_directions": []},
        })
        result = upsert(str(self.run_root), value)
        self.assertEqual(1, result["status_counts"]["无法判断"])

    def test_possible_without_evidence_requires_review(self) -> None:
        value = self.payload(
            status="可能无关",
            evidence_ids=[],
            reporting={"primary_direction": None, "other_directions": []},
        )
        upsert(str(self.run_root), value)
        document = load_json(self.run_root / "job-research-data" / "screening-results.json")
        self.assertEqual("必须复核", document["records"][0]["review_level"])

    def test_returns_next_unprocessed_detail(self) -> None:
        self.assertEqual(job(1)["job_key"], pending(str(self.run_root), 1)["pending"][0]["job_key"])
        upsert(str(self.run_root), self.payload())
        self.assertEqual(job(2)["job_key"], status(str(self.run_root))["next_job_key"])

    def test_pending_uses_s0_config_as_screening_context(self) -> None:
        result = pending(str(self.run_root), 1)
        context = result["screening_context"]
        self.assertEqual("personal", context["candidate_profile"]["basis"])
        self.assertEqual("参与需求分析和跨团队协作", context["target_work_features"][0]["feature"])
        self.assertEqual(["产品与业务", "解决方案"], [item["name"] for item in context["target_directions"]])

    def test_rejects_changed_job_details(self) -> None:
        upsert(str(self.run_root), self.payload())
        path = self.run_root / "job-research-data" / "job-details.json"
        details = load_json(path)
        changed = copy.deepcopy(details)
        changed["records"][0]["summary"]["missing_or_uncertain"] = ["发生变化"]
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            status(str(self.run_root))

    def test_s2_must_be_complete(self) -> None:
        path = self.run_root / "job-research-data" / "job-details.json"
        details = load_json(path)
        details["records"] = [
            record for record in details["records"]
            if record.get("job_key") != job(2)["job_key"]
        ]
        atomic_write_json(path, details)
        with self.assertRaises(S1Error):
            pending(str(self.run_root), 1)

    def test_changed_s0_config_invalidates_results(self) -> None:
        upsert(str(self.run_root), self.payload())
        path = self.run_root / "job-research-data" / "config.json"
        changed = load_json(path)
        changed.pop("config_hash")
        changed["job_target"]["hard_exclusions"][0]["rule"] = "主要职责不符合当前目标"
        atomic_write_json(path, normalize_config(changed))
        with self.assertRaises(S1Error):
            status(str(self.run_root))

    def test_rejects_out_of_order_submission(self) -> None:
        value = self.payload(job_key=job(2)["job_key"])
        value.update({
            "status": "无法判断",
            "evidence_ids": [],
            "items_to_verify": ["重新读取岗位详情"],
            "reporting": {"primary_direction": None, "other_directions": []},
        })
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)


if __name__ == "__main__":
    unittest.main()
