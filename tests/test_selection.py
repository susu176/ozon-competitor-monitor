import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ozon_monitor.selection import (
    RUN_HOURS, due_at, load_candidates, load_rules, main, new_passes, review,
)


class SelectionTests(unittest.TestCase):
    def test_sample_file_passes_only_complete_rows(self):
        result = review(load_candidates(Path('fixtures/selection-sample.json')),
                        load_rules(Path('config/selection-rules.json')))
        self.assertEqual([item.product_id for item in result['passed']], ['sample-pass'])
        reasons = {row['product'].product_id: row['reasons'] for row in result['rejected']}
        self.assertIn('不在', reasons['sample-expensive'][0])
        self.assertIn('月销量未知', ''.join(reasons['sample-unknown-sales']))

    def test_second_table_reports_only_new_passes(self):
        rules = load_rules(Path('config/selection-rules.json'))
        first = review(load_candidates(Path('fixtures/selection-sample.json')), rules)
        again = new_passes(first['passed'], {'sample-pass'})
        self.assertEqual(again, [])

    def test_schedule_is_nine_to_nine_every_two_hours(self):
        self.assertEqual(RUN_HOURS, (9, 11, 13, 15, 17, 19, 21))
        self.assertTrue(due_at(datetime(2026, 11, 8, 1, 0, tzinfo=timezone.utc)))
        self.assertFalse(due_at(datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)))
        self.assertTrue(due_at(datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)))
        self.assertFalse(due_at(datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)))

    def test_cli_writes_report_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = root / 'seen.json'
            output = root / 'out.md'
            code = main(['--input', 'fixtures/selection-sample.json', '--rules', 'config/selection-rules.json',
                         '--seen', str(seen), '--output', str(output)])
            self.assertEqual(code, 0)
            text = output.read_text(encoding='utf-8')
            self.assertIn('这次新过审：1', text)
            self.assertIn('不是程序打开 Ozon', text)
            self.assertEqual(json.loads(seen.read_text(encoding='utf-8')), ['sample-pass'])
            code = main(['--input', 'fixtures/selection-sample.json', '--rules', 'config/selection-rules.json',
                         '--seen', str(seen), '--output', str(output)])
            self.assertEqual(code, 0)
            self.assertIn('这次新过审：0', output.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
