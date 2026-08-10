from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, load_json  # noqa: E402
from s1_store import (  # noqa: E402
    build_documents,
    merge_run_documents,
    next_combination,
    validate_run_documents,
    write_documents,
)
from task_config import prepare  # noqa: E402
from test_task_config import payload as config_payload  # noqa: E402
from s2_store import pending as pending_s2, upsert as upsert_s2  # noqa: E402
from s3_store import pending as pending_s3, upsert as upsert_s3  # noqa: E402
from s4_store import pending as pending_s4  # noqa: E402


URL = "https://www.zhipin.com/web/geek/jobs?city=101280100&jobType=1901&query=%E7%AE%A1%E5%9F%B9%E7%94%9F"


def scroll_trace(initial: int, final: int, *, no_new_rounds: int = 0) -> list[dict]:
    values = []
    round_number = 1
    if final > initial:
        values.append({
            "round": round_number,
            "before_unique_jobs": initial,
            "after_unique_jobs": final,
            "added_unique_jobs": final - initial,
            "before_scroll_y": 0,
            "after_scroll_y": 2000,
            "before_scroll_height": 2400,
            "after_scroll_height": 4400,
            "recovery_nudge": False,
            "effective": True,
        })
        round_number += 1
    for _ in range(no_new_rounds):
        values.append({
            "round": round_number,
            "before_unique_jobs": final,
            "after_unique_jobs": final,
            "added_unique_jobs": 0,
            "before_scroll_y": 3600,
            "after_scroll_y": 3600,
            "before_scroll_height": 4400,
            "after_scroll_height": 4400,
            "recovery_nudge": True,
            "effective": True,
        })
        round_number += 1
    return values


def payload(count: int = 20) -> dict:
    cards = []
    for index in range(count):
        job_id = f"job-{index:02d}"
        cards.append({
            "job_id": job_id,
            "boss_job_url": f"https://www.zhipin.com/job_detail/{job_id}.html?ka=search_list_jname_{index}",
            "job_title": f"岗位{index}",
            "brand_company_name": f"公司{index}",
            "salary": "10-20K",
            "tags": ["1-3年", "本科"],
            "card_city": "广州",
            "posted_at": "",
        })
    return {
        "search_url": URL,
        "city": "广州",
        "term": "管培生",
        "limit": 20,
        "initial_visible_count": 15,
        "scroll_rounds": 1,
        "manual_scroll_rounds": 0,
        "automated_scroll_rounds": 1,
        "successful_refresh_rounds": 1,
        "end_marker_seen": False,
        "scroll_trace": scroll_trace(15, count),
        "unique_jobs_after_scroll": count,
        "stop_reason": "sample_limit_reached",
        "cards": cards,
    }


def combination_payload(combo: dict, job_ids: list[str]) -> dict:
    cards = []
    for index, job_id in enumerate(job_ids):
        cards.append({
            "job_id": job_id,
            "boss_job_url": f"https://www.zhipin.com/job_detail/{job_id}.html",
            "job_title": f"组合岗位{job_id}",
            "brand_company_name": f"组合公司{index}",
            "salary": "12-22K",
            "tags": ["经验不限", "本科"],
            "card_city": combo["city_label"],
            "posted_at": "",
        })
    exhaustive = combo["collection_mode"] == "exhaustive"
    initial = min(len(job_ids), 15)
    trace = scroll_trace(initial, len(job_ids), no_new_rounds=10)
    return {
        "search_url": combo["search_url"],
        "city": combo["city_label"],
        "term": combo["term"],
        "collection_mode": "exhaustive" if exhaustive else "bounded_sample",
        "limit": len(job_ids),
        "initial_visible_count": initial,
        "scroll_rounds": 0 if not exhaustive else len(trace),
        "manual_scroll_rounds": 0,
        "automated_scroll_rounds": 0 if not exhaustive else len(trace),
        "successful_refresh_rounds": 1 if len(job_ids) > initial else 0,
        "end_marker_seen": len(job_ids) < 15,
        "scroll_trace": [] if not exhaustive else trace,
        "consecutive_no_new_rounds": 10 if exhaustive else 0,
        "unique_jobs_after_scroll": len(job_ids),
        "stop_reason": "natural_exhaustion" if exhaustive else "sample_limit_reached",
        "cards": cards,
    }


def complete_downstream(run_root: str, screening_statuses: list[str]) -> None:
    company_url = "https://www.zhipin.com/gongsi/batch-company.html"
    while True:
        current = pending_s2(run_root, 1)["pending"]
        if not current:
            break
        item = current[0]
        browser = {
            "job_key": item["job_key"],
            "job_id": item["job_id"],
            "requested_url": item["boss_job_url"],
            "detail": {
                "status": "ok",
                "final_url": item["boss_job_url"],
                "selector": ".job-detail-section",
                "jd_text": "负责AI解决方案设计与客户沟通",
                "page_job_title": item["job_title"],
                "boss_company_name": "批次公司",
                "company_page_url": company_url,
                "failure": None,
            },
            "company": {
                "status": "acquired",
                "company_page_url": company_url,
                "source_selector": ".job-sec.company-business",
                "fields": {
                    "企业名称": "批次测试科技有限公司",
                    "统一社会信用代码": "91310109695753999F",
                    "法定代表人": "测试人",
                    "成立时间": "2020-01-01",
                    "注册资本": "1000万人民币",
                    "注册地址": "广州市测试路1号",
                },
            },
        }
        semantic = {
            "summary": {
                "core_responsibilities": ["负责AI解决方案设计与客户沟通"],
                "hard_requirements": [],
                "key_capability_and_tool_requirements": ["客户沟通"],
                "work_style_and_risks": [],
                "missing_or_uncertain": [],
            },
            "evidence": [{"category": "responsibility", "text": "负责AI解决方案设计与客户沟通"}],
        }
        upsert_s2(run_root, {"browser": browser, "semantic": semantic})

    index = 0
    while True:
        current = pending_s3(run_root, 1)["pending"]
        if not current:
            break
        item = current[0]
        chosen = screening_statuses[index]
        index += 1
        upsert_s3(run_root, {
            "job_key": item["job_key"],
            "status": chosen,
            "reason": "测试当前批次筛选结果",
            "evidence_ids": [item["evidence"][0]["evidence_id"]],
            "items_to_verify": [],
            "reporting": {
                "primary_direction": "产品与业务" if chosen == "初筛通过" else None,
                "other_directions": [],
            },
        })


class S1StoreTests(unittest.TestCase):
    def test_builds_exactly_twenty_jobs_after_scroll(self) -> None:
        job_index, checkpoint = build_documents(payload())
        self.assertEqual(20, len(job_index["records"]))
        self.assertEqual(1, checkpoint["combinations"][0]["evidence"]["scroll_rounds"])
        self.assertEqual("sample_complete", checkpoint["combinations"][0]["status"])

    def test_deduplicates_before_applying_limit(self) -> None:
        value = payload(21)
        value["cards"].insert(5, copy.deepcopy(value["cards"][0]))
        job_index, _ = build_documents(value)
        self.assertEqual(20, len(job_index["records"]))
        self.assertEqual(20, len({record["job_id"] for record in job_index["records"]}))

    def test_rejects_identity_conflict(self) -> None:
        value = payload()
        value["cards"][0]["job_id"] = "different-id"
        with self.assertRaises(S1Error):
            build_documents(value)

    def test_rejects_no_scroll_proof(self) -> None:
        value = payload()
        value["initial_visible_count"] = 20
        value["scroll_rounds"] = 0
        with self.assertRaises(S1Error):
            build_documents(value)

    def test_writes_only_two_authoritative_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = write_documents(directory, payload())
            self.assertTrue(result["ok"])
            files = sorted(path.name for path in (Path(directory) / "job-research-data").iterdir())
            self.assertEqual(["checkpoint.json", "job-index.json"], files)
            self.assertEqual(20, len(load_json(Path(directory) / "job-research-data" / "job-index.json")["records"]))

    def test_accepts_exhaustive_proof(self) -> None:
        value = payload()
        value["collection_mode"] = "exhaustive"
        value["stop_reason"] = "natural_exhaustion"
        value["consecutive_no_new_rounds"] = 10
        value["scroll_trace"] = scroll_trace(15, 20, no_new_rounds=10)
        value["automated_scroll_rounds"] = len(value["scroll_trace"])
        value["scroll_rounds"] = len(value["scroll_trace"])
        job_index, checkpoint = build_documents(value)
        self.assertEqual("exhaustive", job_index["collection_mode"])
        self.assertEqual("completed", checkpoint["combinations"][0]["status"])

    def test_rejects_short_exhaustive_proof(self) -> None:
        value = payload()
        value["collection_mode"] = "exhaustive"
        value["stop_reason"] = "natural_exhaustion"
        value["consecutive_no_new_rounds"] = 9
        with self.assertRaises(S1Error):
            build_documents(value)

    def test_rejects_fifteen_jobs_without_refresh_or_end_marker(self) -> None:
        value = payload(15)
        value["collection_mode"] = "exhaustive"
        value["stop_reason"] = "natural_exhaustion"
        value["consecutive_no_new_rounds"] = 10
        value["successful_refresh_rounds"] = 0
        value["scroll_trace"] = scroll_trace(15, 15, no_new_rounds=10)
        value["automated_scroll_rounds"] = len(value["scroll_trace"])
        value["scroll_rounds"] = len(value["scroll_trace"])
        with self.assertRaises(S1Error) as captured:
            build_documents(value)
        self.assertEqual("scroll_not_proven", captured.exception.code)

    def test_merges_two_config_combinations_and_global_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory, config_payload())
            first = next_combination(directory)["next_combo"]
            result = merge_run_documents(directory, combination_payload(first, ["shared-job", "first-job"]))
            self.assertEqual(1, result["pending_combinations"])
            second = next_combination(directory)["next_combo"]
            result = merge_run_documents(directory, combination_payload(second, ["shared-job", "second-job"]))
            self.assertEqual(3, result["records"])
            self.assertEqual(0, result["pending_combinations"])
            document = load_json(Path(directory) / "job-research-data" / "job-index.json")
            shared = next(record for record in document["records"] if record["job_id"] == "shared-job")
            self.assertEqual(2, len(shared["source_combinations"]))
            self.assertTrue(validate_run_documents(directory)["ok"])

    def test_target_mode_skips_remaining_city_combinations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_payload()
            config["search_scope"]["search_mode"] = "per_city_target"
            config["search_scope"]["per_city_target_count"] = 2
            prepare(directory, config)
            first = next_combination(directory)["next_combo"]
            self.assertEqual("exhaustive", first["collection_mode"])
            self.assertIsNone(first["limit"])
            result = merge_run_documents(directory, combination_payload(first, ["target-one", "target-two"]))
            self.assertEqual("awaiting_s2", next_combination(directory)["workflow_state"])
            complete_downstream(directory, ["初筛通过", "初筛通过"])
            result = next_combination(directory)
            self.assertEqual(1, result["completed_combinations"])
            self.assertEqual(1, result["skipped_combinations"])
            self.assertEqual("s1_complete", result["workflow_state"])
            self.assertIsNone(result["next_combo"])

    def test_target_mode_continues_after_screening_pass_count_is_below_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = config_payload()
            config["search_scope"]["search_mode"] = "per_city_target"
            config["search_scope"]["per_city_target_count"] = 2
            prepare(directory, config)
            first = next_combination(directory)["next_combo"]
            merge_run_documents(directory, combination_payload(first, ["batch-one-a", "batch-one-b"]))
            complete_downstream(directory, ["初筛通过", "淘汰"])
            with self.assertRaises(S1Error):
                pending_s4(directory, 1)
            second = next_combination(directory)["next_combo"]
            self.assertIsNotNone(second)
            self.assertNotEqual(first["combo_key"], second["combo_key"])
            merge_run_documents(directory, combination_payload(second, ["batch-two-a"]))
            complete_downstream(directory, ["初筛通过"])
            result = next_combination(directory)
            self.assertEqual(2, result["completed_combinations"])
            self.assertEqual("s1_complete", result["workflow_state"])

    def test_rejects_result_for_non_next_combination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory, config_payload())
            first = next_combination(directory)["next_combo"]
            wrong = copy.deepcopy(first)
            wrong["search_url"] = wrong["search_url"].replace(
                "%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86",
                "%E8%A7%A3%E5%86%B3%E6%96%B9%E6%A1%88%E9%A1%BE%E9%97%AE",
            )
            wrong["term"] = "解决方案顾问"
            with self.assertRaises(S1Error):
                merge_run_documents(directory, combination_payload(wrong, ["wrong-job"]))


if __name__ == "__main__":
    unittest.main()
