import unittest
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pandas as pd
from data_provider.market_snapshot_stats import aggregate_snapshot


def snapshot():
    df = pd.DataFrame({'SECURITY_CODE': [str(600000 + i) for i in range(3000)],
                       'CHANGE_RATE': ([1, -1, 0] * 1000), 'DEAL_AMOUNT': [1e8] * 3000,
                       'MAX_TRADE_DATE': ['2026-10-22'] * 3000})
    df.attrs['reported_count'] = 3000
    return df


class SnapshotStatsTests(unittest.TestCase):
    def test_units_counts_and_no_invented_limits(self):
        result = aggregate_snapshot(snapshot(), '2026-10-22')
        self.assertEqual(result['up_count'], 1000)
        self.assertEqual(result['down_count'], 1000)
        self.assertEqual(result['flat_count'], 1000)
        self.assertEqual(result['total_amount'], 3000)
        self.assertNotIn('limit_up_count', result)

    def test_reject_incomplete_duplicates_stale_invalid(self):
        for kind in ('count', 'duplicate', 'stale', 'amount', 'change', 'date'):
            with self.subTest(kind=kind):
                df = snapshot()
                if kind == 'count': df.attrs['reported_count'] = 3001
                if kind == 'duplicate': df.loc[1, 'SECURITY_CODE'] = df.loc[0, 'SECURITY_CODE']
                if kind == 'stale': df.loc[1, 'MAX_TRADE_DATE'] = '2026-10-21'
                if kind == 'amount': df.loc[1, 'DEAL_AMOUNT'] = float('nan')
                if kind == 'change': df.loc[1, 'CHANGE_RATE'] = float('nan')
                if kind == 'date': df.loc[1, 'MAX_TRADE_DATE'] = None
                with self.assertRaises(ValueError): aggregate_snapshot(df, '2026-10-22')

    def test_suspended_not_counted(self):
        df = snapshot()
        df.loc[0, 'DEAL_AMOUNT'] = 0
        df.loc[0, 'MAX_TRADE_DATE'] = '2026-10-01'
        result = aggregate_snapshot(df, '2026-10-22')
        self.assertEqual(result['up_count'], 999)
        self.assertEqual(result['total_amount'], 2999)

    def test_manager_uses_last_resort_only_after_existing_sources(self):
        from data_provider.base import DataFetcherManager
        manager = DataFetcherManager.__new__(DataFetcherManager)
        manager._get_tickflow_fetcher = lambda: None
        first = Mock(name='first')
        first.name = 'first'
        first.get_market_stats.return_value = None
        manager._fetchers = [first]
        result = aggregate_snapshot(snapshot(), '2026-10-22')
        with patch('data_provider.market_snapshot_stats.fetch_snapshot_stats', return_value=result) as fallback:
            self.assertEqual(manager.get_market_stats(purpose='market_review:cn'), result)
            fallback.assert_called_once()
            first.get_market_stats.return_value = {'up_count': 10}
            self.assertEqual(manager.get_market_stats(purpose='market_review:cn'), {'up_count': 10})
            self.assertEqual(fallback.call_count, 1)

    def test_raw_pagination_count_validation(self):
        path = Path(__file__).resolve().parents[1] / 'src/services/screening/snapshot.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'fetch_em_datacenter_raw')
        request = Mock(return_value=SimpleNamespace(json=lambda: {'success': True, 'result': {'count': 2, 'data': [{'SECURITY_CODE': '600000'}]}}))
        namespace = {'pd': pd, '_eastmoney_get': request}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), 'exec'), namespace)
        with self.assertRaises(RuntimeError): namespace['fetch_em_datacenter_raw']()


if __name__ == '__main__': unittest.main()
