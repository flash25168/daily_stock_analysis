"""Offline tests for the manual screening workflow; no live provider or model calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.screen_candidates import make_config, run_screening, safe_text
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

    def test_ten_limit_dedup_and_leading_zero(self):
        def screen(strategy, **kwargs):
            self.assertFalse(kwargs["use_llm"])
            self.assertFalse(kwargs["deep_analysis"])
            self.assertEqual(kwargs["post_analyzers"], ["scorecard"])
            self.assertIsNone(kwargs["context"])
            start = 1 if strategy == "volume_breakout" else 5
            picks = [Pick(rank=i + 1, code=f"{start + i:06}", name="测试",
                          final_score=70, screen_score=70) for i in range(5)]
            return self.result(strategy, picks=picks)
        payload = run_screening(self.output, screen_fn=screen)
        self.assertEqual(len(payload["candidates"]), 9)
        common = next(row for row in payload["candidates"] if row["code"] == "000005")
        self.assertEqual(len(common["signals"]), 2)
        self.assertTrue((self.output / "candidate_codes.txt").read_text().startswith("000001"))

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
        picks = [Pick(rank=1, code="600000", name="测试", final_score=90, screen_score=90,
                      daily_quality_flags="stale_cache"),
                 Pick(rank=2, code="000001", name="测试", final_score=70, screen_score=70)]
        result = run_screening(self.output, screen_fn=lambda strategy, **_: self.result(
            strategy, picks=picks, degradation=["failed https://example.com?token=private"] ))
        self.assertEqual([row["code"] for row in result["candidates"]], ["000001"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("private", safe_text("token=private"))

    def test_workflow_has_no_secrets_schedule_or_analysis_entrypoint(self):
        import yaml
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/01-screening-test.yml"
        text = workflow.read_text(encoding="utf-8")
        data = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(list(data["on"]), ["workflow_dispatch"])
        self.assertNotIn("secrets.", text)
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

        def enrich(frame, **_):
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


if __name__ == "__main__":
    unittest.main()
