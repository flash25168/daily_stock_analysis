"""Offline regression tests of the production market-statistics methods.

Load their AST without importing optional news/model SDKs. No paid API calls.
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import pandas as pd


def load_analyzer():
    source = Path(__file__).resolve().parents[1] / 'src/market_analyzer.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MarketAnalyzer')
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    namespace = {'pd': pd, 'logger': logging.getLogger(__name__)}
    exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
    return namespace['MarketAnalyzer'].__new__(namespace['MarketAnalyzer'])


def overview(**kwargs):
    values = dict(up_count=0, down_count=0, flat_count=0, limit_up_count=0,
                  limit_down_count=0, total_amount=0.0, stats_available_fields=None, indices=[],
                  date='2026-10-22', top_sectors=[], bottom_sectors=[], top_concepts=[], bottom_concepts=[])
    values.update(kwargs)
    return SimpleNamespace(**values)


class MissingMarketStatsTests(unittest.TestCase):
    def setUp(self):
        self.a = load_analyzer()
        self.a.profile = SimpleNamespace(has_market_stats=True)
        self.a.region = 'cn'
        self.a._log_context = lambda: ''
        self.a._get_review_language = lambda: 'zh'
        self.a._get_turnover_unit_label = lambda: '亿元'
        self.a._supports_market_light = lambda: False

    def fetch(self, stats):
        self.a.data_manager = SimpleNamespace(get_market_stats=Mock(return_value=stats))
        obj = overview()
        self.a._get_market_statistics(obj)
        return obj

    def test_failed_provider_does_not_render_zero(self):
        obj = self.fetch(None)
        text = self.a._build_stats_block(obj)
        self.assertIn('数据不可用', text)
        self.assertNotIn(': 0', text)
        self.assertTrue(all(v is None for v in self.a._market_stats_values(obj).values()))

    def test_exception_is_missing(self):
        self.a.data_manager = SimpleNamespace(get_market_stats=Mock(side_effect=TimeoutError()))
        obj = overview()
        self.a._get_market_statistics(obj)
        self.assertEqual(obj.stats_available_fields, [])

    def test_all_zero_snapshot_is_missing(self):
        obj = self.fetch(dict(up_count=0, down_count=0, flat_count=0, total_amount=0))
        self.assertTrue(all(v is None for v in self.a._market_stats_values(obj).values()))

    def test_real_zero_limits_preserved_with_valid_breadth(self):
        obj = self.fetch(dict(up_count=100, down_count=200, flat_count=10,
                              limit_up_count=0, limit_down_count=0, total_amount=12345))
        values = self.a._market_stats_values(obj)
        self.assertEqual(values['limit_up_count'], 0)
        self.assertEqual(values['total_amount'], 12345)
        self.assertNotIn('数据不可用', self.a._market_stats_text(obj))

    def test_partial_response_does_not_invent_other_fields(self):
        obj = self.fetch(dict(up_count=100, total_amount=0))
        values = self.a._market_stats_values(obj)
        self.assertEqual(values['up_count'], 100)
        self.assertIsNone(values['down_count'])
        self.assertIsNone(values['total_amount'])

    def test_invalid_fields_rejected(self):
        obj = self.fetch(dict(up_count=100, down_count=float('nan'),
                              flat_count=-1, limit_up_count=True, total_amount=float('inf')))
        values = self.a._market_stats_values(obj)
        self.assertIsNone(values['down_count'])
        self.assertIsNone(values['flat_count'])
        self.assertIsNone(values['limit_up_count'])
        self.assertIsNone(values['total_amount'])

    def test_english_missing_warning(self):
        self.a._get_review_language = lambda: 'en'
        self.assertIn('Data unavailable', self.a._market_stats_text(self.fetch(None)))

    def test_full_prompt_contains_missing_not_zero(self):
        self.a.profile.has_sector_rankings = False
        self.a._get_output_language = lambda: 'zh'
        self.a._get_market_scope_name = lambda language: 'A股'
        self.a._get_index_hint = lambda: ''
        self.a._get_strategy_prompt_block = lambda: ''
        prompt = self.a._build_review_prompt(self.fetch(None), [])
        self.assertIn('上涨家数: 数据不可用', prompt)
        self.assertNotIn('两市成交额: 0', prompt)
        self.assertIn('缺失值不代表零', prompt)

    def test_partial_signal_explicit_and_mobile_indices(self):
        obj = overview(indices=[SimpleNamespace(name='测试指数', current=3000, change_pct=-1,
                        open=3010, high=3020, low=2990, amplitude=1, amount=1e10)])
        self.a._supports_market_light = lambda: True
        self.a.build_market_light_snapshot = lambda overview: dict(score=40, temperature_label='震荡', label='需观察',
                    reasons=['指数'], guidance='观察', data_quality='partial')
        self.assertIn('信号不完整', self.a._build_stats_block(obj))
        text = self.a._build_indices_block(obj)
        self.assertIn('3000.00', text)
        self.assertNotIn('|', text)


if __name__ == '__main__':
    unittest.main()
