import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('diagnose_report_data', Path(__file__).resolve().parents[1] / 'scripts/diagnose_report_data.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiagnosticTests(unittest.TestCase):
    def test_only_known_failures_printable(self):
        self.assertEqual(module.safe_failure(ValueError('invalid turnover')), 'invalid turnover')
        self.assertEqual(module.safe_failure(ValueError('token=private')), 'ValueError')
        self.assertEqual(module.safe_failure(RuntimeError('private-url')), 'RuntimeError')

    def test_fields_without_values(self):
        result = module.summarize_block({'status': 'ok', 'data': {'revenue': 100, 'report_date': '2026-06-30', 'absent': None}})
        self.assertEqual(result['populated_fields'], ['report_date', 'revenue'])
        self.assertNotIn('100', str(result))

    def test_missing_shell(self):
        self.assertEqual(module.summarize_block(None)['status'], 'missing')

    def test_nested_financial_fields_visible_without_values(self):
        result = module.summarize_block({'status': 'ok', 'data': {
            'financial_report': {'revenue': 12345, 'report_date': '2026-06-30', 'missing': None}}})
        self.assertEqual(result['populated_fields'], ['financial_report.report_date', 'financial_report.revenue'])
        self.assertNotIn('12345', str(result))

    def test_error_categories_hide_original_strings(self):
        self.assertEqual(module.error_category('fundamental stage timeout'), 'stage_budget_exhausted')
        result = module.summarize_block({'errors': ['secret=xxx connection aborted'],
                   'source_chain': [{'provider': 'AkshareFetcher', 'error': 'private-url timeout'}]})
        self.assertNotIn('secret', str(result))
        self.assertNotIn('private-url', str(result))
        self.assertIn('connection_failure', result['error_categories'])

    def test_missing_amount_is_not_assumed_suspended(self):
        import pandas as pd
        frame = pd.DataFrame({'SECURITY_CODE': ['600001', '600002'], 'DEAL_AMOUNT': [None, 100],
                              'NEW_PRICE': [12, 13], 'unrelated_secret': ['hidden', 'hidden']})
        result = module.missing_amount_details(frame)
        self.assertEqual(result['count'], 1)
        self.assertFalse(result['rows'][0]['suspension_verified'])
        self.assertNotIn('hidden', str(result))


if __name__ == '__main__': unittest.main()
