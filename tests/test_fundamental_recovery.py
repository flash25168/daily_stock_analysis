"""Offline regressions for partial disclosures and independently bounded fallbacks."""
import time
import unittest
from threading import BoundedSemaphore, Event, current_thread
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from data_provider.base import DataFetcherManager
from data_provider.fundamental_adapter import AkshareFundamentalAdapter


class FundamentalRecoveryTest(unittest.TestCase):
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
                calls = iter([(frame, 'stock_financial_analysis_indicator_em', [])] + [(None, None, [])] * 5)
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


if __name__ == '__main__':
    unittest.main()
