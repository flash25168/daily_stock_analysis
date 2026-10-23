import unittest
import ast
import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace, MethodType
from unittest.mock import Mock, patch
import pandas as pd
MODULE_PATH = Path(__file__).resolve().parents[1] / 'data_provider/market_snapshot_stats.py'
spec = importlib.util.spec_from_file_location('snapshot_stats_under_test', MODULE_PATH)
stats_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats_module)
aggregate_snapshot = stats_module.aggregate_snapshot


def snapshot():
    df = pd.DataFrame({'SECURITY_CODE': [str(600000 + i) for i in range(3003)],
                       'CHANGE_RATE': ([1, -1, 0] * 1001), 'DEAL_AMOUNT': [1e8] * 3003,
                       'MAX_TRADE_DATE': ['2026-10-22'] * 3003})
    df.attrs['reported_count'] = 3003
    return df


class SnapshotStatsTests(unittest.TestCase):
    def test_units_counts_and_no_invented_limits(self):
        result = aggregate_snapshot(snapshot(), '2026-10-22')
        self.assertEqual(result['sample_stats']['up_count'], 1001)
        self.assertEqual(result['sample_stats']['down_count'], 1001)
        self.assertEqual(result['sample_stats']['flat_count'], 1001)
        self.assertEqual(result['sample_stats']['amount_yi'], 3003)
        self.assertNotIn('up_count', result)
        self.assertNotIn('total_amount', result)
        self.assertNotIn('limit_up_count', result)

    def test_reject_incomplete_duplicates_stale_invalid(self):
        for kind in ('count', 'duplicate', 'stale', 'amount', 'change', 'date'):
            with self.subTest(kind=kind):
                df = snapshot()
                if kind == 'count': df.attrs['reported_count'] = 3004
                if kind == 'duplicate': df.loc[1, 'SECURITY_CODE'] = df.loc[0, 'SECURITY_CODE']
                if kind == 'stale': df.loc[1, 'MAX_TRADE_DATE'] = '2026-10-21'
                if kind == 'amount': df.loc[1, 'DEAL_AMOUNT'] = -1
                if kind == 'change': df.loc[1, 'CHANGE_RATE'] = float('nan')
                if kind == 'date': df.loc[1, 'MAX_TRADE_DATE'] = None
                with self.assertRaises(ValueError): aggregate_snapshot(df, '2026-10-22')

    def test_suspended_not_counted(self):
        df = snapshot()
        df.loc[0, 'DEAL_AMOUNT'] = 0
        df.loc[0, 'MAX_TRADE_DATE'] = '2026-10-01'
        result = aggregate_snapshot(df, '2026-10-22')
        self.assertEqual(result['sample_stats']['up_count'], 1000)
        self.assertEqual(result['sample_stats']['amount_yi'], 3002)

    def test_missing_turnover_excluded_without_assuming_suspension(self):
        df = snapshot()
        df.loc[0, 'DEAL_AMOUNT'] = float('nan')
        sample = aggregate_snapshot(df, '2026-10-22')['sample_stats']
        self.assertEqual(sample['unknown_amount_rows'], 1)
        self.assertEqual(sample['zero_amount_rows'], 0)
        self.assertEqual(sample['covered_rows'], 3002)
        self.assertEqual(sample['amount_yi'], 3002)

    def test_manager_uses_last_resort_only_after_existing_sources(self):
        # Execute the actual manager method without importing optional provider SDKs.
        path = Path(__file__).resolve().parents[1] / 'data_provider/base.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DataFetcherManager')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'get_market_stats')
        module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
        namespace = {'logger': logging.getLogger(__name__), 'time': time}
        exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
        manager = SimpleNamespace()
        manager.get_market_stats = MethodType(namespace['get_market_stats'], manager)
        manager._get_tickflow_fetcher = lambda: None
        first = Mock(name='first')
        first.name = 'first'
        first.get_market_stats.return_value = None
        manager._fetchers = [first]
        result = aggregate_snapshot(snapshot(), '2026-10-22')
        fallback = Mock(return_value=result)
        with patch.dict(sys.modules, {'data_provider.market_snapshot_stats': SimpleNamespace(fetch_snapshot_stats=fallback)}):
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
