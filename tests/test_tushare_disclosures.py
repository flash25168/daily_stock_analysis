from datetime import date
import unittest
from unittest.mock import Mock, MagicMock, patch

import pandas as pd

from data_provider.tushare_disclosures import disclosed, financial_frame, capital_frame


class TushareDisclosuresTest(unittest.TestCase):
    def test_real_adapter_uses_tushare_fields_without_optional_akshare(self):
        from data_provider.fundamental_adapter import AkshareFundamentalAdapter
        identity = dict(ts_code='600000.SH', end_date='20260630', ann_date='20260801')
        data = {'income': pd.DataFrame([{**identity, 'total_revenue': 100, 'n_income_attr_p': 20}]),
                'cashflow': pd.DataFrame([{**identity, 'n_cashflow_act': -5}]),
                'fina_indicator': pd.DataFrame([{**identity, 'roe': 99, 'roe_waa': 10, 'or_yoy': 99, 'tr_yoy': 2,
                    'netprofit_yoy': 3, 'grossprofit_margin': 30}])}
        fetcher = Mock()
        fetcher._convert_stock_code.return_value = '600000.SH'
        fetcher._call_api_with_rate_limit.side_effect = lambda name, **kw: data[name]
        adapter = AkshareFundamentalAdapter()
        with patch.object(adapter, '_has_tushare', return_value=True), \
                patch.object(adapter, '_tushare_fetcher', return_value=fetcher), \
                patch.dict('sys.modules', {'akshare': MagicMock()}):
            result = adapter.get_fundamental_bundle('600000')
        self.assertEqual(result['earnings']['financial_report']['revenue'], 100)
        self.assertEqual(result['earnings']['financial_report']['operating_cash_flow'], -5)
        self.assertEqual(result['growth']['roe'], 10)
        self.assertIn('growth:_financial_tushare', result['source_chain'])

    def test_financial_fields_match_period_without_scaling_yuan(self):
        identity = dict(ts_code='600000.SH', end_date='20260630', ann_date='20260801')
        fetcher = Mock()
        fetcher._convert_stock_code.return_value = '600000.SH'
        data = {'income': pd.DataFrame([{**identity, 'total_revenue': 100, 'n_income_attr_p': 20, 'report_type': '1'}]),
                'cashflow': pd.DataFrame([{**identity, 'n_cashflow_act': -5, 'report_type': '1'}]),
                'fina_indicator': pd.DataFrame([{**identity, 'roe': 99, 'roe_waa': 10, 'or_yoy': 99, 'tr_yoy': 2,
                    'netprofit_yoy': 3, 'grossprofit_margin': 30}])}
        fetcher._call_api_with_rate_limit.side_effect = lambda name, **kw: data[name]
        frame = financial_frame(fetcher, '600000', date(2026, 10, 26))
        self.assertEqual(frame.iloc[0].NETCASH_OPERATE, -5)
        self.assertEqual(frame.iloc[0].TOTALOPERATEREVE, 100)
        self.assertEqual(frame.iloc[0].ROEJQ, 10)
        self.assertEqual(frame.iloc[0].TOTALOPERATEREVETZ, 2)
        data['cashflow'].loc[0, 'end_date'] = '20260331'
        with self.assertRaises(ValueError):
            financial_frame(fetcher, '600000', date(2026, 10, 26))

    def test_wrong_issuer_future_disclosure_parent_statement_excluded(self):
        rows = pd.DataFrame([
            dict(ts_code='600000.SH', end_date='20260630', ann_date='20260801', report_type='1'),
            dict(ts_code='600001.SH', end_date='20260630', ann_date='20260801', report_type='1'),
            dict(ts_code='600000.SH', end_date='20260630', ann_date='20270101', report_type='1'),
            dict(ts_code='600000.SH', end_date='20260630', ann_date='20260801', report_type='6')])
        self.assertEqual(len(disclosed(rows, '600000.SH', date(2026, 10, 26))), 1)

    def test_moneyflow_scales_wan_to_yuan_and_requires_sessions(self):
        sessions = pd.bdate_range('2026-10-13', '2026-10-26')
        frame = pd.DataFrame([dict(ts_code='600000.SH', trade_date=d.strftime('%Y%m%d'),
            buy_lg_amount=10, sell_lg_amount=2, buy_elg_amount=5, sell_elg_amount=1) for d in sessions])
        fetcher = Mock()
        fetcher._convert_stock_code.return_value = '600000.SH'
        fetcher._call_api_with_rate_limit.return_value = frame
        output = capital_frame(fetcher, '600000', date(2026, 10, 26), sessions)
        self.assertEqual(output['主力净流入-净额'].iloc[-1], 120000)
        self.assertEqual(output.attrs['provider'], 'tushare')
        for bad in (frame.iloc[:-1], frame.drop(index=3), pd.concat([frame, frame.iloc[-1:]])):
            fetcher._call_api_with_rate_limit.return_value = bad
            with self.assertRaises(ValueError):
                capital_frame(fetcher, '600000', date(2026, 10, 26), sessions)


if __name__ == '__main__':
    unittest.main()
