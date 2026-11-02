"""Offline scheduling and PushPlus tests. All HTTP is mocked."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.screen_candidates import main as screening_main, schedule_gate
from scripts.send_screening_candidates import notification_gate, send_candidates
from scripts.screening_scope import policy_descriptor


class ScreeningPushplusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "screening_result.json"
        self.clock = {"calendar_status": "ok", "phase": "postmarket", "closed_session": True,
                      "expected_daily_date": "2026-10-21", "checked_at": "2026-10-21T17:17:00+08:00"}

    def payload(self):
        evidence = {"date_audit_status": "pass", "daily_trade_date": "2026-10-21",
                    "snapshot_trade_date": "2026-10-21"}
        pick = {"code": "600001", "name": "测试", "price": 10, "final_score": 70, **evidence}
        strategies = [{"strategy": name, "label": label, "status": "ok", "strategy_version": "fixture",
                       "snapshot_source": "fixture", "snapshot_original_source": "fixture",
                       "snapshot_fetched_at": "2026-10-21T17:16:00+08:00", "snapshot_count": 5000,
                       "daily_success_count": 1, "after_filter_count": 1, "date_audit_status": "pass",
                       "date_audit": [{"code": "600001", **evidence}], "picks": [dict(pick)],
                       "date_rejected_from_top5": [], "run_crossed_session_boundary": False,
                       "quality_rejected_from_top5": 0, "notes": []}
                      for name, label in (("volume_breakout", "放量突破"), ("shrink_pullback", "缩量回踩"))]
        return {"status": "ok", "ready_for_automation": True, "date_audit_version": 3,
                "market_clock": self.clock, "created_at": self.clock["checked_at"],
                "daily_limit_per_strategy": 100, "llm_enabled": False, "notifications_enabled": False,
                "universe_policy": policy_descriptor(),
                "strategies": strategies, "candidates": [{"code": "600001", "name": "测试", "price": 10, "signals": []}]}

    def write(self, payload):
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_schedule_holiday_unknown_intraday_and_delayed_midnight(self):
        self.assertEqual(schedule_gate(self.clock), "run")
        self.assertEqual(schedule_gate({**self.clock, "phase": "non_trading"}), "non_trading_day")
        self.assertEqual(schedule_gate({**self.clock, "calendar_status": "unknown"}), "calendar_unknown")
        self.assertEqual(schedule_gate({**self.clock, "phase": "intraday"}), "not_today_after_close")
        midnight = {**self.clock, "checked_at": "2026-10-22T00:30:00+08:00", "phase": "premarket"}
        self.assertEqual(schedule_gate(midnight), "not_today_after_close")

    def test_expanded_size_gate_accepts_twenty_and_rejects_overflow(self):
        payload = self.payload()
        for index, strategy in enumerate(payload["strategies"]):
            template = strategy["picks"][0]
            strategy["picks"] = [{**template, "code": f"600{index*10+i:03}"} for i in range(10)]
            strategy["date_audit"] = [{key: pick[key] for key in ("code", "date_audit_status", "daily_trade_date", "snapshot_trade_date")} for pick in strategy["picks"]]
        payload["candidates"] = [dict(code=p["code"], name=p["name"], price=p["price"], signals=[])
                                 for strategy in payload["strategies"] for p in strategy["picks"]]
        self.assertEqual(notification_gate(payload, self.clock, scheduled=True), "send")
        payload["candidates"].append(payload["candidates"][0])
        self.assertEqual(notification_gate(payload, self.clock, scheduled=True), "blocked_size")
        payload["candidates"].pop()
        payload["strategies"][0]["picks"].append(payload["strategies"][0]["picks"][0])
        self.assertEqual(notification_gate(payload, self.clock, scheduled=True), "blocked_size")

    def test_scheduled_holiday_skips_before_any_provider(self):
        clock = {**self.clock, "phase": "non_trading"}
        with patch("scripts.screen_candidates.market_clock", return_value=clock), \
             patch("scripts.screen_candidates.run_screening") as screen, \
             patch("sys.argv", ["screen_candidates.py", "--scheduled", "--output-dir", str(self.path.parent)]):
            self.assertEqual(screening_main(), 0)
            screen.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["status"], "skipped")

    def test_unknown_calendar_fails_before_any_provider(self):
        clock = {**self.clock, "calendar_status": "unknown"}
        with patch("scripts.screen_candidates.market_clock", return_value=clock), \
             patch("scripts.screen_candidates.run_screening") as screen, \
             patch("sys.argv", ["screen_candidates.py", "--scheduled", "--output-dir", str(self.path.parent)]):
            self.assertEqual(screening_main(), 1)
            screen.assert_not_called()

    def test_unknown_date_or_expired_result_is_blocked(self):
        payload = self.payload()
        payload["ready_for_automation"] = False
        self.assertEqual(notification_gate(payload, self.clock, scheduled=True), "blocked_date_audit")
        payload = self.payload()
        payload["strategies"][0]["date_audit"][0]["date_audit_status"] = "unknown"
        self.assertEqual(notification_gate(payload, self.clock, scheduled=True), "blocked_date_evidence")
        self.assertEqual(notification_gate(self.payload(), {**self.clock, "expected_daily_date": "2026-10-22"},
                                           scheduled=False), "blocked_expired_result")

    def test_blocked_and_missing_token_never_create_sender(self):
        factory = Mock()
        for ready in (False, True):
            payload = self.payload()
            payload["ready_for_automation"] = ready
            self.write(payload)
            with patch("scripts.send_screening_candidates.market_clock", return_value=self.clock), \
                 patch.dict("os.environ", {"PUSHPLUS_TOKEN": ""}):
                self.assertEqual(send_candidates(self.path, sender_factory=factory), 1)
        factory.assert_not_called()

    def test_skipped_holiday_does_not_send(self):
        payload = self.payload()
        payload["status"] = "skipped"
        self.write(payload)
        factory = Mock()
        with patch("scripts.send_screening_candidates.market_clock", return_value=self.clock):
            self.assertEqual(send_candidates(self.path, scheduled=True, sender_factory=factory), 0)
        factory.assert_not_called()

    def test_outdated_scope_or_gem_candidate_never_initializes_sender(self):
        for old in (True, False):
            payload = self.payload()
            if old:
                payload.pop('universe_policy')
            else:
                payload['candidates'][0]['code'] = '300496'
            self.write(payload)
            factory = Mock()
            with patch('scripts.send_screening_candidates.market_clock', return_value=self.clock):
                self.assertEqual(notification_gate(payload, self.clock, scheduled=False), 'blocked_universe_policy')
                self.assertEqual(send_candidates(self.path, sender_factory=factory), 1)
            factory.assert_not_called()

    def test_native_sender_https_request_and_accepted_status(self):
        self.write(self.payload())
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200}
        with patch("scripts.send_screening_candidates.market_clock", return_value=self.clock), \
             patch.dict("os.environ", {"PUSHPLUS_TOKEN": "dummy-test-token", "PUSHPLUS_TOPIC": ""}), \
             patch("src.notification_sender.pushplus_sender.requests.post", return_value=response) as post:
            self.assertEqual(send_candidates(self.path, scheduled=True), 0)
            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], "https://www.pushplus.plus/send")
            self.assertEqual(post.call_args.kwargs["timeout"], 15)
            message = post.call_args.kwargs["json"]
            self.assertIn("600001", message["content"])
            self.assertEqual(message["token"], "dummy-test-token")
        artifact = self.path.read_text(encoding="utf-8")
        self.assertNotIn("dummy-test-token", artifact)
        self.assertEqual(json.loads(artifact)["notification"]["status"], "accepted")

    def test_same_result_does_not_repeat_accepted_or_uncertain_submission(self):
        for state in ("accepted", "submitting", "failed_or_unknown"):
            payload = self.payload()
            payload["notification"] = {"status": state}
            self.write(payload)
            factory = Mock()
            with patch("scripts.send_screening_candidates.market_clock", return_value=self.clock):
                send_candidates(self.path, sender_factory=factory)
            factory.assert_not_called()

    def test_failed_post_is_recorded_without_token_or_automatic_retry(self):
        self.write(self.payload())
        factory = Mock()
        factory.return_value.send_to_pushplus.side_effect = TimeoutError("never print secret")
        with patch("scripts.send_screening_candidates.market_clock", return_value=self.clock), \
             patch.dict("os.environ", {"PUSHPLUS_TOKEN": "dummy-test-token"}):
            self.assertEqual(send_candidates(self.path, sender_factory=factory), 1)
        factory.return_value.send_to_pushplus.assert_called_once()
        self.assertNotIn("dummy-test-token", self.path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["notification"]["status"],
                         "failed_or_unknown")


if __name__ == "__main__":
    unittest.main()
