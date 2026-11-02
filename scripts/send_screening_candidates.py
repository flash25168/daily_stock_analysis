#!/usr/bin/env python3
"""Send a date-verified factor-only shortlist with DSA's existing PushPlus sender."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.screen_candidates import STRATEGIES, market_clock, safe_text, schedule_gate, write_outputs
from scripts.screening_scope import MAX_CANDIDATES, PICKS_PER_STRATEGY, SCOPE_NOTICE, validate_payload


def notification_gate(payload: dict, clock: dict, *, scheduled: bool) -> str:
    if payload.get("status") == "skipped":
        return "skipped"
    if payload.get("status") != "ok" or not payload.get("ready_for_automation"):
        return "blocked_date_audit"
    if clock["calendar_status"] != "ok" or not clock["closed_session"]:
        return "blocked_market_clock"
    expected = clock["expected_daily_date"]
    if not expected or payload.get("market_clock", {}).get("expected_daily_date") != expected:
        return "blocked_expired_result"
    if scheduled and schedule_gate(clock) != "run":
        return "blocked_schedule"
    results = payload.get("strategies", [])
    if {row.get("strategy") for row in results} != set(STRATEGIES):
        return "blocked_strategy_missing"
    for result in results:
        if result.get("status") != "ok" or result.get("date_audit_status") != "pass":
            return "blocked_date_audit"
        audits = result.get("date_audit", [])
        if not audits or any(row.get("date_audit_status") != "pass"
                             or row.get("daily_trade_date") != expected
                             or row.get("snapshot_trade_date") != expected for row in audits):
            return "blocked_date_evidence"
        if result.get("date_rejected_from_top5") or result.get("run_crossed_session_boundary"):
            return "blocked_date_evidence"
        if len(result.get("picks", [])) > PICKS_PER_STRATEGY:
            return "blocked_size"
        for pick in result.get("picks", []):
            if (pick.get("date_audit_status") != "pass" or pick.get("daily_trade_date") != expected
                    or pick.get("snapshot_trade_date") != expected):
                return "blocked_date_evidence"
    if len(payload.get("candidates", [])) > MAX_CANDIDATES:
        return "blocked_size"
    try:
        validate_payload(payload)
    except (ValueError, TypeError, KeyError):
        return 'blocked_universe_policy'
    return "send"


def message_body(payload: dict) -> tuple[str, str]:
    day = payload["market_clock"]["expected_daily_date"]
    title = f"A股选股候选（非AI）{day}"
    lines = [f"# {title}", "", "仅技术因子筛选，未调用 AI、未核验新闻与公告。",
             SCOPE_NOTICE,
             f"交易日期：{day}；来源声明的快照日期与日线日期核验通过。",
             f"每策略日线核验上限：{payload['daily_limit_per_strategy']}，非逐股全市场扫描。",
             "分数仅用于同一策略、同次候选池内比较，不表示胜率。", ""]
    for result in payload["strategies"]:
        lines += [f"## {STRATEGIES[result['strategy']]}"]
        if not result["picks"]:
            lines.append("本次核验范围内无可输出候选，未放宽条件凑数。")
        for pick in result["picks"]:
            lines.append(f"- {safe_text(pick['code'])} {safe_text(pick['name'])[:40]}"
                         f"（策略内分 {float(pick['final_score']):.2f}）")
        lines.append("")
    lines += [f"去重候选：{len(payload['candidates'])} 只（最多 {MAX_CANDIDATES} 只）。",
              "日期一致不保证每个价格新鲜；名单供观察，不是买入指令。"]
    return title, "\n".join(lines)


def send_candidates(result_path: Path, *, scheduled: bool = False, sender_factory=None) -> int:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    gate = notification_gate(payload, market_clock(), scheduled=scheduled)
    # Avoid repeating an accepted/uncertain submission when this same artifact is reused.
    previous = payload.get("notification", {}).get("status")
    if previous in {"accepted", "submitting", "failed_or_unknown"}:
        print("此结果已有提交记录；不自动重复发送。")
        return 0 if previous == "accepted" else 1

    def save(status, message):
        payload["notifications_enabled"] = True
        payload["notification"] = {"channel": "pushplus", "status": status, "message": message}
        write_outputs(result_path.parent, payload)
        print(message, flush=True)

    if gate == "skipped":
        save("skipped", "休市或非本交易日收盘后任务，未发送通知。")
        return 0
    if gate != "send":
        save("blocked", f"推送已阻止：{gate}，未向 PushPlus 提交名单。")
        return 1
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        save("missing_configuration", "未读到 PUSHPLUS_TOKEN；请检查已有 Secret 的名称与作用域。")
        return 1
    if sender_factory is None:
        from src.notification_sender.pushplus_sender import PushplusSender
        sender_factory = PushplusSender
    title, body = message_body(payload)
    if len(body.encode("utf-8")) > 18000:
        save("blocked", "候选摘要超出单条消息预算，未提交。")
        return 1
    config = SimpleNamespace(pushplus_token=token, pushplus_topic=os.environ.get("PUSHPLUS_TOPIC", "").strip() or None,
                             pushplus_max_bytes=20000)
    # Persist before network I/O. A crash/timeout must not cause an automatic retry.
    save("submitting", "正在向 PushPlus 提交候选摘要。")
    try:
        accepted = sender_factory(config).send_to_pushplus(body, title, timeout_seconds=15)
    except Exception:
        accepted = False
    if accepted:
        save("accepted", "PushPlus 已受理；是否送达请以微信实际收到为准。")
        return 0
    save("failed_or_unknown", "PushPlus 未确认受理或响应异常；可能已提交，不自动重试，请先检查微信。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, default=ROOT / "reports/screening_test/screening_result.json")
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    return send_candidates(args.result, scheduled=args.scheduled)


if __name__ == "__main__":
    raise SystemExit(main())
