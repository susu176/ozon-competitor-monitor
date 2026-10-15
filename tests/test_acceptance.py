import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from subprocess import CalledProcessError

from ozon_monitor.core import FixtureCollector, SnapshotStore, Product, analyze, render
from ozon_monitor.state import git, publish, restore


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.first = FixtureCollector(Path('fixtures/day1.json')).collect()
        self.second = FixtureCollector(Path('fixtures/day2.json')).collect()

    def test_baseline_has_no_discovery_or_category_alert(self):
        delta = analyze(self.first, [])
        self.assertEqual(delta['first_seen'], [])
        self.assertEqual(delta['category_counts'], {})
        self.assertIn('首次发现：0', render(self.first, delta))

    def test_failed_payload_does_not_alert_or_poison_history(self):
        failed = replace(self.second, complete=False, error='timeout')
        delta = analyze(failed, [asdict(self.first)])
        self.assertEqual(delta['first_seen'], [])
        self.assertEqual(delta['price_changes'], [])
        third = replace(self.second, day='2026-10-16')
        delta = analyze(third, [asdict(self.first), asdict(failed)])
        self.assertEqual([p.product_id for p in delta['first_seen']], ['demo-2'])
        self.assertEqual(delta['previous_day'], self.first.day)

    def test_partial_is_lower_bound_not_new_baseline(self):
        partial = replace(self.second, complete=False)
        delta = analyze(partial, [asdict(self.first)])
        self.assertEqual(len(delta['first_seen']), 1)
        self.assertEqual(delta['price_changes'], [])
        self.assertIn('观测下限', render(partial, delta))
        self.assertIn('不建立基准', render(partial, analyze(partial, [])))
        third = replace(self.second, day='2026-10-16')
        delta = analyze(third, [asdict(self.first), asdict(partial)])
        self.assertEqual(delta['first_seen'], [])
        self.assertEqual(delta['previous_day'], self.first.day)

    def test_all_three_category_levels_preserve_ancestors(self):
        products = tuple(Product(str(i), categories=categories) for i, categories in enumerate([
            ('家居', '收纳', '盒'), ('家居', '收纳', '盒'), ('家居', '装饰', None),
            ('服装', '装饰', None), (None, None, None)]))
        delta = analyze(replace(self.second, products=products), [asdict(self.first)])
        self.assertEqual(delta['category_level_counts'][0], {'家居': 3, '服装': 1, '未知': 1})
        self.assertEqual(delta['category_level_counts'][1]['家居 / 收纳'], 2)
        self.assertEqual(delta['category_counts']['家居 / 收纳 / 盒'], 2)
        for counts in delta['category_level_counts']:
            self.assertEqual(sum(counts.values()), 5)

    def test_missing_price_currency_changes_and_decimal_equivalence(self):
        for product in [replace(self.first.products[0], price=None),
                        replace(self.first.products[0], currency='USD'),
                        replace(self.first.products[0], price='100')]:
            delta = analyze(replace(self.second, products=(product,)), [asdict(self.first)])
            self.assertEqual(delta['price_changes'], [])

    def test_fields_round_trip_and_date_evidence(self):
        product = Product('field-test', title='标题', price='1.20', currency='RUB',
                          offer_conditions='公开优惠', image_url='https://example.com/image',
                          product_url='https://example.com/product', categories=('一', '二', '三'),
                          published_at='2026-10-01', published_at_source='fixture explicit evidence')
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / 'db')
            store.save(replace(self.first, products=(product,)))
            raw = store.history(self.first.store_id, self.second.day)[0]['products'][0]
            self.assertEqual(raw['image_url'], product.image_url)
            self.assertEqual(raw['published_at_source'], product.published_at_source)
            self.assertEqual(raw['categories'], ['一', '二', '三'])
            store.close()


class PersistenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.remote.mkdir()
        git(self.remote, 'init', '--bare', '--quiet')
        self.code = self.root / 'code'
        self.code.mkdir()
        git(self.code, 'init', '--quiet')
        git(self.code, 'remote', 'add', 'origin', str(self.remote))
        self.first = FixtureCollector(Path('fixtures/day1.json')).collect()
        self.second = FixtureCollector(Path('fixtures/day2.json')).collect()

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self):
        directory = self.root / 'first-run'
        restore(self.code, directory, bootstrap=True)
        self.collect(directory, self.first)
        publish(directory)
        return directory

    def collect(self, directory, collection):
        store = SnapshotStore(directory / 'snapshots.sqlite3')
        delta = analyze(collection, store.history(collection.store_id, collection.day))
        store.save(collection)
        store.close()
        reports = directory / 'reports'
        reports.mkdir(exist_ok=True)
        (reports / f'{collection.day}.md').write_text(render(collection, delta))
        return delta

    def test_clean_runners_cross_day_restore_and_repeat(self):
        self.seed()
        second = self.root / 'second-run'
        restore(self.code, second)
        delta = self.collect(second, self.second)
        self.assertFalse(delta['baseline'])
        self.assertEqual(delta['previous_day'], '2026-10-14')
        self.assertEqual(len(delta['first_seen']), 1)
        self.assertEqual(len(delta['price_changes']), 1)
        publish(second)
        third = self.root / 'third-run'
        restore(self.code, third)
        head = git(third, 'rev-parse', 'HEAD')
        self.assertEqual(self.collect(third, self.second), delta)
        publish(third)
        self.assertEqual(git(third, 'rev-parse', 'HEAD'), head)
        store = SnapshotStore(third / 'snapshots.sqlite3')
        self.assertEqual(len(store.history('demo-store', '2026-10-16')), 2)
        store.close()

    def test_missing_branch_requires_explicit_initialization(self):
        with self.assertRaises(ValueError):
            restore(self.code, self.root / 'missing')
        self.assertFalse((self.root / 'missing').exists())

    def test_network_error_never_bootstraps(self):
        git(self.code, 'remote', 'set-url', 'origin', str(self.root / 'nonexistent'))
        with self.assertRaises(CalledProcessError):
            restore(self.code, self.root / 'offline', bootstrap=True)
        self.assertFalse((self.root / 'offline').exists())

    def test_corrupt_and_missing_database_are_fatal(self):
        directory = self.seed()
        (directory / 'snapshots.sqlite3').write_bytes(b'not a sqlite database')
        git(directory, 'add', 'snapshots.sqlite3')
        git(directory, 'commit', '-m', 'test corruption')
        git(directory, 'push', 'origin', 'HEAD:monitor-data-mock')
        with self.assertRaises(sqlite3.DatabaseError):
            restore(self.code, self.root / 'corrupt')
        (directory / 'snapshots.sqlite3').unlink()
        git(directory, 'add', '-u')
        git(directory, 'commit', '-m', 'test missing db')
        git(directory, 'push', 'origin', 'HEAD:monitor-data-mock')
        with self.assertRaises(ValueError):
            restore(self.code, self.root / 'lost')

    def test_concurrent_push_cannot_erase_history(self):
        self.seed()
        a, b = self.root / 'a', self.root / 'b'
        restore(self.code, a)
        restore(self.code, b)
        self.collect(a, self.second)
        self.collect(b, replace(self.second, day='2026-10-16'))
        publish(a)
        with self.assertRaises(CalledProcessError):
            publish(b)
        recovered = self.root / 'recovered'
        restore(self.code, recovered)
        store = SnapshotStore(recovered / 'snapshots.sqlite3')
        self.assertEqual([h['day'] for h in store.history('demo-store', '2026-10-17')],
                         ['2026-10-14', '2026-10-15'])
        store.close()
