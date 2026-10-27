"""2000-point Tushare disclosures, normalized to the existing report contract."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


def disclosed(frame, code, today, period=None):
    """Accept only this issuer's disclosed consolidated statements."""
    required = {'ts_code', 'end_date', 'ann_date'}
    if not isinstance(frame, pd.DataFrame) or not required.issubset(frame):
        raise ValueError('invalid disclosure schema')
    data = frame.copy()
    data['end_date'] = data.end_date.astype(str)
    data['ann_date'] = data.ann_date.astype(str)
    end = pd.to_datetime(data.end_date, format='%Y%m%d', errors='coerce')
    announcement = pd.to_datetime(data.ann_date, format='%Y%m%d', errors='coerce')
    mask = data.ts_code.eq(code) & end.notna() & announcement.notna()
    mask &= end.le(pd.Timestamp(today)) & announcement.le(pd.Timestamp(today))
    if 'f_ann_date' in data:
        actual = pd.to_datetime(data.f_ann_date, format='%Y%m%d', errors='coerce')
        mask &= actual.notna() & actual.le(pd.Timestamp(today))
    if 'report_type' in data:
        mask &= data.report_type.astype(str).eq('1')
    if period:
        mask &= data.end_date.eq(period)
    return data.loc[mask].sort_values(['end_date', 'ann_date'], ascending=False).drop_duplicates('end_date')


def financial_frame(fetcher, code, today=None):
    today = today or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    ts_code = fetcher._convert_stock_code(code)
    args = dict(ts_code=ts_code, start_date=(today-timedelta(days=550)).strftime('%Y%m%d'),
                end_date=today.strftime('%Y%m%d'))
    income = disclosed(fetcher._call_api_with_rate_limit('income', **args), ts_code, today)
    if income.empty:
        raise ValueError('no disclosed income')
    period = income.iloc[0].end_date
    cash = disclosed(fetcher._call_api_with_rate_limit('cashflow', **args), ts_code, today, period)
    indicators = disclosed(fetcher._call_api_with_rate_limit('fina_indicator', **args), ts_code, today, period)
    if cash.empty or indicators.empty:
        raise ValueError('no matching disclosed period')
    inc, cf, indicator = income.iloc[0], cash.iloc[0], indicators.iloc[0]
    row = {'SECUCODE': ts_code, 'REPORT_DATE': pd.to_datetime(str(period), format='%Y%m%d').isoformat(),
           'NOTICE_DATE': pd.to_datetime(str(inc.ann_date), format='%Y%m%d').isoformat(),
           'TOTALOPERATEREVE': inc.get('total_revenue'), 'PARENTNETPROFIT': inc.get('n_income_attr_p'),
           'NETCASH_OPERATE': cf.get('n_cashflow_act'), 'ROEJQ': indicator.get('roe_waa'),
           'TOTALOPERATEREVETZ': indicator.get('tr_yoy'),
           'PARENTNETPROFITTZ': indicator.get('netprofit_yoy'), 'XSMLL': indicator.get('grossprofit_margin')}
    required_fields = {'TOTALOPERATEREVE', 'PARENTNETPROFIT', 'NETCASH_OPERATE'}
    for key in list(row)[3:]:
        value = row[key]
        number = pd.to_numeric(value, errors='coerce')
        if isinstance(value, bool) or pd.isna(number) or not float('-inf') < float(number) < float('inf'):
            if key in required_fields:
                raise ValueError('incomplete financial totals')
            row[key] = None  # For example, gross margin can be inapplicable to banks.
        else:
            row[key] = float(number)
    return pd.DataFrame([row])


def capital_frame(fetcher, code, expected, trading_dates):
    ts_code = fetcher._convert_stock_code(code)
    frame = fetcher._call_api_with_rate_limit('moneyflow', ts_code=ts_code,
        start_date=(expected-timedelta(days=45)).strftime('%Y%m%d'), end_date=expected.strftime('%Y%m%d'))
    cols = ['buy_lg_amount', 'sell_lg_amount', 'buy_elg_amount', 'sell_elg_amount']
    if not {'ts_code', 'trade_date', *cols}.issubset(frame):
        raise ValueError('invalid moneyflow schema')
    dates = pd.to_datetime(frame.trade_date, format='%Y%m%d', errors='coerce')
    if (not frame.ts_code.eq(ts_code).all() or dates.isna().any() or dates.duplicated().any()
            or dates.gt(pd.Timestamp(expected)).any()):
        raise ValueError('invalid moneyflow identity or dates')
    if frame[cols].apply(lambda col: col.map(lambda value: isinstance(value, bool))).any().any():
        raise ValueError('boolean moneyflow amounts')
    amounts = frame[cols].apply(pd.to_numeric, errors='coerce')
    if amounts.isna().any().any() or not amounts.ge(0).all().all() or not amounts.lt(float('inf')).all().all():
        raise ValueError('invalid moneyflow amounts')
    frame = pd.DataFrame({'日期': dates, '主力净流入-净额':
        (amounts.buy_lg_amount-amounts.sell_lg_amount+amounts.buy_elg_amount-amounts.sell_elg_amount)*10000})
    frame = frame.sort_values('日期')
    expected_dates = [pd.Timestamp(d).date() for d in trading_dates]
    if not expected_dates or frame.empty or frame['日期'].iloc[-1].date() != expected:
        raise ValueError('stale or unverifiable moneyflow')
    # A missing session must not silently turn the 5/10-day windows into longer periods.
    required = expected_dates[-10:]
    if len(required) < 10 or list(frame['日期'].dt.date.tail(10)) != required:
        raise ValueError('incomplete moneyflow window')
    frame.attrs.update(provider='tushare', calculation='large_plus_super_large_net',
                       identity_validation='code_and_trading_sessions')
    return frame
