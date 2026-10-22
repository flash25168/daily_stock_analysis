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


if __name__ == '__main__': unittest.main()
