import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from ozon_monitor.core import Collection, FixtureCollector, Product, SnapshotStore, analyze, render


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.first = FixtureCollector(Path('fixtures/day1.json')).collect()
        self.second = FixtureCollector(Path('fixtures/day2.json')).collect()

    def test_first_discovery_and_price_change(self):
        delta = analyze(self.second, [asdict(self.first)])
        self.assertEqual([p.product_id for p in delta['first_seen']], ['demo-2'])
        self.assertEqual(delta['price_changes'], [dict(product_id='demo-1', before='100.00', after='90.00', currency='RUB')])
        self.assertIsNone(delta['first_seen'][0].published_at)
        self.assertIn('未知 / 未知 / 未知', delta['category_counts'])

    def test_baseline_is_not_launch_date(self):
        delta = analyze(self.first, [])
        self.assertTrue(delta['baseline'])
        self.assertIn('不计作当天上新', render(self.first, delta))

    def test_incomplete_and_scope_changes_do_not_compare_prices(self):
        history = [asdict(self.first)]
        self.assertEqual(analyze(replace(self.second, complete=False), history)['price_changes'], [])
        self.assertTrue(analyze(replace(self.second, scope='other'), history)['baseline'])
        changed = replace(self.second.products[0], offer_conditions='card only')
        self.assertEqual(analyze(replace(self.second, products=(changed,)), history)['price_changes'], [])

    def test_missing_then_returning_is_not_first_discovery(self):
        empty = replace(self.first, day='2026-10-15', products=())
        returned = replace(self.first, day='2026-10-16')
        delta = analyze(returned, [asdict(self.first), asdict(empty)])
        self.assertEqual(delta['first_seen'], [])

    def test_persistence_idempotency_conflict_and_store_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite3'
            store = SnapshotStore(path)
            store.save(self.first)
            store.save(self.first)
            with self.assertRaises(ValueError):
                store.save(replace(self.first, products=()))
            store.close()
            store = SnapshotStore(path)
            self.assertEqual(len(store.history('demo-store', self.second.day)), 1)
            self.assertEqual(store.history('other', self.second.day), [])
            self.assertEqual(store.history('demo-store', self.first.day), [])
            store.close()

    def test_validation(self):
        for price in ['NaN', '-1', 'Infinity']:
            with self.assertRaises(ValueError):
                Product('id', price=price, currency='RUB')
        with self.assertRaises(ValueError):
            Product('id', published_at='2026-01-01')
        with self.assertRaises(ValueError):
            replace(self.first, products=self.first.products * 2)
        with self.assertRaises(ValueError):
            replace(self.first, error='blocked')

    def test_cli_mock_payload(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for fixture in ['day1', 'day2', 'day2']:
                subprocess.run([sys.executable, '-m', 'ozon_monitor', '--fixture',
                                f'fixtures/{fixture}.json', '--db', str(root / 'db'),
                                '--output', str(root / 'reports')], check=True, capture_output=True)
            payloads = list((root / 'reports').glob('*.json'))
            self.assertEqual(len(payloads), 2)
            payload = json.loads(payloads[-1].read_text())
            self.assertEqual(payload['msg_type'], 'text')
            self.assertIn('【模拟数据】', payload['content']['text'])


if __name__ == '__main__':
    unittest.main()
