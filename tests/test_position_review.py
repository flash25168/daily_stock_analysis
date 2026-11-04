import unittest
from datetime import date
from unittest.mock import Mock, patch
import tempfile
from pathlib import Path
import json
from types import SimpleNamespace

import pandas as pd

from scripts.track_positions import positions_from_json, review_position, prepare, send
from src.short_term_rules import short_pullback
from src.services.screening.models import HardFilterConfig
from src.services.screening.filter import apply_hard_filters, without_daily_filters, requires_daily_features


class PositionReviewTest(unittest.TestCase):
    def test_short_rule_and_filter_are_real(self):
        history = pd.DataFrame({'close': list(range(90, 109))+[107.5], 'volume': [100]*19+[60]})
        self.assertTrue(short_pullback(history))
        for last in (70, 100, float('nan'), -1):
            history.loc[19, 'volume'] = last
            self.assertFalse(short_pullback(history))
        config = HardFilterConfig(exclude_st=False, require_short_pullback=True)
        frame = pd.DataFrame({'short_pullback': [True, False]})
        self.assertEqual(len(apply_hard_filters(frame, config)), 1)
        self.assertTrue(requires_daily_features(config))
        self.assertFalse(without_daily_filters(config).require_short_pullback)

    def test_downtrend_or_insufficient_history_is_not_pullback(self):
        self.assertFalse(short_pullback(pd.DataFrame({'close': range(110, 90, -1), 'volume': [1]*20})))
        self.assertFalse(short_pullback(pd.DataFrame({'close': [1], 'volume': [1]})))

    def test_invalid_historical_values_cannot_create_a_signal(self):
        for column, value in [('close', -1), ('close', float('inf')),
                              ('volume', -1), ('volume', float('nan'))]:
            history = pd.DataFrame({'close': list(range(90, 109))+[107.5],
                                    'volume': [100.]*19+[60.]})
            history.loc[17, column] = value
            self.assertFalse(short_pullback(history))

    def test_daily_feature_reaches_filter_and_condition_diagnostics(self):
        from src.services.screening.daily import compute_daily_features
        from src.services.screening.filter import check_hard_filters
        history = pd.DataFrame({'date': pd.bdate_range('2026-09-01', periods=20),
                                'close': list(range(90, 109))+[107.5],
                                'volume': [100]*19+[60]})
        config = HardFilterConfig(exclude_st=False, require_short_pullback=True)
        for volume, expected in [(60, True), (70, False)]:
            history.loc[19, 'volume'] = volume
            features = compute_daily_features(history)
            self.assertEqual(not apply_hard_filters(pd.DataFrame([features]), config).empty, expected)
            self.assertEqual(check_hard_filters(features, config)[0]['passed'], expected)
        broken = pd.concat([history.iloc[:1], history], ignore_index=True)
        broken.loc[len(broken)-1, 'close'] = float('nan')
        self.assertFalse(compute_daily_features(broken)['short_pullback'])

    def test_lots_keep_separate_dates_and_execution_prices(self):
        sessions = pd.bdate_range('2026-09-01', '2026-10-26')
        clock = SimpleNamespace(phase=SimpleNamespace(value='postmarket'),
            warnings=[], effective_daily_bar_date=date(2026, 10, 26))
        # The old batch is expired; the new batch alone is on day one.
        position = dict(code='000001', entry_date='2026-10-01', entry_price=99, shares=200,
            lots=[dict(entry_date='2026-10-01', execution_price=11, shares=100),
                  dict(entry_date='2026-10-23', execution_price=10, shares=100)])
        fetch = Mock(return_value=pd.DataFrame({'date': sessions, 'close': [10.]*len(sessions)}))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.assertTrue(prepare(folder, json.dumps([position]), clock=clock,
                                   calendar=Mock(return_value=sessions), fetch=fetch)['send'])
            body = (folder/'position_review.md').read_text(encoding='utf8')
            self.assertIn('2026-10-23，100股', body)
            self.assertIn('本批成交价变动+0.00%', body)
            self.assertNotIn('2026-10-01', body)
            self.assertNotIn('99', body)
        fetch.assert_called_once()
        position['lots'][0]['entry_date'] = '2026-10-22'
        fetch.reset_mock()
        with tempfile.TemporaryDirectory() as temp:
            prepare(Path(temp), json.dumps([position]), clock=clock,
                    calendar=Mock(return_value=sessions), fetch=fetch)
            body = (Path(temp)/'position_review.md').read_text(encoding='utf8')
            self.assertIn('第2个交易日', body)
            self.assertIn('第1个交易日', body)
        fetch.assert_called_once()

    def test_invalid_batches_do_not_fall_back_to_aggregate_cost(self):
        for lots in ([], [dict(entry_date='2026-10-23', execution_price=10, shares=100)],
                     [dict(entry_date='2026-10-23', execution_price=-1, shares=200)]):
            position = dict(code='000001', entry_date='2026-10-23', entry_price=10,
                            shares=200, lots=lots)
            with self.assertRaises(ValueError):
                positions_from_json(json.dumps([position]))

    def test_manual_positions_are_not_candidate_codes(self):
        self.assertEqual(positions_from_json(''), [])
        for raw in ('["000001"]', '[{"code":"300001"}]', '{}'):
            with self.assertRaises((ValueError, KeyError)):
                positions_from_json(raw)

    def test_count_sessions_not_weekend_days_and_stop(self):
        sessions = pd.bdate_range('2026-09-25', '2026-10-26')
        history = pd.DataFrame({'date': sessions, 'close': [10.]*(len(sessions)-1)+[9.]})
        position = {'code': '000001', 'entry_date': '2026-10-23', 'entry_price': 10, 'shares': 100}
        result = review_position(position, date(2026, 10, 26), sessions, history)
        self.assertIn('第1个交易日', result)
        self.assertIn('跌破MA20', result)
        self.assertIn('-10.00%', result)
        position['entry_date'] = '2026-10-21'
        self.assertIn('第3交易日', review_position(position, date(2026, 10, 26), sessions, history))
        # This test passes explicit sessions, not a fabricated production calendar.
        for bad in (history.iloc[:-1], history.iloc[-19:]):
            with self.assertRaises(ValueError):
                review_position(position, date(2026, 10, 26), sessions, bad)

    def test_no_positions_does_not_fetch(self):
        fetch = Mock()
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(prepare(Path(temp), '', fetch=fetch)['send'])
        fetch.assert_not_called()

    def test_holidays_and_positions_outside_three_sessions_do_not_send(self):
        raw = '[{"code":"000001","entry_date":"2026-10-20","entry_price":10,"shares":100}]'
        sessions = pd.bdate_range('2026-09-01', '2026-10-26')
        fetch = Mock()
        for phase in ('postmarket', 'non_trading'):
            clock = SimpleNamespace(phase=SimpleNamespace(value=phase),
                warnings=[], effective_daily_bar_date=date(2026, 10, 26))
            with tempfile.TemporaryDirectory() as temp:
                self.assertFalse(prepare(Path(temp), raw, clock=clock,
                    calendar=Mock(return_value=sessions), fetch=fetch)['send'])
        fetch.assert_not_called()

    def test_duplicate_reservation_blocks_sender(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder/'review_plan.json').write_text('{"send":true,"name":"day","date":"2026-10-26","run_id":"1"}')
            api, sender = Mock(return_value={'total_count': 2, 'artifacts': []}), Mock()
            with self.assertRaises(ValueError):
                send(folder, '1', api=api, sender=sender)
            sender.send_to_pushplus.assert_not_called()

    def test_send_is_verified_and_same_result_cannot_be_resent(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder/'review_plan.json').write_text(json.dumps(
                dict(send=True, name='day', date='2026-10-26', run_id='2')))
            (folder/'position_review.md').write_text('offline fake reminder')
            api = Mock(return_value={'total_count': 1, 'artifacts': [
                {'id': 1, 'expired': False, 'workflow_run': {'id': 2}}]})
            sender = Mock()
            sender.send_to_pushplus.return_value = True
            with patch.dict('os.environ', {'PUSHPLUS_TOKEN': 'fake'}), \
                    patch('scripts.screen_candidates.market_clock', return_value={
                        'phase': 'postmarket', 'expected_daily_date': '2026-10-26'}):
                send(folder, '1', api=api, sender=sender)
                with self.assertRaises(ValueError):
                    send(folder, '1', api=api, sender=sender)
            sender.send_to_pushplus.assert_called_once()

    def test_registered_position_generates_review_without_any_model(self):
        sessions = pd.bdate_range('2026-09-01', '2026-10-26')
        history = pd.DataFrame({'date': sessions, 'close': [10.]*len(sessions)})
        clock = SimpleNamespace(phase=SimpleNamespace(value='postmarket'),
            warnings=[], effective_daily_bar_date=date(2026, 10, 26))
        raw = '[{"code":"000001","entry_date":"2026-10-23","entry_price":10,"shares":100}]'
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            plan = prepare(folder, raw, clock=clock, calendar=Mock(return_value=sessions),
                           fetch=Mock(return_value=history))
            self.assertTrue(plan['send'])
            self.assertIn('第1个交易日', (folder/'position_review.md').read_text(encoding='utf8'))


if __name__ == '__main__':
    unittest.main()
