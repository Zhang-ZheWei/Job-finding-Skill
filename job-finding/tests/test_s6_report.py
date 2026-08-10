from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(TEST_DIR))

from s1_common import S1Error, atomic_write_json, load_json  # noqa: E402
from s5_store import upsert as upsert_s5  # noqa: E402
from s6_report import (  # noqa: E402
    MAIN_HEADER,
    REPORT_RELATIVE_PATH,
    _anchor,
    _escape,
    _safe_https_url,
    build,
    validate,
)
import test_s5_store as s5_fixture  # noqa: E402


class S6ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = s5_fixture.S5StoreTests(methodName="test_scores_and_redistributes_unknown_weight_with_coverage")
        self.fixture.setUp()
        self.run_root = self.fixture.run_root
        upsert_s5(str(self.run_root), self.fixture.scoring_payload())
        low = self.fixture.scoring_payload(unscorable_job=set(), unscorable_company=set())
        for item in low["job_match"]:
            item["matched_anchor"] = "mismatch"
        for item in low["company_evaluation"]:
            item["matched_anchor"] = "major_concern" if item["dimension_id"] == "public_risk" else "mismatch"
        upsert_s5(str(self.run_root), low)

    def tearDown(self) -> None:
        self.fixture.tearDown()

    @property
    def report_path(self) -> Path:
        return self.run_root / REPORT_RELATIVE_PATH

    def test_build_creates_one_report_and_manifest(self) -> None:
        result = build(str(self.run_root))
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["counts"]["scored_jobs"])
        self.assertEqual(1, result["counts"]["unscored_jobs"])
        self.assertEqual([self.report_path], list((self.run_root / "result").glob("*.md")))
        self.assertTrue((self.run_root / "job-research-data" / "report-manifest.json").is_file())

    def test_main_table_uses_confirmed_field_order_and_city_subheading(self) -> None:
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        self.assertIn("## 2. 岗位决策总表\n\n### 示例城市", report)
        self.assertIn(MAIN_HEADER, report)

    def test_score_links_reach_job_and_company_sections(self) -> None:
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        scores = load_json(self.run_root / "job-research-data" / "job-scores.json")["records"]
        for score in scores:
            job_anchor = _anchor("job", score["job_key"])
            company_anchor = _anchor("company", score["company_key"])
            self.assertEqual(1, report.count(f'<a id="{job_anchor}"></a>'))
            self.assertEqual(1, report.count(f"](#{job_anchor})"))
            self.assertIn(f"](#{company_anchor})", report)

    def test_same_company_has_one_card_and_multiple_company_score_links(self) -> None:
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        scores = load_json(self.run_root / "job-research-data" / "job-scores.json")["records"]
        self.assertEqual(scores[0]["company_key"], scores[1]["company_key"])
        anchor = _anchor("company", scores[0]["company_key"])
        self.assertEqual(1, report.count(f'<a id="{anchor}"></a>'))
        self.assertEqual(2, report.count(f"](#{anchor})"))

    def test_all_scores_are_kept_including_zero(self) -> None:
        data = load_json(self.run_root / "job-research-data" / "job-scores.json")
        second_key = data["records"][1]["job_key"]
        self.assertEqual(2, len(data["records"]))
        self.assertEqual(0.0, data["records"][1]["overall"]["score"])
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        index = {item["job_key"]: item for item in load_json(self.run_root / "job-research-data" / "job-index.json")["records"]}
        self.assertIn(index[second_key]["job_title"], report)
        self.assertNotIn("有效岗位", report)
        self.assertNotIn("入选分数线", report)

    def test_unscorable_overall_is_kept(self) -> None:
        fixture = s5_fixture.S5StoreTests(methodName="test_scores_and_redistributes_unknown_weight_with_coverage")
        fixture.setUp()
        try:
            all_company = {
                "business_and_industry_fit", "scale_and_stability", "employee_experience_and_culture",
                "company_growth_platform", "public_risk",
            }
            upsert_s5(
                str(fixture.run_root),
                fixture.scoring_payload(unscorable_job=set(), unscorable_company=all_company),
            )
            upsert_s5(str(fixture.run_root), fixture.scoring_payload())
            result = build(str(fixture.run_root))
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertEqual(2, result["counts"]["scored_jobs"])
            self.assertIn("不可评分", report)
        finally:
            fixture.tearDown()

    def test_company_identity_skip_is_in_compact_final_section(self) -> None:
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        screening = load_json(self.run_root / "job-research-data" / "screening-results.json")["records"][-1]
        job = next(
            item for item in load_json(self.run_root / "job-research-data" / "job-index.json")["records"]
            if item["job_key"] == screening["job_key"]
        )
        section = report.split("## 5. 未进入评分的岗位", 1)[1]
        self.assertIn(job["job_title"], section)
        self.assertIn("企业主体不可用，已跳过后续流程", section)
        self.assertIn("BOSS 公司页未取得可信企业名称", section)

    def test_summary_uses_existing_score_reasons(self) -> None:
        build(str(self.run_root))
        report = self.report_path.read_text(encoding="utf-8")
        self.assertIn("岗位职责与目标方向匹配", report)
        self.assertIn("存在明显不足", report)
        main_row = next(line for line in report.splitlines() if line.startswith("| 1 |"))
        self.assertNotIn("岗位匹配分8", main_row)
        self.assertNotIn("公司评价分", main_row)
        self.assertNotIn("当前结构化证据支持该档位", main_row)

    def test_repeated_build_is_idempotent(self) -> None:
        first = build(str(self.run_root))
        first_bytes = self.report_path.read_bytes()
        first_manifest = (self.run_root / "job-research-data" / "report-manifest.json").read_bytes()
        second = build(str(self.run_root))
        self.assertEqual(first["report_sha256"], second["report_sha256"])
        self.assertEqual(first_bytes, self.report_path.read_bytes())
        self.assertEqual(first_manifest, (self.run_root / "job-research-data" / "report-manifest.json").read_bytes())

    def test_validate_detects_report_change(self) -> None:
        build(str(self.run_root))
        changed = self.report_path.read_text(encoding="utf-8") + "额外内容\n"
        self.report_path.write_text(changed, encoding="utf-8")
        with self.assertRaises(S1Error):
            validate(str(self.run_root))

    def test_upstream_change_blocks_reuse_and_preserves_old_report(self) -> None:
        build(str(self.run_root))
        before = self.report_path.read_bytes()
        path = self.run_root / "job-research-data" / "company-research.json"
        changed = copy.deepcopy(load_json(path))
        changed["records"][0]["enterprise_name"] = "变化后的企业名称"
        atomic_write_json(path, changed)
        with self.assertRaises(S1Error):
            build(str(self.run_root))
        self.assertEqual(before, self.report_path.read_bytes())

    def test_unsafe_markdown_and_non_https_url_are_rejected_or_escaped(self) -> None:
        self.assertEqual(r"公司\|名称\[测试\]", _escape("公司|名称[测试]"))
        with self.assertRaises(S1Error):
            _safe_https_url("http://example.com")
        with self.assertRaises(S1Error):
            _safe_https_url("https://user:secret@example.com/path")


if __name__ == "__main__":
    unittest.main()
