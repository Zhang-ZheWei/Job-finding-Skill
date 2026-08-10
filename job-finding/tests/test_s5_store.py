from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from s2_store import upsert as upsert_s2  # noqa: E402
from s3_store import upsert as upsert_s3  # noqa: E402
from s4_store import pending as pending_s4, upsert as upsert_s4  # noqa: E402
from s5_store import _round, pending, status, upsert  # noqa: E402
from task_config import generate_search_plan, prepare  # noqa: E402
import test_s4_store as s4_fixture  # noqa: E402
from test_task_config import payload as config_payload  # noqa: E402


class S5StoreTests(unittest.TestCase):
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
            record = s4_fixture.job(index)
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
        upsert_s2(str(self.run_root), {"browser": s4_fixture.browser(1, "acquired"), "semantic": s4_fixture.semantic()})
        upsert_s2(str(self.run_root), {"browser": s4_fixture.browser(2, "reused"), "semantic": s4_fixture.semantic()})
        upsert_s2(str(self.run_root), {"browser": s4_fixture.browser(3, "not_found"), "semantic": s4_fixture.semantic()})
        details = load_json(data_dir / "job-details.json")
        evidence = {
            record["job_key"]: record["evidence"][0]["evidence_id"]
            for record in details["records"] if record.get("record_type") == "job_detail"
        }
        for index in (1, 2):
            upsert_s3(str(self.run_root), {
                "job_key": s4_fixture.job(index)["job_key"],
                "status": "初筛通过",
                "reason": "岗位存在客户需求分析和解决方案职责",
                "evidence_ids": [evidence[s4_fixture.job(index)["job_key"]]],
                "items_to_verify": [],
                "reporting": {"primary_direction": "解决方案", "other_directions": []},
            })
        upsert_s3(str(self.run_root), {
            "job_key": s4_fixture.job(3)["job_key"],
            "status": "初筛通过",
            "reason": "岗位存在客户需求分析和解决方案职责",
            "evidence_ids": [evidence[s4_fixture.job(3)["job_key"]]],
            "items_to_verify": [],
            "reporting": {"primary_direction": "解决方案", "other_directions": []},
        })
        task = pending_s4(str(self.run_root), 1)["pending"][0]
        holder = SimpleNamespace(task=task, company_key=task["company_key"])
        upsert_s4(str(self.run_root), s4_fixture.S4StoreTests.payload(holder, all_complete=True))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def scoring_payload(self, *, unscorable_job: set[str] | None = None, unscorable_company: set[str] | None = None) -> dict:
        if unscorable_job is None:
            unscorable_job = {"role_growth_fit"}
        if unscorable_company is None:
            unscorable_company = set()
        result = pending(str(self.run_root), 1)
        task = result["pending"][0]
        criteria = {
            item["dimension_id"]: item["criteria"][0]["criterion_id"]
            for group in result["scoring_context"].values() for item in group
        }
        job_evidence = task["job"]["evidence"][0]["evidence_id"]
        company_evidence: dict[str, list[str]] = {}
        for item in task["company"]["evidence"]:
            company_evidence.setdefault(item["group"], []).append(item["evidence_id"])

        job_anchors = {
            "target_and_responsibility_fit": "strong_match",
            "strength_utilization": "match",
            "work_style_fit": "partial_match",
            "role_growth_fit": "weak_match",
        }
        company_anchors = {
            "business_and_industry_fit": "strong_match",
            "scale_and_stability": "match",
            "employee_experience_and_culture": "partial_match",
            "company_growth_platform": "partial_match",
            "public_risk": "no_material_concern",
        }
        company_groups = {
            "business_and_industry_fit": "basic_profile",
            "scale_and_stability": "basic_profile",
            "employee_experience_and_culture": "employee_reviews",
            "company_growth_platform": "employee_reviews",
            "public_risk": "public_risks",
        }

        def dimension(dimension_id: str, anchor: str, evidence_ids: list[str], unscorable: bool) -> dict:
            return {
                "dimension_id": dimension_id,
                "status": "不可评分" if unscorable else "可评分",
                "matched_anchor": None if unscorable else anchor,
                "criterion_ids": [criteria[dimension_id]],
                "evidence_ids": [] if unscorable else evidence_ids,
                "reason": "现有信息不足" if unscorable else "当前结构化证据支持该档位",
            }

        return {
            "job_key": task["job_key"],
            "job_match": [
                dimension(dimension_id, anchor, [job_evidence], dimension_id in unscorable_job)
                for dimension_id, anchor in job_anchors.items()
            ],
            "company_evaluation": [
                dimension(
                    dimension_id,
                    anchor,
                    [company_evidence[company_groups[dimension_id]][0]],
                    dimension_id in unscorable_company,
                )
                for dimension_id, anchor in company_anchors.items()
            ],
        }

    def test_pending_exposes_dynamic_criteria_without_hard_exclusions(self) -> None:
        result = pending(str(self.run_root), 10)
        self.assertEqual(2, result["remaining"])
        self.assertEqual(
            [s4_fixture.job(1)["job_key"], s4_fixture.job(2)["job_key"]],
            [item["job_key"] for item in result["pending"]],
        )
        paths = [
            criterion["source_path"]
            for group in result["scoring_context"].values()
            for dimension in group for criterion in dimension["criteria"]
        ]
        self.assertFalse(any("hard_exclusions" in path for path in paths))
        self.assertTrue(any("candidate_profile.capabilities" in path for path in paths))

    def test_scores_and_redistributes_unknown_weight_with_coverage(self) -> None:
        result = upsert(str(self.run_root), self.scoring_payload())
        self.assertEqual(1, result["scored_jobs"])
        document = load_json(self.run_root / "job-research-data" / "job-scores.json")
        record = document["records"][0]
        self.assertEqual(8.2, record["job_match"]["score"])
        self.assertEqual(0.9, record["job_match"]["evidence_coverage"])
        self.assertEqual(8.0, record["overall"]["score"])
        self.assertEqual("匹配", record["overall"]["rating"])
        growth = record["job_match"]["dimensions"][-1]
        self.assertEqual(0.0, growth["effective_weight"])
        self.assertIsNone(growth["dimension_score"])
        self.assertNotIn("review_level", record)
        self.assertNotIn("is_effective", record["overall"])

    def test_all_jobs_are_preserved_without_score_threshold(self) -> None:
        upsert(str(self.run_root), self.scoring_payload())
        value = self.scoring_payload(unscorable_job=set(), unscorable_company=set())
        for item in value["job_match"]:
            item["matched_anchor"] = "mismatch"
        for item in value["company_evaluation"]:
            item["matched_anchor"] = "major_concern" if item["dimension_id"] == "public_risk" else "mismatch"
        result = upsert(str(self.run_root), value)
        self.assertEqual(2, result["scored_jobs"])
        self.assertEqual(0, result["pending_jobs"])
        records = load_json(self.run_root / "job-research-data" / "job-scores.json")["records"]
        self.assertEqual(0.0, records[1]["overall"]["score"])
        self.assertEqual("不推荐", records[1]["overall"]["rating"])

    def test_entire_company_group_unscorable_keeps_job_without_cross_group_transfer(self) -> None:
        all_company = {
            "business_and_industry_fit", "scale_and_stability", "employee_experience_and_culture",
            "company_growth_platform", "public_risk",
        }
        upsert(str(self.run_root), self.scoring_payload(unscorable_job=set(), unscorable_company=all_company))
        record = load_json(self.run_root / "job-research-data" / "job-scores.json")["records"][0]
        self.assertEqual("不可评分", record["company_evaluation"]["status"])
        self.assertEqual("不可评分", record["overall"]["status"])
        self.assertIsNone(record["overall"]["score"])
        self.assertEqual(5, len(record["information_limitations"]))

    def test_rejects_illegal_anchor(self) -> None:
        value = self.scoring_payload()
        value["job_match"][0]["matched_anchor"] = "no_material_concern"
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_no_material_concern_requires_completed_empty_risk_group(self) -> None:
        path = self.run_root / "job-research-data" / "company-research.json"
        changed = load_json(path)
        changed["records"][0]["public_risks"]["items"] = [{
            "category": "regulatory_measure",
            "summary": "存在监管措施",
            "evidence_ids": changed["records"][0]["public_risks"]["source_evidence_ids"][:1],
        }]
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), self.scoring_payload())

    def test_rejects_cross_group_evidence(self) -> None:
        value = self.scoring_payload()
        task = pending(str(self.run_root), 1)["pending"][0]
        company_id = task["company"]["evidence"][0]["evidence_id"]
        value["job_match"][0]["evidence_ids"] = [company_id]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_model_numeric_fields(self) -> None:
        value = self.scoring_payload()
        value["job_match"][0]["score"] = 10
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_rejects_dimension_reordering(self) -> None:
        value = self.scoring_payload()
        value["job_match"][0], value["job_match"][1] = value["job_match"][1], value["job_match"][0]
        with self.assertRaises(S1Error):
            upsert(str(self.run_root), value)

    def test_upstream_change_invalidates_scores(self) -> None:
        upsert(str(self.run_root), self.scoring_payload())
        path = self.run_root / "job-research-data" / "company-research.json"
        changed = copy.deepcopy(load_json(path))
        changed["records"][0]["enterprise_name"] = "变更后的企业名称"
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            status(str(self.run_root))

    def test_decimal_round_half_up(self) -> None:
        self.assertEqual(8.0, _round(Decimal("7.95"), "0.1"))


if __name__ == "__main__":
    unittest.main()
