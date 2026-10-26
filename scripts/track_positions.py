"""Review manually registered positions after close. No model or order API."""
import argparse
from datetime import date
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from scripts.screening_scope import is_mainboard


def positions_from_json(raw):
    rows = json.loads(raw) if raw.strip() else []
    if not isinstance(rows, list) or len(rows) > 10:
        raise ValueError('at most ten manually registered positions')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not is_mainboard(row.get('code')) or row['code'] in seen:
            raise ValueError('invalid or duplicate position')
        seen.add(row['code'])
        date.fromisoformat(row['entry_date'])
        price, shares = row.get('entry_price'), row.get('shares')
        if (isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price)
                or price <= 0 or type(shares) is not int or shares <= 0 or shares % 100):
            raise ValueError('invalid manually recorded cost or shares')
    return rows


def review_position(position, expected, sessions, history):
    entry = date.fromisoformat(position['entry_date'])
    labels = [pd.Timestamp(value).date() for value in sessions]
    if entry not in labels or entry > expected:
        raise ValueError('entry must be a verified completed session')
    held = sum(entry < day <= expected for day in labels)
    if not {'date', 'close'}.issubset(history) or len(history) < 20:
        raise ValueError('insufficient daily history')
    dates = pd.to_datetime(history.date, errors='coerce')
    if dates.isna().any() or dates.duplicated().any():
        raise ValueError('unknown or duplicate history dates')
    data = history.assign(_date=dates).sort_values('_date')
    if data._date.iloc[-1].date() != expected:
        raise ValueError('stale or future daily history')
    if list(data._date.dt.date.tail(20)) != labels[-20:]:
        raise ValueError('incomplete daily sessions')
    close = pd.to_numeric(data.close.tail(20), errors='coerce')
    if close.isna().any() or not close.gt(0).all() or not close.lt(float('inf')).all():
        raise ValueError('invalid daily prices')
    price, ma20 = float(close.iloc[-1]), float(close.mean())
    pnl = (price/position['entry_price']-1)*100
    status = '收盘跌破MA20，请人工复核止损条件' if price < ma20 else '未触发收盘跌破MA20条件'
    if held >= 3:
        status += '；已到第3交易日或更晚，请人工复核是否结束短线观察'
    elif held in (1, 2):
        status += f'；第{held}个交易日复盘'
    else:
        status += '；登记买入当日，尚未进入第1个后续交易日'
    return f"- {position['code']}：{status}；收盘{price:.2f}，MA20 {ma20:.2f}，按登记成本变动{pnl:+.2f}%（未计费用）。"


def save_plan(folder, plan):
    (folder/'review_plan.json').write_text(json.dumps(plan), encoding='utf8')
    output = os.getenv('GITHUB_OUTPUT')
    if output:
        with open(output, 'a', encoding='utf8') as handle:
            handle.write(f"send={'true' if plan['send'] else 'false'}\nname={plan.get('name', 'no-positions')}\n")


def prepare(folder, raw, fetch=None, clock=None, calendar=None):
    from src.core.trading_calendar import build_market_phase_context, get_trading_dates
    from src.services.screening.daily import fetch_daily_history
    from datetime import timedelta
    positions = positions_from_json(raw)
    folder.mkdir(parents=True, exist_ok=True)
    plan = {'send': False}
    if positions:
        context = clock or build_market_phase_context(market='cn')
        if (context.phase.value not in ('postmarket', 'non_trading', 'premarket')
                or any(x in context.warnings for x in ('calendar_error', 'calendar_unavailable'))):
            raise ValueError('unverified closed session')
        expected = context.effective_daily_bar_date
        if context.phase.value != 'postmarket':
            plan['reason'] = 'not_postmarket'
            save_plan(folder, plan)
            print('非交易日收盘后，跳过持仓提醒。')
            return plan
        if any(date.fromisoformat(p['entry_date']) > expected for p in positions):
            raise ValueError('future entry date')
        start = min(date.fromisoformat(p['entry_date']) for p in positions)
        sessions = (calendar or get_trading_dates)('cn', min(start, expected-timedelta(days=60)), expected)
        if sessions is None or len(sessions) < 20:
            raise ValueError('trading calendar unavailable')
        lines = ['# 人工持仓1—3交易日复盘', '', f'交易日：{expected}',
                 '仅根据手动登记记录提醒；不是自动下单、卖出指令或收益保证。', '']
        eligible_count = 0
        for position in positions:
            entry = date.fromisoformat(position['entry_date'])
            session_dates = [pd.Timestamp(day).date() for day in sessions]
            age = sum(entry < day <= expected for day in session_dates)
            if entry in session_dates and (age == 0 or age > 3):
                continue
            eligible_count += 1
            try:
                history = (fetch or fetch_daily_history)(position['code'], source='auto', retries=1)
                lines.append(review_position(position, expected, sessions, history))
            except Exception:
                lines.append(f"- {position['code']}：数据或登记日期无法核实，不能判断止损，请人工核对。")
        (folder/'position_review.md').write_text('\n'.join(lines), encoding='utf8')
        # One reminder per closed session, independent of AI and candidate results.
        plan = {'send': eligible_count > 0, 'date': str(expected), 'name': f'position-review-sent-{expected}',
                'run_id': os.getenv('GITHUB_RUN_ID', '')}
    save_plan(folder, plan)
    print(f"人工持仓复盘：登记{len(positions)}条；{'已生成复盘（不输出成本）' if plan['send'] else '无持仓，不发送'}。")
    return plan


def send(folder, reservation, api=None, sender=None):
    from scripts.screening_ai import api_json
    api = api or api_json
    plan = json.loads((folder/'review_plan.json').read_text(encoding='utf8'))
    if not plan.get('send') or not str(reservation).isdigit():
        raise ValueError('missing reminder reservation')
    claims = api('artifacts', params={'name': plan['name'], 'per_page': 100})
    records = claims.get('artifacts', [])
    if (claims.get('total_count') != 1 or len(records) != 1
            or str(records[0].get('id')) != str(reservation)
            or str(records[0].get('workflow_run', {}).get('id')) != plan['run_id']
            or records[0].get('expired') is not False):
        raise ValueError('reminder already reserved or reservation unverified')
    from scripts.screen_candidates import market_clock
    clock = market_clock()
    if clock['phase'] != 'postmarket' or clock['expected_daily_date'] != plan['date']:
        raise ValueError('reminder expired')
    attempted = folder/'send_attempted'
    if attempted.exists():
        raise ValueError('reminder already attempted')
    token = os.getenv('PUSHPLUS_TOKEN', '').strip()
    if not token:
        raise ValueError('missing PushPlus configuration')
    if sender is None:
        from types import SimpleNamespace
        from src.notification_sender.pushplus_sender import PushplusSender
        sender = PushplusSender(SimpleNamespace(pushplus_token=token,
            pushplus_topic=os.getenv('PUSHPLUS_TOPIC') or None, pushplus_max_bytes=20000))
    attempted.write_text('started', encoding='utf8')
    accepted = sender.send_to_pushplus((folder/'position_review.md').read_text(encoding='utf8'),
        f"DSA人工持仓复盘 {plan['date']}", timeout_seconds=15)
    if not accepted:
        raise RuntimeError('PushPlus acceptance unknown; do not automatically retry')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'send'])
    parser.add_argument('--reservation-id', default='')
    args = parser.parse_args()
    folder = ROOT/'reports/position_review'
    try:
        if args.mode == 'prepare':
            prepare(folder, os.getenv('SCREENING_POSITIONS_JSON', ''))
        else:
            send(folder, args.reservation_id)
        return 0
    except Exception as exc:
        print(f'持仓复盘未完成：{type(exc).__name__}；未自动重试。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
