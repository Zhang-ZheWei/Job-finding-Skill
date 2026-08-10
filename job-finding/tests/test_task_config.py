from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from task_config import build_search_url, generate_search_plan, inspect_search_url, prepare, validate_run  # noqa: E402
from task_manager import create_task  # noqa: E402


URL = "https://www.zhipin.com/web/geek/jobs?city=101010100&degree=203&query=%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86"
RESUME_REFERENCE = str(Path(__file__).resolve())


def payload() -> dict:
    return {
        "schema_version": 2,
        "information_sources": {
            "resume_status": "provided",
            "items": [
                {
                    "source_id": "resume_1",
                    "source_type": "resume",
                    "reference": RESUME_REFERENCE,
                },
                {
                    "source_id": "user_job_target",
                    "source_type": "user_statement",
                    "reference": "intake:job_target",
                },
            ],
        },
        "candidate_profile": {
            "basis": "personal",
            "education": [
                {
                    "institution": "示例大学",
                    "institution_attributes": [],
                    "degree_level": "本科",
                    "major": "信息管理",
                    "start_date": "2020-09",
                    "end_date": "2024-06",
                    "is_current": False,
                    "academic_highlights": [],
                    "source_ids": ["resume_1"],
                }
            ],
            "experiences": [
                {
                    "experience_type": "project",
                    "organization": "示例组织",
                    "name": "业务分析项目",
                    "role": "项目成员",
                    "start_date": "2023",
                    "end_date": "2024",
                    "is_current": False,
                    "domains": ["企业服务"],
                    "responsibilities": ["访谈用户并整理产品需求"],
                    "achievements": ["交付了结构化需求文档"],
                    "source_ids": ["resume_1"],
                }
            ],
            "capabilities": [
                {
                    "category": "product_business",
                    "name": "需求分析",
                    "evidence": ["在项目中完成用户访谈和需求文档"],
                    "source_ids": ["resume_1"],
                }
            ],
            "credentials": [
                {
                    "credential_type": "certificate",
                    "name": "示例资格证书",
                    "issuer": "示例机构",
                    "issue_date": "2024",
                    "details": None,
                    "source_ids": ["resume_1"],
                }
            ],
            "career_strengths": [
                {
                    "statement": "擅长跨团队沟通",
                    "evidence": ["协调项目成员完成交付"],
                    "source_ids": ["resume_1", "user_job_target"],
                }
            ],
            "eligibility_facts": [
                {
                    "fact_type": "毕业状态",
                    "value": "已毕业",
                    "source_ids": ["resume_1"],
                }
            ],
        },
        "job_target": {
            "target_directions": [
                {
                    "name": "产品与业务",
                    "description": "产品规划、需求管理或业务分析",
                    "positive_signals": ["负责需求调研或产品方案"],
                    "source_ids": ["user_job_target"],
                },
                {
                    "name": "解决方案",
                    "description": "客户需求和方案设计",
                    "positive_signals": ["分析客户需求并设计解决方案"],
                    "source_ids": ["user_job_target"],
                },
            ],
            "search_keywords": [
                {"term": "产品经理", "directions": ["产品与业务"]},
                {"term": "解决方案顾问", "directions": ["解决方案"]},
            ],
            "desired_work_features": [
                {
                    "scope": "responsibility",
                    "feature": "参与需求分析和跨团队协作",
                    "priority": "required",
                    "source_ids": ["user_job_target"],
                }
            ],
            "hard_exclusions": [
                {
                    "scope": "core_responsibility",
                    "rule": "主要工作与目标方向完全无关",
                    "source_ids": ["user_job_target"],
                }
            ],
            "soft_preferences": [
                {
                    "scope": "growth",
                    "preference": "优先考虑成长空间明确的岗位",
                    "source_ids": ["user_job_target"],
                }
            ],
        },
        "company_preferences": {
            "preferred_features": [
                {
                    "category": "growth",
                    "feature": "有明确的人才培养机制",
                    "source_ids": ["user_job_target"],
                }
            ],
            "disqualifying_conditions": [
                {
                    "category": "business_model",
                    "condition": "核心业务与用户确认方向完全无关",
                    "source_ids": ["user_job_target"],
                }
            ],
            "risk_concerns": [
                {
                    "category": "employment",
                    "concern": "重点关注公开劳动争议信息",
                    "source_ids": ["user_job_target"],
                }
            ],
        },
        "search_scope": {
            "search_mode": "exhaustive",
            "per_city_target_count": None,
            "search_urls": [{"city_label": "示例城市", "url": URL}],
        },
    }


def generic_payload() -> dict:
    value = payload()
    value["information_sources"] = {
        "resume_status": "declined",
        "items": [
            {
                "source_id": "user_job_target",
                "source_type": "user_statement",
                "reference": "intake:job_target",
            }
        ],
    }
    value["candidate_profile"] = {
        "basis": "generic",
        "education": [],
        "experiences": [],
        "capabilities": [],
        "credentials": [],
        "career_strengths": [],
        "eligibility_facts": [],
    }
    return value


class TaskConfigTests(unittest.TestCase):
    def test_cli_prepares_and_validates_bound_task(self) -> None:
        script = SCRIPT_DIR / "task_config.py"
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory)
            prepared = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "prepare",
                    "--run-root",
                    task["run_root"],
                    "--task-id",
                    task["task_id"],
                    "--input",
                    "-",
                ],
                input=json.dumps(payload(), ensure_ascii=False),
                check=True,
                capture_output=True,
                text=True,
            )
            validated = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "validate",
                    "--run-root",
                    task["run_root"],
                    "--task-id",
                    task["task_id"],
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(prepared.stdout)["config_hash"], json.loads(validated.stdout)["config_hash"])

    def test_binds_s0_config_to_timestamp_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory)
            result = prepare(task["run_root"], payload(), task["task_id"])
            identity = load_json(Path(task["run_root"]) / "job-research-data" / "task.json")
            self.assertEqual(result["config_hash"], identity["config_hash"])
            self.assertTrue(validate_run(task["run_root"], task["task_id"])["ok"])

    def test_rejects_task_b_id_before_writing_task_a_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_a = create_task(directory)
            task_b = create_task(directory)
            with self.assertRaises(S1Error) as caught:
                prepare(task_a["run_root"], payload(), task_b["task_id"])
            self.assertEqual("task_id_mismatch", caught.exception.code)
            self.assertFalse((Path(task_a["run_root"]) / "job-research-data" / "config.json").exists())

    def test_detects_task_and_config_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = create_task(directory)
            prepare(task["run_root"], payload(), task["task_id"])
            identity_path = Path(task["run_root"]) / "job-research-data" / "task.json"
            identity = load_json(identity_path)
            identity["config_hash"] = "f" * 64
            atomic_write_json(identity_path, identity)
            with self.assertRaises(S1Error) as caught:
                validate_run(task["run_root"], task["task_id"])
            self.assertEqual("task_config_mismatch", caught.exception.code)

    def test_inspects_url_and_replaces_existing_query(self) -> None:
        result = inspect_search_url(URL, "示例城市")
        self.assertEqual("101010100", result["city"])
        self.assertIn("query=", result["search_base"])
        self.assertTrue(build_search_url(result["search_base"], "解决方案顾问").endswith("query=%E8%A7%A3%E5%86%B3%E6%96%B9%E6%A1%88%E9%A1%BE%E9%97%AE"))

    def test_accepts_url_without_query(self) -> None:
        url = "https://www.zhipin.com/web/geek/jobs?city=101020100&degree=203"
        result = inspect_search_url(url, "另一城市")
        self.assertTrue(build_search_url(result["search_base"], "测试岗位").endswith("query=%E6%B5%8B%E8%AF%95%E5%B2%97%E4%BD%8D"))

    def test_writes_normalized_config_with_hashes_and_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = prepare(directory, payload())
            self.assertTrue(result["ok"])
            self.assertEqual(2, result["combination_count"])
            config = load_json(Path(directory) / "job-research-data" / "config.json")
            self.assertEqual(64, len(config["config_hash"]))
            self.assertEqual(64, len(config["information_sources"]["items"][0]["content_hash"]))
            self.assertEqual(0, config["search_scope"]["search_urls"][0]["order"])
            self.assertEqual(1, config["job_target"]["search_keywords"][1]["order"])
            self.assertEqual(1, config["job_target"]["target_directions"][1]["order"])
            self.assertTrue(validate_run(directory)["ok"])

    def test_generates_stable_city_keyword_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory, payload())
            config = load_json(Path(directory) / "job-research-data" / "config.json")
            plan = generate_search_plan(config)
            self.assertEqual(2, len(plan))
            self.assertEqual([0, 1], [item["keyword_order"] for item in plan])
            self.assertEqual("产品经理", plan[0]["term"])
            self.assertIn("query=%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86", plan[0]["search_url"])
            self.assertNotEqual(plan[0]["combo_key"], plan[1]["combo_key"])

    def test_same_config_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = prepare(directory, payload())
            second = prepare(directory, payload())
            self.assertEqual(first, second)

    def test_refuses_different_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepare(directory, payload())
            changed = payload()
            changed["job_target"]["search_keywords"][0]["term"] = "另一岗位"
            with self.assertRaises(S1Error):
                prepare(directory, changed)

    def test_rejects_keyword_direction_not_confirmed(self) -> None:
        value = payload()
        value["job_target"]["search_keywords"][0]["directions"] = ["不存在的方向"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_generic_screening_rejects_personal_facts(self) -> None:
        value = payload()
        value["candidate_profile"]["basis"] = "generic"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_personal_screening_requires_structured_fact(self) -> None:
        value = generic_payload()
        value["candidate_profile"]["basis"] = "personal"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_target_mode_requires_positive_count(self) -> None:
        value = copy.deepcopy(payload())
        value["search_scope"]["search_mode"] = "per_city_target"
        value["search_scope"]["per_city_target_count"] = 0
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_provided_resume_requires_resume_source(self) -> None:
        value = payload()
        value["information_sources"]["items"] = [value["information_sources"]["items"][1]]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_declined_resume_rejects_resume_source(self) -> None:
        value = payload()
        value["information_sources"]["resume_status"] = "declined"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_declined_resume_can_continue_with_generic_screening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(prepare(directory, generic_payload())["ok"])

    def test_rejects_unknown_source_reference(self) -> None:
        value = payload()
        value["candidate_profile"]["capabilities"][0]["source_ids"] = ["missing_source"]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_rejects_extra_candidate_field(self) -> None:
        value = payload()
        value["candidate_profile"]["contact_details"] = {"phone": "123"}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(S1Error):
                prepare(directory, value)

    def test_allows_non_technical_personal_profile(self) -> None:
        value = generic_payload()
        value["candidate_profile"]["basis"] = "personal"
        value["candidate_profile"]["experiences"] = [
            {
                "experience_type": "leadership",
                "organization": "示例协会",
                "name": "活动组织",
                "role": "负责人",
                "start_date": None,
                "end_date": None,
                "is_current": False,
                "domains": ["公益"],
                "responsibilities": ["组织志愿者完成活动"],
                "achievements": [],
                "source_ids": ["user_job_target"],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(prepare(directory, value)["ok"])

    def test_detects_changed_local_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resume = Path(directory) / "resume.txt"
            resume.write_text("版本一", encoding="utf-8")
            value = payload()
            value["information_sources"]["items"][0]["reference"] = str(resume)
            prepare(directory, value)
            resume.write_text("版本二", encoding="utf-8")
            with self.assertRaises(S1Error):
                validate_run(directory)


if __name__ == "__main__":
    unittest.main()
