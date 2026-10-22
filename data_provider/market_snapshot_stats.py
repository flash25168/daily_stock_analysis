"""Conservative closed-session statistics from the existing screening source."""
import pandas as pd


def aggregate_snapshot(df: pd.DataFrame, expected_date: str) -> dict:
    required = {'SECURITY_CODE', 'CHANGE_RATE', 'DEAL_AMOUNT', 'MAX_TRADE_DATE'}
    if not required.issubset(df.columns):
        raise ValueError('missing snapshot fields')
    if len(df) != df.attrs.get('reported_count') or len(df) < 3000:
        raise ValueError('incomplete source coverage')
    codes = df.SECURITY_CODE.astype(str)
    if not codes.str.fullmatch(r'\d{6}').all() or codes.duplicated().any():
        raise ValueError('invalid or duplicate codes')
    # This output explicitly covers Shanghai/Shenzhen, excluding Beijing.
    frame = df[codes.str.startswith(('60', '68', '00', '30'))].copy()
    if len(frame) < 3000:
        raise ValueError('insufficient Shanghai/Shenzhen coverage')
    amounts = pd.to_numeric(frame.DEAL_AMOUNT, errors='coerce')
    changes = pd.to_numeric(frame.CHANGE_RATE, errors='coerce')
    if ((amounts.notna()) & (~amounts.between(0, 1e14))).any():
        raise ValueError('invalid turnover')
    traded = amounts > 0
    if int(traded.sum()) < 3000 or changes[traded].isna().any() or (~changes[traded].between(-100, 1000)).any():
        raise ValueError('invalid traded rows')
    dates = pd.to_datetime(frame.loc[traded, 'MAX_TRADE_DATE'], errors='coerce')
    if dates.isna().any() or not dates.dt.strftime('%Y-%m-%d').eq(expected_date).all():
        raise ValueError('stale or unknown trade date')
    pct = changes[traded]
    sample = dict(up_count=int((pct > 0).sum()), down_count=int((pct < 0).sum()),
                  flat_count=int((pct == 0).sum()), amount_yi=float(amounts[traded].sum() / 1e8),
                  covered_rows=int(traded.sum()), unknown_amount_rows=int(amounts.isna().sum()),
                  zero_amount_rows=int(amounts.eq(0).sum()), sh_sz_rows=len(frame))
    # Do not fill canonical full-market fields with a partial sample.
    return dict(sample_stats=sample, source='em_datacenter', trade_date=expected_date,
                source_rows=len(df), scope='沪深已覆盖交易样本；非完整市场统计；未知成交额不按零处理')


def fetch_snapshot_stats() -> dict:
    from src.core.trading_calendar import build_market_phase_context
    from src.services.screening.snapshot import fetch_em_datacenter_raw
    clock = build_market_phase_context(market='cn')
    if clock.phase.value not in ('postmarket', 'premarket', 'non_trading') or any(
        code in clock.warnings for code in ('calendar_error', 'calendar_unavailable')
    ):
        raise ValueError('closed-session calendar required')
    return aggregate_snapshot(fetch_em_datacenter_raw(), clock.effective_daily_bar_date.isoformat())
