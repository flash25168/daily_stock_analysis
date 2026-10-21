#!/usr/bin/env python3
"""Manual, factor-only candidate test using DSA's bundled screening engine."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRATEGIES = {"volume_breakout": "放量突破", "shrink_pullback": "缩量回踩"}
FIELDS = ("rank", "code", "name", "price", "change_pct", "final_score", "screen_score",
          "daily_source", "daily_quality_flags", "breakout_20d_pct",
          "volume_ratio_20d", "pullback_to_ma20_pct", "ma_bullish")


def market_clock(now=None) -> dict:
    """Use the existing exchange calendar; never label fail-open dates verified."""
    from src.core.trading_calendar import build_market_phase_context

    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    try:
        context = build_market_phase_context(market="cn", current_time=now)
        reliable = context.phase.value != "unknown" and not any(
            flag in context.warnings for flag in ("calendar_unavailable", "calendar_error"))
        return {"checked_at": now.isoformat(), "phase": context.phase.value,
                "calendar_status": "ok" if reliable else "unknown",
                "expected_daily_date": context.effective_daily_bar_date.isoformat() if reliable else None,
                "closed_session": reliable and context.phase.value in (
                    "postmarket", "premarket", "non_trading")}
    except Exception:
        return {"checked_at": now.isoformat(), "phase": "unknown", "calendar_status": "unknown",
                "expected_daily_date": None, "closed_session": False}


def schedule_gate(clock: dict) -> str:
    """A delayed cron must never send the previous session as today's report."""
    if clock["calendar_status"] != "ok":
        return "calendar_unknown"
    if clock["phase"] == "non_trading":
        return "non_trading_day"
    local_date = datetime.fromisoformat(clock["checked_at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if clock["phase"] != "postmarket" or clock["expected_daily_date"] != local_date:
        return "not_today_after_close"
    return "run"


def parse_trade_date(value) -> str | None:
    """Accept explicit provider session dates, never Unix timestamps or a fetch date."""
    text = str(value if value is not None else "").strip()
    if re.fullmatch(r"\d{8}", text):
        pattern, text = "%Y%m%d", text
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", text):
        pattern, text = "%Y-%m-%d", text[:10]
    else:
        return None
    try:
        return datetime.strptime(text, pattern).date().isoformat()
    except ValueError:
        return None


def history_dates(history) -> dict:
    import pandas as pd

    date_col = next((col for col in ("date", "日期", "trade_date") if col in history), None)
    close_col = next((col for col in ("close", "收盘") if col in history), None)
    evidence = {"daily_trade_date": None, "daily_last_close": None,
                "daily_received_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "daily_date_source": "history_returned_to_pipeline"}
    if date_col is None or close_col is None:
        return evidence
    valid = []
    for day, price in zip(history[date_col], history[close_col]):
        number = pd.to_numeric(price, errors="coerce")
        if pd.isna(number) or not math.isfinite(float(number)) or number <= 0:
            continue
        parsed = parse_trade_date(day)
        if parsed is None:
            evidence["daily_date_invalid"] = True
            continue
        valid.append((parsed, float(number)))
    if valid:
        evidence["daily_trade_date"], evidence["daily_last_close"] = max(valid, key=lambda row: row[0])
    return evidence


def snapshot_dates(cache_path: Path) -> dict:
    """Read the very snapshot used by this run, including its actual provider dates."""
    evidence = {"snapshot_fetched_at": None, "snapshot_original_source": None,
                "dates_by_code": {}, "date_fields_by_code": {}}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        evidence["snapshot_fetched_at"] = payload.get("created_at")
        evidence["snapshot_original_source"] = safe_text(payload.get("metadata", {}).get("snapshot_source"))
        frame = payload["frame"]
        columns = frame["columns"]
        code_col = columns.index("code")
        # Eastmoney's original xuangu query returns MAX_TRADE_DATE with each row.
        # Do not substitute TRADE_DATE: requesting that field can refer to a
        # different dated dataset rather than the current snapshot declaration.
        allowed_fields = (("MAX_TRADE_DATE",) if evidence["snapshot_original_source"] == "em_datacenter"
                          else ("trade_date", "TRADE_DATE", "交易日期", "日期"))
        date_cols = [columns.index(col) for col in allowed_fields
                     if col in columns]
        for row in frame["data"]:
            code = str(row[code_col]).zfill(6)
            values = [parse_trade_date(row[index]) for index in date_cols]
            if values and all(values) and len(set(values)) == 1:
                evidence["dates_by_code"][code] = values[0]
                evidence["date_fields_by_code"][code] = ",".join(columns[index] for index in date_cols)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        pass
    return evidence


def audit_pick_dates(code: str, history: dict, snapshot: dict, clock: dict) -> dict:
    daily_date = history.get("daily_trade_date")
    snapshot_date = snapshot.get("dates_by_code", {}).get(code)
    expected = clock["expected_daily_date"]
    reasons = []
    failed = False
    if history.get("daily_date_invalid"):
        reasons.append("invalid_daily_date")
        failed = True
    if not daily_date:
        reasons.append("daily_date_missing")
    if not snapshot_date:
        reasons.append("snapshot_date_missing")
    if clock["calendar_status"] != "ok":
        reasons.append("calendar_unknown")
    if not clock["closed_session"]:
        reasons.append("not_a_closed_session_test")
    if daily_date and expected and daily_date != expected:
        reasons.append("daily_date_not_expected")
        failed = True
    if snapshot_date and expected and snapshot_date != expected:
        reasons.append("snapshot_date_not_expected")
        failed = True
    if snapshot_date and daily_date and snapshot_date != daily_date:
        reasons.append("snapshot_daily_date_mismatch")
        failed = True
    return {**history, "snapshot_trade_date": snapshot_date,
            "snapshot_trade_date_field": snapshot.get("date_fields_by_code", {}).get(code),
            "snapshot_fetched_at": snapshot["snapshot_fetched_at"],
            "snapshot_original_source": snapshot["snapshot_original_source"],
            "expected_daily_date": expected,
            "date_audit_status": "fail" if failed else ("unknown" if reasons else "pass"),
            "date_audit_reasons": reasons}


def safe_text(value: object) -> str:
    text = str(value or "")
    # Source exception messages can contain request URLs. Never publish them verbatim.
    text = re.sub(r"https?://\S+", "[链接已省略]", text)
    text = re.sub(r"(?i)\b(?:sk-|tvly-)[\w.-]+", "[已隐藏]", text)
    text = re.sub(r"(?i)(?:bearer\s+|(?:api[_-]?key|token|secret|authorization)\s*[=:]\s*)[^\s,;]+",
                  "[已隐藏]", text)
    return text.replace("|", "/").replace("\r", " ").replace("\n", " ")[:1200]


def pick_row(pick) -> dict:
    row = {}
    for field in FIELDS:
        value = getattr(pick, field, None)
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        row[field] = safe_text(value) if isinstance(value, str) else value
    return row


def make_config(cache_dir: Path, daily_limit: int):
    from src.services.screening.config import Config

    # Deliberately do not use Config.from_env(): model and remote-analyzer settings
    # must not leak into this entry point, even on an already configured machine.
    return Config(
        data_dir=cache_dir,
        fallback_snapshot_path=cache_dir / "snapshot.last_good.json",
        snapshot_fallback_max_age_hours=0,
        snapshot_cache_ttl_seconds=3600,
        daily_history_cache_dir=cache_dir / "daily_history",
        daily_history_cache_ttl_hours=0,
        daily_enrich_enabled=True,
        daily_enrich_max_candidates=daily_limit,
        daily_fetch_retries=1,
        daily_fetch_max_workers=2,
        post_analyzers=["scorecard"],
        llm_api_key="", llm_channels=[], llm_config_path=None,
        llm_candidate_context_enabled=False,
        industry_provider="none", dsa_api_url="", post_analyzer_url="",
    )


def run_screening(output_dir: Path, *, daily_limit: int = 30, screen_fn=None) -> dict:
    if not 1 <= daily_limit <= 1000:
        raise ValueError("daily_limit must be between 1 and 1000")
    if screen_fn is None:
        from src.services.screening.pipeline import screen as screen_fn

    output_dir.mkdir(parents=True, exist_ok=True)
    # Isolate evidence to this invocation, while reusing one snapshot across
    # its two strategies. A failed cache write must not expose a previous run.
    config = make_config(output_dir / "cache" / uuid4().hex, daily_limit)
    run_clock = market_clock()
    results = []
    for strategy, label in STRATEGIES.items():
        print(f"开始：{label}；日线核验上限 {daily_limit}", flush=True)
        histories = {}
        history_lock = threading.Lock()

        def audited_history_fetcher(code, **kwargs):
            from src.services.screening.daily import fetch_daily_history
            try:
                history = fetch_daily_history(code, **kwargs)
            except Exception:
                with history_lock:
                    histories[str(code).zfill(6)] = {"daily_trade_date": None,
                                                   "daily_date_source": "history_fetch_failed"}
                raise
            evidence = history_dates(history)
            with history_lock:
                histories[str(code).zfill(6)] = evidence
            return history

        try:
            result = screen_fn(
                strategy, market="cn", max_output=5, use_llm=False,
                collect_llm_candidate_context=False, candidate_context_providers=[],
                post_analyzers=["scorecard"], deep_analysis=False,
                daily_enrich=True, daily_enrich_max_candidates=daily_limit,
                explain_filters=True, context=None, config=config,
                daily_history_fetcher=audited_history_fetcher,
            )
            if result.llm_ranked or result.llm_attempted_models or result.deep_analysis_requested:
                raise RuntimeError("Unexpected model execution")
            if result.snapshot_count <= 0:
                raise RuntimeError("Empty market snapshot")
            # A source outage must not masquerade as a valid zero-candidate result.
            if result.daily_enriched and result.daily_enrich_count == 0:
                raise RuntimeError("All attempted daily histories failed")
            clock = market_clock()
            snapshot = snapshot_dates(config.fallback_snapshot_path)
            accepted, rejected, date_rejected = [], 0, []
            audits = {code: audit_pick_dates(code, evidence, snapshot, clock)
                      for code, evidence in histories.items()}
            for pick in result.picks:
                code = str(pick.code).zfill(6)
                audit = audits.get(code) or audit_pick_dates(code, {}, snapshot, clock)
                if audit["date_audit_status"] == "fail":
                    date_rejected.append({"code": code, **audit})
                    continue
                flags = str(pick.daily_quality_flags or "").lower()
                if any(word in flags for word in (
                    "stale", "fetch_failed", "short_history", "missing_", "incomplete_",
                    "negative_volume", "invalid_ohlc", "non_positive_price",
                )):
                    rejected += 1
                    continue
                accepted.append({**pick_row(pick), **audit})
            statuses = [row["date_audit_status"] for row in audits.values()]
            statuses.extend(row["date_audit_status"] for row in accepted if row["code"] not in audits)
            session_changed = run_clock["expected_daily_date"] != clock["expected_daily_date"]
            if session_changed:
                statuses.append("fail")
            audit_status = ("fail" if "fail" in statuses else
                            "unknown" if not statuses or "unknown" in statuses else "pass")
            results.append({
                "strategy": strategy, "label": label, "status": "ok",
                "strategy_version": result.strategy_version,
                "snapshot_count": result.snapshot_count,
                "snapshot_source": safe_text(result.snapshot_source),
                "after_filter_count": result.after_filter_count,
                "daily_success_count": result.daily_enrich_count,
                "daily_limit": daily_limit,
                "quality_rejected_from_top5": rejected,
                "date_audit_status": audit_status,
                "date_audit_counts": dict(Counter(statuses)),
                "run_crossed_session_boundary": session_changed,
                "date_rejected_from_top5": date_rejected,
                "snapshot_fetched_at": snapshot["snapshot_fetched_at"],
                "snapshot_original_source": snapshot["snapshot_original_source"],
                "date_audit": [{"code": code, **audit} for code, audit in audits.items()],
                "notes": [safe_text(item) for item in result.degradation],
                "picks": accepted,
            })
            print(f"完成：{label}；快照 {result.snapshot_count} 条；有效输出 {len(accepted)} 只", flush=True)
        except Exception as exc:
            # Do not echo exception messages: they can contain provider credentials.
            results.append({"strategy": strategy, "label": label, "status": "error",
                            "error_type": type(exc).__name__, "picks": []})
            print(f"失败：{label}（{type(exc).__name__}）；不是正常的零候选结果", flush=True)

    # Factor scores from different strategies are not directly comparable.
    # Interleave within-strategy ranks, deduplicate, and retain both signal labels.
    candidates = {}
    for rank in range(5):
        for result in results:
            picks = result["picks"]
            if rank >= len(picks):
                continue
            row = picks[rank]
            code = str(row["code"])
            if code not in candidates:
                candidates[code] = {"code": code, "name": row["name"], "signals": []}
            candidates[code]["signals"].append({"strategy": result["strategy"],
                                                "rank": row["rank"], "score": row["final_score"],
                                                "daily_trade_date": row.get("daily_trade_date"),
                                                "snapshot_trade_date": row.get("snapshot_trade_date"),
                                                "date_audit_status": row["date_audit_status"]})
    payload = {
        "mode": "factor_only_test", "llm_enabled": False, "notifications_enabled": False,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "date_audit_version": 3,
        "market_clock": run_clock,
        "ready_for_automation": bool(results) and all(
            row["status"] == "ok" and row["date_audit_status"] == "pass" for row in results),
        "status": "ok" if all(r["status"] == "ok" for r in results) else "incomplete",
        "coverage": "全市场快照初筛；每策略仅核验有界候选日线，非逐股全量日线扫描",
        "daily_limit_per_strategy": daily_limit,
        "strategies": results, "candidates": list(candidates.values()),
    }
    write_outputs(output_dir, payload)
    return payload


def write_outputs(output_dir: Path, payload: dict) -> None:
    (output_dir / "screening_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    codes = [row["code"] for row in payload["candidates"]]
    (output_dir / "candidate_codes.txt").write_text(
        ",".join(codes) + "\n", encoding="utf-8")
    with (output_dir / "candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "name", "strategy", "rank_in_strategy", "factor_score",
                         "daily_trade_date", "snapshot_trade_date", "date_audit_status"])
        for row in payload["candidates"]:
            for signal in row["signals"]:
                # Prevent spreadsheet formula interpretation of upstream text.
                name = row["name"]
                if name.lstrip().startswith(("=", "+", "-", "@")):
                    name = "'" + name
                writer.writerow([row["code"], name, STRATEGIES[signal["strategy"]],
                                 signal["rank"], signal["score"], signal["daily_trade_date"],
                                 signal["snapshot_trade_date"], signal["date_audit_status"]])
    notification = payload.get("notification", {})
    lines = ["# A股自动选股（因子筛选）", "", f"生成时间（北京时间）：{payload['created_at']}", "",
             "仅执行本地因子筛选；未调用模型、未搜索新闻。",
             f"通知状态：{notification.get('status', 'not_requested')}；"
             f"{notification.get('message', '本次未请求发送通知。')}", "",
             "全市场快照初筛，每策略最多核验 "
             f"{payload['daily_limit_per_strategy']} 只候选的日线；不是全市场逐股日线扫描。",
             "沿用仓库选股引擎策略阈值，不等同于根目录 strategies 中的 AI 自然语言规则。",
             "快照源返回成功不保证覆盖全部上市股票，也不保证行情交易日期正确；请核对源和时间。",
             "盘中或休市运行仅作测试；未核实新闻、公告、财务及筹码风险。候选不是买入指令。", "",
             f"日期核验版本：{payload['date_audit_version']}；"
             f"当前市场阶段：{payload['market_clock']['phase']}；"
             f"预期已收盘日线日期：{payload['market_clock']['expected_daily_date'] or '未知'}。",
             f"是否具备日期条件以接入每日自动运行：{'是' if payload['ready_for_automation'] else '否'}。",
             "抓取时间不是交易日期；未知日期不视为通过。unknown 候选只用于诊断，fail 候选移除且不补位。", "",
             "快照交易日按来源原始字段记录；东方财富 em_datacenter 使用 MAX_TRADE_DATE。",
             "日期 pass 仅表示来源声明日期与日线/日历一致，不证明逐股实际最后成交日、价格准确或数据完整。", "",
             f"运行状态：{payload['status']}；去重候选数：{len(codes)}（最多 10 只）", ""]
    if payload.get("skip_reason"):
        lines += [f"本次跳过：{payload['skip_reason']}；未抓取行情、未发送通知。", ""]
    for result in payload["strategies"]:
        lines += [f"## {result['label']}", ""]
        if result["status"] != "ok":
            lines += [f"执行失败（{result['error_type']}），不能解释为没有符合条件的股票。", ""]
            continue
        lines += [f"策略版本：{result['strategy_version']}；快照源：{result['snapshot_source']}；"
                  f"快照 {result['snapshot_count']} 条；日线成功 {result['daily_success_count']} 条；"
                  f"硬筛后 {result['after_filter_count']} 条。", ""]
        lines += [f"日期核验：{result['date_audit_status']}；原快照来源："
                  f"{result['snapshot_original_source'] or '未知'}；快照抓取时间："
                  f"{result['snapshot_fetched_at'] or '未知'}（不是交易日期）。",
                  f"Top 5 中因日期失败移除：{len(result['date_rejected_from_top5'])} 只；"
                  "逐股日线/快照日期及原因详见 JSON 的 date_audit。", ""]
        if result["run_crossed_session_boundary"]:
            lines += ["运行跨越了已完成交易日的边界，不能作为同一交易日的完整结果。", ""]
        if result["picks"]:
            lines += ["| 代码 | 名称 | 策略内因子分 | 行情价 | 日线交易日 | 快照交易日 | 日期核验 |",
                      "|---|---|---:|---:|---|---|---|"]
            for row in result["picks"]:
                lines.append(f"| {row['code']} | {row['name']} | {row['final_score']} | "
                             f"{row['price']} | {row.get('daily_trade_date') or '未知'} | "
                             f"{row.get('snapshot_trade_date') or '未知'} | {row['date_audit_status']} |")
        else:
            lines.append("本次核验范围内没有可输出候选，未放宽条件凑数。")
        lines += ["", f"Top 5 中因陈旧/失败/样本不足而移除：{result['quality_rejected_from_top5']} 只。",
                  "", "筛选记录（包含数据源降级、缺失和过滤情况）：", ""]
        lines += [f"- {note}" for note in result["notes"]]
        lines.append("")
    (output_dir / "screening_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-limit", type=int, default=30,
                        help="Daily-history cap per strategy (1-1000); default: 30")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "screening_test")
    parser.add_argument("--scheduled", action="store_true", help="Only run on a trading day after today's close")
    args = parser.parse_args()
    if not 1 <= args.daily_limit <= 1000:
        parser.error("--daily-limit must be between 1 and 1000")
    # Avoid publishing raw third-party exception logs. Output a bounded summary instead.
    logging.basicConfig(level=logging.CRITICAL)
    if args.scheduled:
        clock = market_clock()
        gate = schedule_gate(clock)
        if gate != "run":
            args.output_dir.mkdir(parents=True, exist_ok=True)
            payload = {"mode": "factor_only_daily", "created_at": clock["checked_at"],
                       "status": "error" if gate == "calendar_unknown" else "skipped",
                       "skip_reason": gate, "market_clock": clock, "date_audit_version": 3,
                       "ready_for_automation": False, "llm_enabled": False,
                       "notifications_enabled": False, "daily_limit_per_strategy": args.daily_limit,
                       "strategies": [], "candidates": []}
            write_outputs(args.output_dir, payload)
            print(f"定时任务未执行选股：{gate}")
            return 1 if gate == "calendar_unknown" else 0
    payload = run_screening(args.output_dir, daily_limit=args.daily_limit)
    if args.scheduled and not payload["ready_for_automation"]:
        print("日期核验未全部通过；本次定时任务不发送候选名单。")
        return 1
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
