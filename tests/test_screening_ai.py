"""Offline cost-boundary tests; never initialize a live model or submit a message."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock, patch

from tests import test_screening_pushplus as fixtures
from scripts.screening_ai import analyze, native_analysis, prepare, select_codes, send_report


class ScreeningAITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "screening_result.json"
        self.clock = {"calendar_status": "ok", "phase": "postmarket", "closed_session": True,
                      "expected_daily_date": "2026-10-21", "checked_at": "2026-10-21T17:17:00+08:00"}
        self.payload = fixtures.ScreeningPushplusTest.payload(SimpleNamespace(clock=self.clock))
        template = self.payload["strategies"][0]["picks"][0]
        codes = [f"600{i:03}" for i in range(1, 7)]
        self.payload["strategies"][0]["picks"] = [{**template, "code": code} for code in codes]
        self.payload["candidates"] = [{"code": code, "name": "测试", "price": 10, "signals": []} for code in codes]
        self.write()
        self.clock_patch = patch("scripts.screening_ai.market_clock", return_value=self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.env_patch = patch.dict("os.environ", {"GITHUB_RUN_ID": "42", "GITHUB_OUTPUT": str(self.path.parent / "output.txt")})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def write(self):
        self.path.write_text(json.dumps(self.payload), encoding="utf-8")

    def ready_plan(self):
        return prepare(self.path, api=lambda *_, **__: {"total_count": 0, "artifacts": []})

    def api(self, path, **kwargs):
        if path == "artifacts/123":
            plan = json.loads((self.path.parent / "ai_plan.json").read_text(encoding="utf-8"))
            return {"id": 123, "name": plan["budget_name"], "expired": False, "workflow_run": {"id": 42}}
        return {"total_count": 1, "artifacts": [{"id": 123}]}

    def test_selects_no_more_than_five_in_existing_order(self):
        self.assertEqual(select_codes(self.payload), ["600001", "600002", "600003", "600004", "600005"])
        plan = self.ready_plan()
        self.assertEqual(len(plan["codes"]), 5)
        self.assertEqual(plan["budget_day"], "2026-10-21")

    def test_sixth_stock_is_not_analyzed_but_appears_in_report(self):
        self.ready_plan()
        runner = Mock(side_effect=lambda codes: (codes, "# AI result"))
        self.assertEqual(analyze(self.path, "123", api=self.api, runner=runner), 0)
        self.assertEqual(len(runner.call_args.args[0]), 5)
        report = (self.path.parent / "ai_report.md").read_text(encoding="utf-8")
        self.assertIn("600006 测试：仅因子筛选，未做AI分析", report)
        self.assertIn("600005 测试：已完成AI分析", report)

    def test_native_entry_rejects_six_before_initializing_pipeline(self):
        with self.assertRaises(ValueError):
            native_analysis([f"600{i:03}" for i in range(1, 7)])

    def test_old_or_ineligible_artifact_never_queries_budget(self):
        original = json.loads(json.dumps(self.payload))
        for mutation in ('old', 'board', 'price', 'unknown_price', 'name'):
            self.payload = json.loads(json.dumps(original))
            if mutation == 'old':
                self.payload.pop('universe_policy')
            else:
                row = self.payload['candidates'][0]
                row.update({'board': {'code': '300496'}, 'price': {'price': 100},
                            'unknown_price': {'price': None}, 'name': {'name': '*ST测试'}}[mutation])
            self.write()
            api = Mock()
            plan = prepare(self.path, api=api)
            self.assertEqual(plan['status'], 'blocked')
            self.assertEqual(plan['reason'], 'blocked_universe_policy')
            api.assert_not_called()

    def test_scope_changed_after_reservation_never_calls_model(self):
        self.ready_plan()
        self.payload['candidates'][0]['price'] = 100
        self.write()
        runner, api = Mock(), Mock()
        with self.assertRaises(ValueError):
            analyze(self.path, '123', api=api, runner=runner)
        runner.assert_not_called()
        api.assert_not_called()

    def test_native_entry_rejects_non_mainboard_before_importing_model(self):
        with self.assertRaises(ValueError):
            native_analysis(['300496'])

    def test_empty_candidates_do_not_query_budget_or_model(self):
        self.payload["candidates"] = []
        self.write()
        api = Mock()
        plan = prepare(self.path, api=api)
        self.assertEqual(plan["status"], "skipped_no_candidates")
        api.assert_not_called()

    def test_existing_daily_reservation_skips_the_whole_batch(self):
        plan = prepare(self.path, api=lambda *_, **__: {"total_count": 1, "artifacts": [{"id": 9}]})
        self.assertEqual(plan["status"], "skipped_daily_budget")
        self.assertEqual(plan["codes"], [])

    def test_budget_api_error_is_not_treated_as_unused_budget(self):
        api = Mock(side_effect=TimeoutError("private response"))
        with self.assertRaises(TimeoutError):
            prepare(self.path, api=api)

    def test_bad_dates_block_before_budget_api(self):
        self.payload["ready_for_automation"] = False
        self.write()
        api = Mock()
        self.assertEqual(prepare(self.path, api=api)["status"], "blocked")
        api.assert_not_called()

    def test_model_runs_only_after_matching_exclusive_reservation(self):
        plan = self.ready_plan()
        runner = Mock(return_value=(plan["codes"], "# Fixture report"))
        self.assertEqual(analyze(self.path, "123", api=self.api, runner=runner), 0)
        runner.assert_called_once_with(plan["codes"])
        saved = json.loads((self.path.parent / "ai_result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "success")
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["llm_enabled"])

    def test_wrong_reservation_stops_before_model(self):
        self.ready_plan()
        runner = Mock()
        with self.assertRaises(RuntimeError):
            analyze(self.path, "123", api=lambda *_, **__: {"name": "wrong", "expired": False}, runner=runner)
        runner.assert_not_called()

    def test_nonexclusive_daily_budget_stops_before_model(self):
        self.ready_plan()
        runner = Mock()
        def api(path, **kwargs):
            if path == "artifacts":
                return {"total_count": 2, "artifacts": [{"id": 123}, {"id": 124}]}
            return self.api(path, **kwargs)
        with self.assertRaises(RuntimeError):
            analyze(self.path, "123", api=api, runner=runner)
        runner.assert_not_called()

    def test_model_failure_consumes_attempt_and_cannot_retry_same_result(self):
        self.ready_plan()
        runner = Mock(side_effect=RuntimeError("do-not-publish-secret"))
        self.assertEqual(analyze(self.path, "123", api=self.api, runner=runner), 1)
        self.assertEqual(analyze(self.path, "123", api=self.api, runner=runner), 1)
        runner.assert_called_once()
        self.assertNotIn("do-not-publish-secret", (self.path.parent / "ai_result.json").read_text(encoding="utf-8"))

    def test_expired_plan_never_calls_model(self):
        self.ready_plan()
        self.clock["expected_daily_date"] = "2026-10-22"
        runner = Mock()
        with self.assertRaises(RuntimeError):
            analyze(self.path, "123", api=self.api, runner=runner)
        runner.assert_not_called()

    def test_native_pipeline_disables_agent_market_context_and_all_notifications(self):
        module = ModuleType("src.core.pipeline")
        pipeline_class = Mock()
        module.StockAnalysisPipeline = pipeline_class
        pipeline = pipeline_class.return_value
        pipeline.run.return_value = [SimpleNamespace(code="600001", success=True)]
        pipeline.notifier.generate_dashboard_report.return_value = "report"
        config = SimpleNamespace(llm_model="existing-model-unchanged")
        with patch.dict("sys.modules", {"src.core.pipeline": module}), \
             patch("src.config.get_config", return_value=config), \
             patch.dict("os.environ", {"LITELLM_CONFIG_YAML": ""}):
            completed, report = native_analysis(["600001"])
        self.assertEqual(completed, ["600001"])
        self.assertEqual(report, "report")
        self.assertFalse(config.agent_mode)
        self.assertFalse(config.single_stock_notify)
        self.assertFalse(config.market_review_enabled)
        self.assertFalse(config.daily_market_context_enabled)
        self.assertEqual(config.llm_model, "existing-model-unchanged")
        pipeline.run.assert_called_once_with(stock_codes=["600001"], send_notification=False)
        self.assertFalse(pipeline_class.call_args.kwargs["daily_market_context_allow_generate"])

    def test_completed_report_can_be_sent_once_without_exposing_token(self):
        plan = self.ready_plan()
        analyze(self.path, "123", api=self.api, runner=lambda codes: (codes, "# Safe report"))
        factory = Mock()
        factory.return_value.send_to_pushplus.return_value = True
        with patch.dict("os.environ", {"PUSHPLUS_TOKEN": "dummy-secret-token"}):
            self.assertEqual(send_report(self.path, sender_factory=factory), 0)
            self.assertEqual(send_report(self.path, sender_factory=factory), 0)
        factory.return_value.send_to_pushplus.assert_called_once()
        for path in self.path.parent.glob("*.json"):
            self.assertNotIn("dummy-secret-token", path.read_text(encoding="utf-8"))

    def test_partial_analysis_is_not_reported_as_complete_or_sent(self):
        self.ready_plan()
        self.assertEqual(analyze(self.path, "123", api=self.api, runner=lambda codes: (codes[:1], "partial")), 1)
        factory = Mock()
        self.assertEqual(send_report(self.path, sender_factory=factory), 1)
        factory.assert_not_called()

    def test_old_report_with_different_codes_is_not_sent(self):
        self.ready_plan()
        analyze(self.path, '123', api=self.api, runner=lambda codes: (codes, '# Safe report'))
        path = self.path.parent / 'ai_result.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['selected_codes'] = ['300496']
        path.write_text(json.dumps(state), encoding='utf-8')
        factory = Mock()
        self.assertEqual(send_report(self.path, sender_factory=factory), 1)
        factory.assert_not_called()

    def test_workflow_reserves_before_model_and_manual_ai_defaults_off(self):
        import yaml
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/01-screening-test.yml"
        data = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        inputs = data["on"]["workflow_dispatch"]["inputs"]
        if "run_ai" not in inputs:
            return  # Support the documented ordinary-files-first installation.
        self.assertEqual(inputs["run_ai"]["default"], "false")
        steps = data["jobs"]["screen"]["steps"]
        reserve = next(i for i, step in enumerate(steps) if step.get("id") == "reserve_ai_budget")
        model = next(i for i, step in enumerate(steps) if "screening_ai.py analyze" in step.get("run", ""))
        self.assertLess(reserve, model)
        self.assertEqual(data["permissions"]["actions"], "read")


if __name__ == "__main__":
    unittest.main()
