"""Offline tests for the manual screening workflow; no live provider or model calls."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.screen_candidates import (
    audit_pick_dates, history_dates, make_config, market_clock, parse_trade_date,
    run_screening, safe_text, snapshot_dates,
)
from src.services.screening.models import Pick, ScreenResult


class CandidateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def result(self, strategy, **kwargs):
        return ScreenResult(strategy=strategy, market="cn", snapshot_count=5000, **kwargs)

    def test_config_ignores_remote_and_model_environment(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "dummy", "POST_ANALYZERS": "dsa,external_http",
                                       "POST_ANALYZER_URL": "https://invalid.example"}):
            config = make_config(self.output, 30)
        self.assertFalse(config.has_llm_config())
        self.assertEqual(config.post_analyzers, ["scorecard"])
        self.assertEqual(config.post_analyzer_url, "")
        self.assertEqual(config.dsa_api_url, "")

    def test_overlapping_strategies_dedup_and_leading_zero(self):
        def screen(strategy, **kwargs):
            self.assertFalse(kwargs["use_llm"])
            self.assertFalse(kwargs["deep_analysis"])
            self.assertEqual(kwargs["post_analyzers"], ["scorecard"])
            self.assertIsNone(kwargs["context"])
            start = 1 if strategy == "volume_breakout" else 5
            picks = [Pick(price=10, rank=i + 1, code=f"{start + i:06}", name="测试",
                          final_score=70, screen_score=70) for i in range(5)]
            return self.result(strategy, picks=picks)
        payload = run_screening(self.output, screen_fn=screen)
        self.assertEqual(len(payload["candidates"]), 9)
        common = next(row for row in payload["candidates"] if row["code"] == "000005")
        self.assertEqual(len(common["signals"]), 2)
        self.assertTrue((self.output / "candidate_codes.txt").read_text().startswith("000001"))

    def test_twenty_candidates_keep_rank_order_and_ten_per_strategy(self):
        def screen(strategy, **kwargs):
            self.assertEqual(kwargs["max_output"], 10)
            start = 1 if strategy == "volume_breakout" else 11
            picks = [Pick(price=10, rank=i+1, code=f"600{start+i:03}",
                          name="测试", final_score=70, screen_score=70) for i in range(12)]
            return self.result(strategy, picks=picks)
        result = run_screening(self.output, screen_fn=screen)
        self.assertEqual(len(result["candidates"]), 20)
        self.assertTrue(all(len(strategy["picks"]) == 10 for strategy in result["strategies"]))
        self.assertEqual([p["code"] for p in result["candidates"][:4]],
                         ["600001", "600011", "600002", "600012"])
        self.assertIn("最多 20 只", (self.output / "screening_report.md").read_text(encoding="utf-8"))

    def test_legitimate_empty_is_success(self):
        result = run_screening(self.output, screen_fn=lambda strategy, **_: self.result(strategy))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"], [])

    def test_daily_outage_is_incomplete_not_normal_empty(self):
        result = run_screening(self.output, screen_fn=lambda strategy, **_: self.result(
            strategy, daily_enriched=True, daily_enrich_count=0))
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(all(row["status"] == "error" for row in result["strategies"]))

    def test_one_strategy_failure_keeps_other_result_and_hides_exception(self):
        def screen(strategy, **_):
            if strategy == "volume_breakout":
                raise RuntimeError("token=do-not-print-this")
            return self.result(strategy)
        result = run_screening(self.output, screen_fn=screen)
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("do-not-print-this", (self.output / "screening_result.json").read_text(encoding="utf-8"))

    def test_bad_quality_removed_and_source_urls_redacted(self):
        picks = [Pick(price=10, rank=1, code="600000", name="测试", final_score=90, screen_score=90,
                      daily_quality_flags="stale_cache"),
                 Pick(price=10, rank=2, code="000001", name="测试", final_score=70, screen_score=70)]
        result = run_screening(self.output, screen_fn=lambda strategy, **_: self.result(
            strategy, picks=picks, degradation=["failed https://example.com?token=private"] ))
        self.assertEqual([row["code"] for row in result["candidates"]], ["000001"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("private", safe_text("token=private"))

    def test_workflow_limits_secrets_to_their_execution_stage(self):
        import yaml
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/01-screening-test.yml"
        text = workflow.read_text(encoding="utf-8")
        data = yaml.load(text, Loader=yaml.BaseLoader)
        # Keep v3's manual-only workflow compatible while users install v4
        # ordinary files first, then replace the workflow in a second commit.
        self.assertIn("workflow_dispatch", data["on"])
        self.assertTrue(set(data["on"]).issubset({"schedule", "workflow_dispatch"}))
        if "schedule" in data["on"]:
            self.assertEqual(data["on"]["schedule"][0]["cron"], "17 9 * * 1-5")
        for step in data["jobs"]["screen"]["steps"]:
            step_text = json.dumps(step)
            if "secrets." in step_text:
                command = step.get("run", "")
                if "screening_ai.py analyze" in command:
                    self.assertNotIn("PUSHPLUS_TOKEN", step["env"])
                    self.assertIn("steps.reserve_ai_budget.outputs.artifact-id", step["env"]["RESERVATION_ID"])
                else:
                    self.assertTrue("send_screening_candidates.py" in command or "screening_ai.py send" in command)
                    self.assertTrue(set(step["env"]).issubset({"PUSHPLUS_TOKEN", "PUSHPLUS_TOPIC", "EVENT_NAME"}))
        self.assertNotIn("main.py", text)

    def test_unexpected_model_marker_fails(self):
        result = run_screening(self.output, screen_fn=lambda strategy, **_: self.result(
            strategy, llm_attempted_models=["unexpected"]))
        self.assertEqual(result["status"], "incomplete")

    def test_real_pipeline_strategies_and_no_remote_paths(self):
        """Exercise real YAML, filters, scoring and scorecard with offline provider data."""
        from src.services.screening.pipeline import screen

        snapshot = pd.DataFrame([
            dict(code="600001", name="测试突破", price=10, change_pct=3, amount=200000000,
                 total_mv=10000000000, turnover_rate=4, volume_ratio=2.5, pe_ratio=15, pb_ratio=2),
            dict(code="000002", name="测试回踩", price=10, change_pct=-0.5, amount=200000000,
                 total_mv=10000000000, turnover_rate=2, volume_ratio=1, pe_ratio=15, pb_ratio=2),
        ])
        snapshot.attrs["snapshot_source"] = "offline_fixture"
        # These would otherwise consume the bounded daily pool. Give them the
        # same valid strategy inputs so this exercises scope, not factor luck.
        template = snapshot.iloc[0].to_dict()
        snapshot = pd.concat([snapshot, pd.DataFrame([
            {**template, 'code': code, 'price': price, 'name': name}
            for code, price, name in [('300496', 10, '创业板'), ('688981', 10, '科创板'),
                ('920001', 10, '北交所'), ('600519', 1000, '高价主板'),
                ('002001', 10, '*ST测试'), ('600002', None, '未知价格')]
        ])], ignore_index=True)
        snapshot.attrs['snapshot_source'] = 'offline_fixture'

        def enrich(frame, **_):
            self.assertTrue(set(frame['code']).issubset({'600001', '000002'}))
            self.assertGreater(len(frame), 0)
            enriched = frame.copy()
            for key, value in dict(signal_score=85, ma_bullish=True, price_above_ma20=True,
                                   macd_status="bullish", breakout_20d_pct=0.2, range_20d_pct=20,
                                   body_pct=2, consolidation_days_20d=12, pullback_to_ma20_pct=2,
                                   volatility_20d_pct=20, max_drawdown_20d_pct=-5, atr_20_pct=3,
                                   daily_quality_score=100, daily_quality_flags="",
                                   daily_source="offline_fixture").items():
                enriched[key] = value
            enriched["volume_ratio_20d"] = enriched["code"].map({"600001": 1.8, "000002": 1.0})
            enriched.attrs["daily_success_count"] = len(enriched)
            return enriched

        with patch("src.services.screening.pipeline.fetch_snapshot_with_fallback", return_value=snapshot), \
             patch("src.services.screening.pipeline.enrich_daily_features", side_effect=enrich), \
             patch("src.services.screening.pipeline.rank_candidates_with_metadata", side_effect=AssertionError("LLM")) as llm, \
             patch("src.services.screening.pipeline.collect_candidate_context", side_effect=AssertionError("news")) as news, \
             patch("src.services.screening.post_analysis._run_dsa_analyzer", side_effect=AssertionError("DSA")) as dsa, \
             patch("src.services.screening.post_analysis._run_external_http_analyzer", side_effect=AssertionError("HTTP")) as remote:
            result = run_screening(self.output, screen_fn=screen)
            for mocked in (llm, news, dsa, remote):
                mocked.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([p["code"] for p in result["strategies"][0]["picks"]], ["600001"])
        self.assertEqual([p["code"] for p in result["strategies"][1]["picks"]], ["000002"])
        scope = result['strategies'][0]['universe_audit']
        self.assertEqual((scope['input_count'], scope['eligible_count']), (8, 2))
        self.assertEqual(scope['rejected_counts']['outside_mainboard'], 3)

    def test_mainboard_budget_boundaries_and_unknown_values(self):
        from scripts.screening_scope import rejection_reason
        for code in ('000001', '001001', '002001', '003001', '600001', '601001', '603001', '605001'):
            self.assertIsNone(rejection_reason(code, '主板测试', 99))
        for code in ('300496', '301001', '688981', '689001', '920001', '830001',
                     '430001', '200001', '900901', '510300', '159001', 'sh600001', '00001', 600001):
            self.assertEqual(rejection_reason(code, '测试', 10), 'outside_mainboard')
        self.assertEqual(rejection_reason('600001', '测试', 99.01), 'over_budget')
        for price in (None, 0, -1, True, float('nan'), float('inf'), '12%', ''):
            self.assertEqual(rejection_reason('600001', '测试', price), 'price_unknown')
        for name in ('*ST测试', 'st测试', '测试退'):
            self.assertEqual(rejection_reason('600001', name, 10), 'risk_name')

    def test_out_of_scope_engine_output_is_removed_before_candidate_files(self):
        def screen(strategy, **kwargs):
            return self.result(strategy, picks=[
                Pick(rank=1, code='300496', name='中科创达', price=57.52, final_score=99, screen_score=99),
                Pick(rank=2, code='600519', name='贵州茅台', price=1329, final_score=98, screen_score=98),
                Pick(rank=3, code='000528', name='柳工', price=8.51, final_score=70, screen_score=70),
            ])
        payload = run_screening(self.output, screen_fn=screen)
        self.assertEqual([row['code'] for row in payload['candidates']], ['000528'])
        self.assertEqual(payload['candidates'][0]['price'], 8.51)
        self.assertEqual(len(payload['strategies'][0]['scope_rejected_from_top5']), 2)
        self.assertEqual((self.output / 'candidate_codes.txt').read_text().strip(), '000528')
        report = (self.output / 'screening_report.md').read_text(encoding='utf-8')
        self.assertIn('参考股价≤99元', report)
        self.assertNotIn('中科创达', report)

    def clock(self):
        return {"calendar_status": "ok", "expected_daily_date": "2026-10-21",
                "closed_session": True}

    def snapshot(self, day=None):
        return {"snapshot_fetched_at": "2026-10-22T00:05:00+08:00",
                "snapshot_original_source": "fixture",
                "dates_by_code": {"600001": day} if day else {}}

    def test_midnight_fetch_time_does_not_prove_snapshot_date(self):
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-21"},
                                 self.snapshot(), self.clock())
        self.assertIsNone(audit["snapshot_trade_date"])
        self.assertEqual(audit["date_audit_status"], "unknown")
        self.assertIn("snapshot_date_missing", audit["date_audit_reasons"])

    def test_explicit_same_dates_pass_but_mismatch_fails(self):
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-21"},
                                 self.snapshot("2026-10-21"), self.clock())
        self.assertEqual(audit["date_audit_status"], "pass")
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-21"},
                                 self.snapshot("2026-10-20"), self.clock())
        self.assertEqual(audit["date_audit_status"], "fail")
        self.assertIn("snapshot_daily_date_mismatch", audit["date_audit_reasons"])

    def test_future_bar_and_unknown_calendar_cannot_pass(self):
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-22"},
                                 self.snapshot("2026-10-21"), self.clock())
        self.assertEqual(audit["date_audit_status"], "fail")
        clock = {**self.clock(), "calendar_status": "unknown", "expected_daily_date": None}
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-21"},
                                 self.snapshot("2026-10-21"), clock)
        self.assertEqual(audit["date_audit_status"], "unknown")

    def test_history_uses_latest_usable_close_not_row_order_or_zero_close(self):
        history = pd.DataFrame({"date": ["20261021", "2026-10-20", "2026-10-22"],
                                "close": [10, 9, 0]})
        result = history_dates(history)
        self.assertEqual(result["daily_trade_date"], "2026-10-21")
        self.assertEqual(result["daily_last_close"], 10)
        self.assertIsNone(parse_trade_date("2026-02-30"))
        self.assertIsNone(parse_trade_date(1792596000))
        self.assertIsNone(parse_trade_date(pd.NA))

    def test_snapshot_cache_uses_provider_date_never_cache_timestamp(self):
        path = self.output / "snapshot.json"
        payload = {"created_at": "2026-10-22T00:05:00+08:00", "metadata": {},
                   "frame": {"columns": ["code", "price"], "data": [["600001", 10]]}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(snapshot_dates(path)["dates_by_code"], {})
        payload["frame"] = {"columns": ["code", "TRADE_DATE"], "data": [["600001", "20261021"]]}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(snapshot_dates(path)["dates_by_code"], {"600001": "2026-10-21"})

    def test_midnight_weekend_and_intraday_calendar(self):
        for instant, expected in (("2026-10-22T00:05:00+08:00", "2026-10-21"),
                                  ("2026-10-24T12:00:00+08:00", "2026-10-23")):
            clock = market_clock(datetime.fromisoformat(instant))
            self.assertEqual(clock["calendar_status"], "ok")
            self.assertEqual(clock["expected_daily_date"], expected)
            self.assertTrue(clock["closed_session"])
        clock = market_clock(datetime.fromisoformat("2026-10-22T10:00:00+08:00"))
        self.assertFalse(clock["closed_session"])

    def test_eastmoney_original_query_max_trade_date_is_used(self):
        path = self.output / "snapshot.json"
        payload = {"created_at": "2026-10-22T00:36:00+08:00",
                   "metadata": {"snapshot_source": "em_datacenter"},
                   "frame": {"columns": ["code", "price", "MAX_TRADE_DATE", "TRADE_DATE"],
                             "data": [["600001", 10, "2026-10-21", "2026-08-19"]]}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        snapshot = snapshot_dates(path)
        self.assertEqual(snapshot["dates_by_code"], {"600001": "2026-10-21"})
        audit = audit_pick_dates("600001", {"daily_trade_date": "2026-10-21"},
                                 snapshot, self.clock())
        self.assertEqual(audit["snapshot_trade_date_field"], "MAX_TRADE_DATE")
        self.assertEqual(audit["date_audit_status"], "pass")

    def test_eastmoney_date_missing_or_malformed_stays_unknown(self):
        path = self.output / "snapshot.json"
        for fields, values in ((["TRADE_DATE"], ["2026-10-21"]),
                               (["MAX_TRADE_DATE"], ["invalid"])):
            payload = {"created_at": "2026-10-22T00:36:00+08:00",
                       "metadata": {"snapshot_source": "em_datacenter"},
                       "frame": {"columns": ["code", *fields], "data": [["600001", *values]]}}
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(snapshot_dates(path)["dates_by_code"], {})

    def test_other_provider_cannot_borrow_eastmoney_max_date_field(self):
        path = self.output / "snapshot.json"
        payload = {"metadata": {"snapshot_source": "sina"},
                   "frame": {"columns": ["code", "MAX_TRADE_DATE"],
                             "data": [["600001", "2026-10-21"]]}}
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(snapshot_dates(path)["dates_by_code"], {})

    def test_callback_audits_same_history_and_rejects_mismatched_pick(self):
        seen = []

        def screen(strategy, **kwargs):
            history = kwargs["daily_history_fetcher"]("600001", source="auto")
            seen.append(history)
            return self.result(strategy, picks=[Pick(price=10, rank=1, code="600001", name="fixture",
                                                     final_score=70, screen_score=70)])
        hist = pd.DataFrame({"date": ["2026-10-20"], "close": [10]})
        clock = {**self.clock(), "phase": "premarket", "checked_at": "2026-10-22T00:05:00+08:00"}
        with patch("scripts.screen_candidates.market_clock", return_value=clock), \
             patch("src.services.screening.daily.fetch_daily_history", return_value=hist) as fetch:
            result = run_screening(self.output, screen_fn=screen)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(row is hist for row in seen))
        self.assertFalse(result["ready_for_automation"])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(len(result["strategies"][0]["date_rejected_from_top5"]), 1)

    def test_unknown_snapshot_keeps_diagnostic_pick_but_not_ready(self):
        def screen(strategy, **kwargs):
            kwargs["daily_history_fetcher"]("600001")
            return self.result(strategy, picks=[Pick(price=10, rank=1, code="600001", name="fixture",
                                                     final_score=70, screen_score=70)])
        hist = pd.DataFrame({"date": ["2026-10-21"], "close": [10]})
        clock = {**self.clock(), "phase": "premarket", "checked_at": "2026-10-22T00:05:00+08:00"}
        with patch("scripts.screen_candidates.market_clock", return_value=clock), \
             patch("src.services.screening.daily.fetch_daily_history", return_value=hist):
            result = run_screening(self.output, screen_fn=screen)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["ready_for_automation"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["strategies"][0]["date_audit_counts"], {"unknown": 1})
        self.assertIn("unknown", (self.output / "candidates.csv").read_text(encoding="utf-8-sig"))

    def test_ready_requires_explicit_dates_and_all_sampled_histories(self):
        def screen(strategy, **kwargs):
            kwargs["daily_history_fetcher"]("600001")
            return self.result(strategy, picks=[Pick(price=10, rank=1, code="600001", name="fixture",
                                                     final_score=70, screen_score=70)])
        hist = pd.DataFrame({"date": ["2026-10-21"], "close": [10]})
        clock = {**self.clock(), "phase": "premarket", "checked_at": "2026-10-22T00:05:00+08:00"}
        with patch("scripts.screen_candidates.market_clock", return_value=clock), \
             patch("scripts.screen_candidates.snapshot_dates", return_value=self.snapshot("2026-10-21")), \
             patch("src.services.screening.daily.fetch_daily_history", return_value=hist):
            result = run_screening(self.output, screen_fn=screen)
        self.assertTrue(result["ready_for_automation"])
        self.assertEqual(result["strategies"][0]["date_audit_counts"], {"pass": 1})


if __name__ == "__main__":
    unittest.main()
