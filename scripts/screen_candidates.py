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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRATEGIES = {"volume_breakout": "放量突破", "shrink_pullback": "缩量回踩"}
FIELDS = ("rank", "code", "name", "price", "change_pct", "final_score", "screen_score",
          "daily_source", "daily_quality_flags", "breakout_20d_pct",
          "volume_ratio_20d", "pullback_to_ma20_pct", "ma_bullish")


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
    config = make_config(output_dir / "cache", daily_limit)
    results = []
    for strategy, label in STRATEGIES.items():
        print(f"开始：{label}；日线核验上限 {daily_limit}", flush=True)
        try:
            result = screen_fn(
                strategy, market="cn", max_output=5, use_llm=False,
                collect_llm_candidate_context=False, candidate_context_providers=[],
                post_analyzers=["scorecard"], deep_analysis=False,
                daily_enrich=True, daily_enrich_max_candidates=daily_limit,
                explain_filters=True, context=None, config=config,
            )
            if result.llm_ranked or result.llm_attempted_models or result.deep_analysis_requested:
                raise RuntimeError("Unexpected model execution")
            if result.snapshot_count <= 0:
                raise RuntimeError("Empty market snapshot")
            # A source outage must not masquerade as a valid zero-candidate result.
            if result.daily_enriched and result.daily_enrich_count == 0:
                raise RuntimeError("All attempted daily histories failed")
            accepted, rejected = [], 0
            for pick in result.picks:
                flags = str(pick.daily_quality_flags or "").lower()
                if any(word in flags for word in (
                    "stale", "fetch_failed", "short_history", "missing_", "incomplete_",
                    "negative_volume", "invalid_ohlc", "non_positive_price",
                )):
                    rejected += 1
                    continue
                accepted.append(pick_row(pick))
            results.append({
                "strategy": strategy, "label": label, "status": "ok",
                "strategy_version": result.strategy_version,
                "snapshot_count": result.snapshot_count,
                "snapshot_source": safe_text(result.snapshot_source),
                "after_filter_count": result.after_filter_count,
                "daily_success_count": result.daily_enrich_count,
                "daily_limit": daily_limit,
                "quality_rejected_from_top5": rejected,
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
                                                "rank": row["rank"], "score": row["final_score"]})
    payload = {
        "mode": "factor_only_test", "llm_enabled": False, "notifications_enabled": False,
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
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
        writer.writerow(["code", "name", "strategy", "rank_in_strategy", "factor_score"])
        for row in payload["candidates"]:
            for signal in row["signals"]:
                # Prevent spreadsheet formula interpretation of upstream text.
                name = row["name"]
                if name.lstrip().startswith(("=", "+", "-", "@")):
                    name = "'" + name
                writer.writerow([row["code"], name, STRATEGIES[signal["strategy"]],
                                 signal["rank"], signal["score"]])
    lines = ["# A股自动选股测试", "", f"生成时间（北京时间）：{payload['created_at']}", "",
             "仅执行本地因子筛选；未调用模型、未搜索新闻、未发送通知。", "",
             "全市场快照初筛，每策略最多核验 "
             f"{payload['daily_limit_per_strategy']} 只候选的日线；不是全市场逐股日线扫描。",
             "沿用仓库选股引擎策略阈值，不等同于根目录 strategies 中的 AI 自然语言规则。",
             "快照源返回成功不保证覆盖全部上市股票，也不保证行情交易日期正确；请核对源和时间。",
             "盘中或休市运行仅作测试；未核实新闻、公告、财务及筹码风险。候选不是买入指令。", "",
             f"运行状态：{payload['status']}；去重候选数：{len(codes)}（最多 10 只）", ""]
    for result in payload["strategies"]:
        lines += [f"## {result['label']}", ""]
        if result["status"] != "ok":
            lines += [f"执行失败（{result['error_type']}），不能解释为没有符合条件的股票。", ""]
            continue
        lines += [f"策略版本：{result['strategy_version']}；快照源：{result['snapshot_source']}；"
                  f"快照 {result['snapshot_count']} 条；日线成功 {result['daily_success_count']} 条；"
                  f"硬筛后 {result['after_filter_count']} 条。", ""]
        if result["picks"]:
            lines += ["| 代码 | 名称 | 策略内因子分 | 行情价 | 日线来源 | 质量标记 |",
                      "|---|---|---:|---:|---|---|"]
            for row in result["picks"]:
                lines.append(f"| {row['code']} | {row['name']} | {row['final_score']} | "
                             f"{row['price']} | {row['daily_source']} | {row['daily_quality_flags']} |")
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
    args = parser.parse_args()
    if not 1 <= args.daily_limit <= 1000:
        parser.error("--daily-limit must be between 1 and 1000")
    # Avoid publishing raw third-party exception logs. Output a bounded summary instead.
    logging.basicConfig(level=logging.CRITICAL)
    payload = run_screening(args.output_dir, daily_limit=args.daily_limit)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
