"""Public HTTP contract, closed-session validation, fallback and actual rendering."""
import copy
import json
from datetime import date
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pandas as pd
import pytest

from data_provider.fundamental_adapter import AkshareFundamentalAdapter


SESSIONS = pd.bdate_range(end='2026-10-23', periods=10)


@pytest.fixture
def public_rows():
    return [{'opendate': day.date().isoformat(), 'r0_net': '200000000',
             'r1_net': '-50000000', 'netamount': '999', 'ratioamount': '0.99'}
            for day in reversed(SESSIONS)]


@pytest.fixture
def clock():
    with patch('src.core.trading_calendar.build_market_phase_context', return_value=SimpleNamespace(
        phase=SimpleNamespace(value='non_trading'), warnings=[],
        effective_daily_bar_date=date(2026, 10, 23))), \
         patch('src.core.trading_calendar.get_trading_dates', return_value=SESSIONS):
        yield


def response(rows):
    return BytesIO(json.dumps(rows).encode())


def test_http_contract_and_full_adapter_do_not_wait_for_failed_sector(clock, public_rows):
    requests = []

    def http(request, timeout):
        requests.append((request.full_url, timeout))
        return response(public_rows)

    with patch('urllib.request.urlopen', side_effect=http):
        result = AkshareFundamentalAdapter().get_capital_flow('000858')
    assert len(requests) == 1
    query = parse_qs(urlparse(requests[0][0]).query)
    assert query['daima'] == ['sz000858']
    assert query['num'] == ['10']
    assert requests[0][1] == 4
    assert result['stock_flow'] == {
        'provider': 'sina', 'calculation': 'super_large_plus_large',
        'identity_validation': 'request_scoped', 'trade_date': '2026-10-23',
        'currency': 'CNY', 'main_net_inflow': 150000000,
        'inflow_5d': 750000000, 'inflow_10d': 1500000000,
    }
    assert result['source_chain'] == ['capital_stock:_capital_history_sina']
    assert result['sector_rankings'] == {'top': [], 'bottom': []}


@pytest.mark.parametrize('amount', [None, 'NaN', 'Infinity', True, '12%', '', 'missing'])
def test_bad_latest_amount_cannot_become_zero_or_a_ratio(clock, public_rows, amount):
    public_rows[0]['r0_net'] = amount
    with patch('urllib.request.urlopen', return_value=response(public_rows)):
        with pytest.raises(ValueError):
            AkshareFundamentalAdapter()._capital_history_sina('000858', 'sz')


@pytest.mark.parametrize('value', [0, -200000000])
def test_zero_and_negative_amounts_are_preserved(clock, public_rows, value):
    public_rows[0].update(r0_net=value, r1_net=0)
    with patch('urllib.request.urlopen', return_value=response(public_rows)):
        frame = AkshareFundamentalAdapter()._capital_history_sina('000858', 'sz')
    assert frame.iloc[-1]['主力净流入-净额'] == value


@pytest.mark.parametrize('bad', ['stale', 'future', 'weekend', 'duplicate', 'symbol', 'code', 'shape'])
def test_stale_mismatched_or_malformed_payload_is_rejected(clock, public_rows, bad):
    if bad == 'stale':
        public_rows = public_rows[1:]
    elif bad in ('future', 'weekend'):
        public_rows[0]['opendate'] = '2026-10-26' if bad == 'future' else '2026-10-24'
    elif bad == 'duplicate':
        public_rows[1] = copy.deepcopy(public_rows[0])
    elif bad == 'symbol':
        public_rows[0]['symbol'] = 'sh600519'
    elif bad == 'code':
        public_rows[0]['code'] = '600519'
    else:
        public_rows = {'error': 'not a list'}
    with patch('urllib.request.urlopen', return_value=response(public_rows)):
        with pytest.raises(ValueError):
            AkshareFundamentalAdapter()._capital_history_sina('000858', 'sz')


def test_missing_or_invalid_session_does_not_shift_window(clock, public_rows):
    adapter = AkshareFundamentalAdapter()
    for rows in (public_rows[:3] + public_rows[4:],
                 [dict(row, r0_net=None) if i == 2 else row for i, row in enumerate(public_rows)]):
        with patch('urllib.request.urlopen', return_value=response(rows)):
            frame = adapter._capital_history_sina('000858', 'sz')
        assert frame.attrs['inflow_5d'] is None
        assert frame.attrs['inflow_10d'] is None


def test_intraday_and_unreliable_calendar_never_make_network_request():
    adapter = AkshareFundamentalAdapter()
    for phase, warnings in [('intraday', []), ('non_trading', ['calendar_unavailable'])]:
        with patch('src.core.trading_calendar.build_market_phase_context', return_value=SimpleNamespace(
                phase=SimpleNamespace(value=phase), warnings=warnings)), \
             patch('urllib.request.urlopen') as http:
            with pytest.raises(ValueError):
                adapter._capital_history_sina('000858', 'sz')
            http.assert_not_called()


def test_sina_failure_reaches_existing_eastmoney_candidate(clock):
    adapter = AkshareFundamentalAdapter()
    with patch.object(adapter, '_capital_history_sina', side_effect=ConnectionError('unavailable')), \
         patch.object(adapter, '_capital_history_em', return_value=pd.DataFrame(
             {'日期': ['2026-10-23'], '主力净流入-净额': [123]})):
        frame, source, errors = adapter._call_df_candidates([
            ('_capital_history_sina', {'stock': '000858', 'market': 'sz'}),
            ('_capital_history_em', {'stock': '000858', 'market': 'sz'}),
        ])
    assert source == '_capital_history_em'
    assert frame.iloc[0]['主力净流入-净额'] == 123
    assert errors == ['_capital_history_sina:ConnectionError']


@pytest.mark.parametrize('language', ['zh', 'en'])
def test_report_renderer_exposes_provider_definition_date_units_and_na(language):
    from src.notification import NotificationService
    service = object.__new__(NotificationService)
    result = SimpleNamespace(report_language=language, fundamental_context={
        'capital_flow': {'status': 'ok', 'data': {'stock_flow': {
            'provider': 'sina', 'trade_date': '2026-10-23', 'main_net_inflow': -150000000,
            'inflow_5d': 0, 'inflow_10d': None}}}})
    lines = []
    service._append_fundamental_blocks(lines, result)
    text = '\n'.join(lines)
    assert '2026-10-23' in text and 'N/A' in text
    assert ('Sina' if language == 'en' else '新浪财经') in text
    assert ('Eastmoney' if language == 'en' else '东方财富') in text
    assert '-1.50' in text
