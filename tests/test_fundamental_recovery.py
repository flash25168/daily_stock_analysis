"""Offline regressions for partial disclosures and independently bounded fallbacks."""
import time
import json
import sys
import unittest
from threading import BoundedSemaphore, Event, current_thread
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from data_provider.base import DataFetcherManager
from data_provider.fundamental_adapter import AkshareFundamentalAdapter


class FundamentalRecoveryTest(unittest.TestCase):
    def test_cashflow_http_contract_validates_identity_period_and_notice(self):
        adapter = AkshareFundamentalAdapter()
        record = {'SECURITY_CODE': '600519', 'REPORT_DATE': '2026-06-30',
                  'NOTICE_DATE': '2026-08-15', 'NETCASH_OPERATE': 70690750119.06}
        response = SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {'result': {'data': [record]}})
        with patch('requests.get', return_value=response) as get:
            frame = adapter._cashflow_statement_em('600519', '2026-06-30')
            self.assertEqual(frame.iloc[0].NETCASH_OPERATE, 70690750119.06)
            self.assertIn('SECURITY_CODE="600519"', get.call_args.kwargs['params']['filter'])
            self.assertEqual(get.call_args.kwargs['timeout'], 5)
            for key, wrong in [('SECURITY_CODE', '000858'), ('REPORT_DATE', '2026-03-31'),
                               ('NOTICE_DATE', '2099-01-01')]:
                original = record[key]
                record[key] = wrong
                with self.assertRaises(ValueError):
                    adapter._cashflow_statement_em('600519', '2026-06-30')
                record[key] = original

    def test_capital_http_identity_shape_and_windows(self):
        from io import BytesIO
        from urllib.parse import parse_qs, urlparse
        adapter = AkshareFundamentalAdapter()
        payload = {'rc': 0, 'data': {'code': '000858', 'klines': [
            f'2026-09-{day:02d},{day},0,0,0,0,0,0,0,0,0,12,0,0,0' for day in range(1, 11)]}}
        with patch('urllib.request.urlopen', side_effect=lambda *a, **k: BytesIO(json.dumps(payload).encode())) as get:
            frame = adapter._capital_history_em('000858', 'sz')
            params = parse_qs(urlparse(get.call_args.args[0].full_url).query)
            self.assertEqual(params['lmt'], ['10'])
            self.assertEqual(params['secid'], ['0.000858'])
            with patch.object(adapter, '_call_df_candidates', side_effect=[(frame, 'direct', []), (None, None, [])]):
                result = adapter.get_capital_flow('000858')
            self.assertEqual(result['stock_flow']['inflow_5d'], 40)
            self.assertEqual(result['stock_flow']['inflow_10d'], 55)
            payload['data']['code'] = '600519'
            with self.assertRaises(ValueError):
                adapter._capital_history_em('000858', 'sz')

    def manager(self):
        manager = object.__new__(DataFetcherManager)
        manager._fundamental_adapter = AkshareFundamentalAdapter()
        manager._fundamental_timeout_slots = BoundedSemaphore(8)
        return manager

    def test_completed_financials_survive_hanging_later_endpoint(self):
        manager = self.manager()
        release = Event()
        workers = []
        frame = pd.DataFrame({'SECUCODE': ['600519.SH'], 'REPORT_DATE': ['2026-06-30'],
            'TOTALOPERATEREVE': [1000], 'PARENTNETPROFIT': [100],
            'TOTALOPERATEREVETZ': [10], 'PARENTNETPROFITTZ': [8], 'ROEJQ': [12]})

        def call(items, **kwargs):
            name = items[0][0]
            if name == 'stock_financial_analysis_indicator_em':
                return frame, name, []
            if name == 'stock_yjyg_em':
                workers.append(current_thread())
                release.wait()
            return None, None, []

        try:
            with patch.object(manager._fundamental_adapter, '_call_df_candidates', side_effect=call):
                start = time.monotonic()
                payload, error, _ = manager._get_cn_fundamental_bundle('600519', 0.5)
                self.assertIsNone(error)
                self.assertLess(time.monotonic() - start, 1.5)
                self.assertEqual(payload['growth']['revenue_yoy'], 10)
                self.assertEqual(payload['earnings']['financial_report']['revenue'], 1000)
                self.assertEqual(payload['earnings']['financial_report']['report_date'], '2026-06-30')
                self.assertTrue(any('timeout' in e for e in payload['errors']))
                manager._get_cn_fundamental_bundle('600519', 0.5)
                self.assertEqual(len(workers), 1)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)

    def test_financial_fallback_rejects_wrong_stock_and_future_period(self):
        for code, date in [('000858.SZ', '2026-06-30'), ('600519.SH', '2099-12-31')]:
            with self.subTest(code=code, date=date):
                frame = pd.DataFrame({'SECUCODE': [code], 'REPORT_DATE': [date], 'ROEJQ': [123]})
                calls = iter([(frame, 'stock_financial_analysis_indicator_em', [])] + [(None, None, [])] * 6)
                result = AkshareFundamentalAdapter().get_fundamental_bundle(
                    '600519', candidate_runner=lambda *a, **k: next(calls))
                self.assertEqual(result['growth'], {})
                self.assertEqual(result['earnings'], {})

    def test_hung_board_primary_does_not_block_backup_or_spawn_repeats(self):
        manager = self.manager()
        release = Event()
        workers = []

        def slow(n):
            workers.append(current_thread())
            release.wait()

        ranks = ([{'name': 'test', 'change_pct': 1}], [{'name': 'test', 'change_pct': -1}])
        manager._fetchers = [SimpleNamespace(name='recovery_primary', get_sector_rankings=slow),
                            SimpleNamespace(name='recovery_backup', get_sector_rankings=lambda n: ranks)]
        try:
            for _ in range(2):
                top, bottom, chain, _ = manager._get_sector_rankings_with_meta(5, budget_seconds=0.2)
                self.assertEqual((top, bottom), ranks)
                self.assertEqual(chain[0]['result'], 'failed')
                self.assertEqual(chain[-1]['provider'], 'recovery_backup')
            self.assertEqual(len(workers), 1)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)

    def test_capital_endpoint_errors_are_failed_not_unsupported(self):
        adapter = AkshareFundamentalAdapter()
        with patch.object(adapter, '_call_df_candidates', return_value=(None, None, ['connection_failure'])):
            self.assertEqual(adapter.get_capital_flow('000858')['status'], 'failed')

    def test_sina_https_sector_contract_and_invalid_percent_filter(self):
        from data_provider.akshare_fetcher import AkshareFetcher
        fetcher = object.__new__(AkshareFetcher)
        payload = {str(i): ','.join(['label', name, '10', '1', '0', pct] + ['0'] * 7)
                   for i, (name, pct) in enumerate([('行业甲', '2'), ('行业乙', '-1'), ('异常', 'inf')])}
        response = SimpleNamespace(content=json.dumps(payload, ensure_ascii=False).encode('gb18030'),
                                   raise_for_status=lambda: None)
        with patch.dict(sys.modules, {'akshare': SimpleNamespace()}), \
             patch.object(fetcher, '_enforce_rate_limit'), \
             patch('data_provider.akshare_fetcher.requests.get', return_value=response) as request:
            top, bottom = fetcher.get_sector_rankings(1)
        self.assertEqual(top, [{'name': '行业甲', 'change_pct': 2}])
        self.assertEqual(bottom, [{'name': '行业乙', 'change_pct': -1}])
        self.assertEqual(request.call_args.args[0], 'https://money.finance.sina.com.cn/q/view/newFLJK.php')
        self.assertEqual(request.call_args.kwargs, {'params': {'param': 'industry'}, 'timeout': 5.0})

    def test_empty_board_source_continues_to_backup(self):
        manager = self.manager()
        ranks = ([{'name': 'test', 'change_pct': 1}], [])
        manager._fetchers = [SimpleNamespace(name='empty', get_sector_rankings=lambda n: ([], [])),
                            SimpleNamespace(name='backup', get_sector_rankings=lambda n: ranks)]
        top, bottom, chain, _ = manager._get_sector_rankings_with_meta(budget_seconds=0.2)
        self.assertEqual((top, bottom), ranks)
        self.assertEqual(chain[-1]['provider'], 'backup')


if __name__ == '__main__':
    unittest.main()
