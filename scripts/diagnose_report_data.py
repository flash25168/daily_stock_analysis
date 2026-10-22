"""Read-only data diagnostics. Never initializes a model or sends notifications."""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VALIDATION_REASONS = {
    'missing snapshot fields', 'incomplete source coverage',
    'invalid or duplicate codes', 'insufficient Shanghai/Shenzhen coverage',
    'invalid turnover', 'invalid traded rows', 'stale or unknown trade date',
    'closed-session calendar required',
}


def safe_failure(exc):
    # Only our finite validation vocabulary is printable, never upstream errors/URLs.
    return str(exc) if type(exc) is ValueError and str(exc) in VALIDATION_REASONS else type(exc).__name__


def error_category(value):
    text = str(value).lower()
    for category, words in (
        ('stage_budget_exhausted', ('stage timeout', 'budget exhausted')),
        ('timeout', ('timeout', 'timed out', '超时')),
        ('connection_failure', ('connection', 'remote', 'disconnect', '连接')),
        ('authentication_or_permission', ('unauthorized', 'forbidden', 'permission', '权限', '401', '403')),
        ('rate_limited', ('429', 'rate limit', '限流')),
        ('response_parse_failure', ('json', 'expecting value', 'decode')),
        ('not_supported', ('not supported', 'not_supported', '不支持')),
        ('empty_result', ('empty', 'no data', '无数据', '空结果')),
    ):
        if any(word in text for word in words):
            return category
    return 'unclassified'


def missing_amount_details(frame):
    import pandas as pd
    amounts = pd.to_numeric(frame.DEAL_AMOUNT, errors='coerce')
    bad = frame.loc[~amounts.between(0, 1e14)]
    rows = []
    for _, row in bad.head(30).iterrows():
        record = {'suspension_verified': False}
        # Public market fields only; no arbitrary payload/error strings.
        for key in ('SECURITY_CODE', 'SECURITY_NAME_ABBR', 'MAX_TRADE_DATE',
                    'NEW_PRICE', 'CHANGE_RATE', 'DEAL_AMOUNT', 'VOLUME',
                    'TRADE_STATUS', 'TRADE_STATE', 'SUSPEND_FLAG'):
            if key in frame.columns:
                value = row[key]
                record[key] = None if pd.isna(value) else str(value)[:80]
        rows.append(record)
    return {'count': len(bad), 'sample_limit': 30, 'rows': rows,
            'interpretation': 'Missing amount is unknown, not verified suspension or zero turnover.'}


def summarize_block(block):
    if not isinstance(block, dict):
        return {'status': 'missing', 'populated_fields': []}
    data = block.get('data') or {}
    keys = []
    if isinstance(data, dict):
        keys = [k for k, v in data.items() if isinstance(k, str) and k.replace('_', '').isalnum()
                and v is not None and not isinstance(v, (dict, list)) and str(v).strip() not in ('', '-', 'nan', 'None')]
    status = block.get('status')
    return {'status': status if status in ('ok', 'partial', 'failed', 'not_supported', 'timeout', 'success', 'empty', 'disabled') else 'unknown',
            'populated_fields': sorted(keys), 'error_count': len(block.get('errors') or []),
            'source_count': len(block.get('source_chain') or []),
            'error_categories': sorted(set(error_category(item) for item in (block.get('errors') or []))),
            'sources': [summarize_source(item) for item in (block.get('source_chain') or []) if isinstance(item, dict)]}


def summarize_source(item):
    result = {}
    for key in ('source', 'provider', 'status'):
        value = item.get(key)
        if isinstance(value, str) and value.replace('_', '').replace('-', '').isalnum() and len(value) <= 60:
            result[key] = value
    for key in ('elapsed_ms', 'cost_ms', 'duration_ms'):
        if isinstance(item.get(key), (int, float)):
            result[key] = item[key]
    for key in ('error', 'reason', 'message'):
        if item.get(key):
            result[key + '_category'] = error_category(item[key])
    return result


def diagnose():
    import pandas as pd
    from src.core.trading_calendar import build_market_phase_context
    from src.services.screening.snapshot import fetch_em_datacenter_raw
    from data_provider.market_snapshot_stats import aggregate_snapshot
    from data_provider.base import DataFetcherManager
    from src.config import get_config

    output = {'generated_at': datetime.now().isoformat(), 'model_called': False,
              'notification_sent': False, 'snapshot': {}, 'fundamentals': {}}
    snap = output['snapshot']
    try:
        clock = build_market_phase_context(market='cn')
        expected = clock.effective_daily_bar_date.isoformat()
        snap['phase'] = clock.phase.value
        snap['expected_date'] = expected
        snap['calendar_reliable'] = not any(k in clock.warnings for k in ('calendar_error', 'calendar_unavailable'))
        frame = fetch_em_datacenter_raw()
        original = frame
        snap['rows'] = len(frame)
        snap['reported_count'] = frame.attrs.get('reported_count')
        required = ['SECURITY_CODE', 'CHANGE_RATE', 'DEAL_AMOUNT', 'MAX_TRADE_DATE']
        snap['missing_columns'] = [k for k in required if k not in frame.columns]
        if 'SECURITY_CODE' in frame:
            codes = frame.SECURITY_CODE.astype(str)
            snap['duplicate_codes'] = int(codes.duplicated().sum())
            frame = frame[codes.str.startswith(('60', '68', '00', '30'))].copy()
        snap['sh_sz_rows'] = len(frame)
        if 'DEAL_AMOUNT' in frame:
            amounts = pd.to_numeric(frame.DEAL_AMOUNT, errors='coerce')
            snap['amount_missing'] = int(amounts.isna().sum())
            snap['amount_invalid'] = int((~amounts.between(0, 1e14)).sum())
            snap['missing_amount_details'] = missing_amount_details(frame)
            active = amounts.gt(0)
            snap['traded_rows'] = int(active.sum())
            if 'MAX_TRADE_DATE' in frame:
                dates = pd.to_datetime(frame.loc[active, 'MAX_TRADE_DATE'], errors='coerce')
                snap['date_missing_traded'] = int(dates.isna().sum())
                snap['date_mismatch_traded'] = int((dates.notna() & dates.dt.strftime('%Y-%m-%d').ne(expected)).sum())
                snap['date_counts'] = {str(k): int(v) for k, v in dates.dt.strftime('%Y-%m-%d').value_counts().head(5).items()}
            if 'CHANGE_RATE' in frame:
                pct = pd.to_numeric(frame.loc[active, 'CHANGE_RATE'], errors='coerce')
                snap['change_invalid_traded'] = int((~pct.between(-100, 1000)).sum())
        # Validate original unfiltered frame, exactly as the production fallback does.
        if not snap['calendar_reliable'] or snap['phase'] not in ('postmarket', 'premarket', 'non_trading'):
            raise ValueError('closed-session calendar required')
        aggregate_snapshot(original, expected)
        snap['validation'] = 'pass'
    except Exception as exc:
        snap['validation'] = safe_failure(exc)

    config = get_config()
    output['fundamental_settings'] = {'enabled': config.enable_fundamental_pipeline,
        'stage_budget_seconds': config.fundamental_stage_timeout_seconds,
        'fetch_timeout_seconds': config.fundamental_fetch_timeout_seconds}
    manager = DataFetcherManager()
    for code in ('600519', '000858'):
        try:
            context = manager.get_fundamental_context(code, budget_seconds=config.fundamental_stage_timeout_seconds)
            output['fundamentals'][code] = {key: summarize_block(context.get(key)) for key in
                ('valuation', 'growth', 'earnings', 'institution', 'capital_flow', 'dragon_tiger', 'boards')}
        except Exception as exc:
            output['fundamentals'][code] = {'failure': safe_failure(exc)}
    return output


def main():
    logging.disable(logging.CRITICAL)
    target = ROOT / 'reports/data_diagnostics'
    target.mkdir(parents=True, exist_ok=True)
    try:
        result = diagnose()
    except Exception as exc:
        result = {'diagnostic_failure': safe_failure(exc), 'model_called': False, 'notification_sent': False}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    (target / 'diagnostic.json').write_text(text, encoding='utf-8')
    (target / 'diagnostic.md').write_text('# 数据诊断（不调用模型、不发送通知）\n\n```json\n' + text + '\n```\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
