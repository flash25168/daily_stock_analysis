#!/usr/bin/env python3
"""Prepare, analyze and optionally push at most five screened stocks per day."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.screen_candidates import market_clock, safe_text, write_outputs
from scripts.send_screening_candidates import notification_gate
from scripts.screening_scope import SCOPE_NOTICE, is_mainboard, validate_payload

MAX_STOCKS = 5


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def api_json(path: str, *, params=None):
    import requests

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not token:
        raise RuntimeError("GitHub budget verification unavailable")
    response = requests.get(
        f"https://api.github.com/repos/{repository}/actions/{path}", params=params,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}, timeout=15)
    response.raise_for_status()
    return response.json()


def select_codes(payload: dict) -> list[str]:
    validate_payload(payload)
    allowed = {row["code"] for strategy in payload["strategies"] for row in strategy["picks"]}
    codes = []
    for row in payload.get("candidates", []):
        code = row.get("code", "")
        if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or code not in allowed:
            raise ValueError("Invalid candidate code")
        if code not in codes:
            codes.append(code)
    return codes[:MAX_STOCKS]


def prepare(result_path: Path, *, scheduled=False, api=api_json) -> dict:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    clock = market_clock()
    plan = {"status": "blocked", "codes": [], "trade_date": clock["expected_daily_date"],
            "budget_day": datetime.fromisoformat(clock["checked_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""), "scheduled": scheduled,
            "max_stocks": MAX_STOCKS}
    plan["budget_name"] = "screening-ai-budget-" + plan["budget_day"]
    gate = notification_gate(payload, clock, scheduled=scheduled)
    if gate == "skipped":
        plan["status"] = "skipped_no_session"
    elif gate != "send":
        plan["reason"] = gate
    else:
        codes = select_codes(payload)
        if not codes:
            plan["status"] = "skipped_no_candidates"
        else:
            metadata = api("artifacts", params={"name": plan["budget_name"], "per_page": 100})
            if not isinstance(metadata.get("artifacts"), list) or not isinstance(metadata.get("total_count"), int):
                raise RuntimeError("Invalid budget response")
            # Any record for this Beijing day consumes the single batch, including
            # an aborted attempt. Never release it automatically after an error.
            if metadata["total_count"] > 0 or metadata["artifacts"]:
                plan["status"] = "skipped_daily_budget"
            else:
                plan["status"], plan["codes"] = "ready", codes
    write_json(result_path.parent / "ai_plan.json", plan)
    payload["ai_analysis"] = {"status": plan["status"], "codes": plan["codes"],
                              "budget_day": plan["budget_day"]}
    write_outputs(result_path.parent, payload)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"analyze={'true' if plan['status'] == 'ready' else 'false'}\n")
            handle.write(f"budget_name={plan['budget_name']}\n")
    print(f"AI 计划：{plan['status']}；本批 {len(plan['codes'])} 只（最多 {MAX_STOCKS} 只）。", flush=True)
    return plan


def native_analysis(codes: list[str]) -> tuple[list[str], str]:
    if not codes or len(codes) > MAX_STOCKS or not all(is_mainboard(code) for code in codes):
        raise ValueError('Invalid mainboard analysis scope')
    # Import after the budget reservation is verified. No model is initialized
    # during planning, offline tests or a no-candidate run.
    yaml_text = os.environ.get("LITELLM_CONFIG_YAML", "")
    if yaml_text.strip():
        config_path = ROOT / "work" / "screening-llm-config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml_text, encoding="utf-8")
        os.environ["LITELLM_CONFIG"] = str(config_path)
    from src.config import get_config
    from src.core.pipeline import StockAnalysisPipeline

    config = get_config()
    config.stock_list = list(codes)
    config.agent_mode = False
    config._agent_mode_explicit = True
    config.single_stock_notify = False
    config.market_review_enabled = False
    config.daily_market_context_enabled = False
    config.backtest_enabled = False
    config.report_type = "full"
    pipeline = StockAnalysisPipeline(
        config=config, max_workers=1, save_context_snapshot=False,
        daily_market_context_enabled=False, daily_market_context_allow_generate=False)
    results = pipeline.run(stock_codes=list(codes), send_notification=False)
    successful = [row for row in results if getattr(row, "success", False) and row.code in codes]
    completed = list(dict.fromkeys(row.code for row in successful))
    report = pipeline.notifier.generate_dashboard_report(successful) if successful else ""
    return completed, report


def analyze(result_path: Path, reservation_id: str, *, api=api_json, runner=native_analysis) -> int:
    folder = result_path.parent
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    plan = json.loads((folder / "ai_plan.json").read_text(encoding="utf-8"))
    previous_path = folder / "ai_result.json"
    if previous_path.exists():
        print("本结果已记录 AI 尝试，不自动重复调用模型。")
        return 1
    codes = plan.get("codes", [])
    if (plan.get("status") != "ready" or not 1 <= len(codes) <= MAX_STOCKS
            or len(set(codes)) != len(codes) or codes != select_codes(payload)):
        raise ValueError("Invalid AI plan")
    if not str(reservation_id).isdigit():
        raise ValueError("Missing confirmed budget artifact")
    clock = market_clock()
    if (notification_gate(payload, clock, scheduled=plan.get("scheduled", False)) != "send"
            or datetime.fromisoformat(clock["checked_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != plan["budget_day"]):
        raise RuntimeError("AI plan expired")
    reservation = api(f"artifacts/{reservation_id}")
    if (reservation.get("name") != plan["budget_name"] or reservation.get("expired") is not False
            or str(reservation.get("workflow_run", {}).get("id")) != plan["run_id"]):
        raise RuntimeError("Budget reservation mismatch")
    claims = api("artifacts", params={"name": plan["budget_name"], "per_page": 100})
    if (claims.get("total_count") != 1 or len(claims.get("artifacts", [])) != 1
            or str(claims["artifacts"][0].get("id")) != str(reservation_id)):
        raise RuntimeError("Daily budget not exclusive")
    state = {"status": "started", "selected_codes": codes, "completed_codes": [],
             "budget_day": plan["budget_day"], "trade_date": plan["trade_date"],
             "notification": {"status": "not_requested"}}
    write_json(previous_path, state)
    payload["llm_enabled"] = True
    try:
        completed, report = runner(codes)
        if any(code not in codes for code in completed):
            raise ValueError("Unexpected analysis code")
        state["completed_codes"] = list(dict.fromkeys(completed))
        state["status"] = "success" if set(completed) == set(codes) and report.strip() else "incomplete"
        if report.strip():
            heading = (f"# 筛选候选 AI 分析 — {plan['trade_date']}\n\n"
                       f"本次选择 {len(codes)} 只，完成 {len(state['completed_codes'])} 只。"
                       f"候选来源为有界技术筛选，分析仅供参考。\n\n{SCOPE_NOTICE}\n\n")
            overview = ["## 本轮候选总览（最多20只）", "",
                        "仅标注已完成的股票做过本轮AI分析，其余仅为技术因子候选；未逐只核验新闻和公告。", ""]
            for candidate in payload.get("candidates", []):
                code = candidate["code"]
                label = "已完成AI分析" if code in completed else "仅因子筛选，未做AI分析"
                overview.append(f"- {code} {safe_text(candidate['name'])}：{label}")
            (folder / "ai_report.md").write_text(
                heading + "\n".join(overview) + "\n\n" + report, encoding="utf-8")
    except Exception as exc:
        state["status"], state["error_type"] = "failed", type(exc).__name__
    write_json(previous_path, state)
    payload["ai_analysis"] = {key: value for key, value in state.items() if key != "notification"}
    write_outputs(folder, payload)
    print(f"AI 分析：{state['status']}；完成 {len(state['completed_codes'])}/{len(codes)} 只。", flush=True)
    return 0 if state["status"] == "success" else 1


def send_report(result_path: Path, *, sender_factory=None) -> int:
    folder = result_path.parent
    state_path = folder / "ai_result.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    plan = json.loads((folder / "ai_plan.json").read_text(encoding="utf-8"))
    if state["status"] != "success" or notification_gate(payload, market_clock(), scheduled=plan["scheduled"]) != "send":
        print("AI 报告未完整完成或日期已过期，不发送。")
        return 1
    codes = select_codes(payload)
    if (state.get('selected_codes') != codes or set(state.get('completed_codes', [])) != set(codes)
            or plan.get('codes') != codes):
        print('AI报告与当前主板候选不一致，不发送。')
        return 1
    if state["notification"]["status"] in {"submitting", "accepted", "failed_or_unknown"}:
        print("已有 AI 报告推送记录，不自动重发。")
        return 0 if state["notification"]["status"] == "accepted" else 1
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("未读到现有 PUSHPLUS_TOKEN，未发送 AI 报告。")
        return 1
    report = (folder / "ai_report.md").read_text(encoding="utf-8")
    if sender_factory is None:
        from src.notification_sender.pushplus_sender import PushplusSender
        sender_factory = PushplusSender
    state["notification"] = {"status": "submitting"}
    write_json(state_path, state)
    try:
        sender = sender_factory(SimpleNamespace(pushplus_token=token,
            pushplus_topic=os.environ.get("PUSHPLUS_TOPIC", "").strip() or None, pushplus_max_bytes=20000))
        accepted = sender.send_to_pushplus(report, f"DSA 候选 AI 分析 {state['trade_date']}", timeout_seconds=15)
    except Exception:
        accepted = False
    state["notification"] = {"status": "accepted" if accepted else "failed_or_unknown"}
    write_json(state_path, state)
    payload["notification"] = {"status": state["notification"]["status"], "channel": "pushplus",
                               "message": "AI 报告已受理，微信实际送达待确认。" if accepted else "AI 报告推送异常，不自动重试。"}
    payload["notifications_enabled"] = True
    write_outputs(folder, payload)
    print(payload["notification"]["message"], flush=True)
    return 0 if accepted else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "analyze", "send"))
    parser.add_argument("--result", type=Path, default=ROOT / "reports/screening_test/screening_result.json")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--reservation-id", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    try:
        if args.mode == "prepare":
            plan = prepare(args.result, scheduled=args.scheduled)
            return 1 if plan["status"] == "blocked" else 0
        if args.mode == "analyze":
            return analyze(args.result, args.reservation_id)
        return send_report(args.result)
    except Exception as exc:
        print(f"AI 阶段未完成：{type(exc).__name__}。未自动重试；请检查额度记录与结果。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
