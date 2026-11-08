import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ozon_monitor.access import (
    USER_AGENT, access_permitted, classify_response, fetch_once, recent_block, robots_decision,
)
from ozon_monitor.config import load_config
from ozon_monitor.core import Product, SnapshotStore, analyze, render
from ozon_monitor.daily import main as daily_main
from ozon_monitor.prep import (
    SUPPORTED_PARSERS, PageCoverage, PublicShopCollector, assemble_collection, assess_readiness,
    load_evidence_records, report_day,
)
from ozon_monitor.probe import main as probe_main, run_page, run_robots, run_terms


EVIDENCE = Path('docs/evidence/2026-11-08-public-access.json')
SHOP = 'https://www.ozon.ru/seller/__unit_test_not_requested__'


def _product(product_id='sku-1', price='10.00', currency='RUB', title='标题'):
    return Product(product_id, title=title, price=price, currency=currency,
                   product_url='https://www.ozon.ru/product/__unit_test_not_requested__')


def _coverage(**changes):
    values = dict(sort='newest', pages_fetched=1, reached_end=True, truncated_by_limit=False, expected_pages=1)
    values.update(changes)
    return PageCoverage(**values)


def _at(hour, minute=0):
    return datetime(2026, 11, 8, hour, minute, tzinfo=timezone.utc)


class EvidenceTests(unittest.TestCase):
    def test_recorded_requests_do_not_grant_shop_access(self):
        records = load_evidence_records(EVIDENCE)
        self.assertEqual(len(records), 4)
        robots, terms, shop, retry = records
        self.assertEqual(robots['requested_url'], 'https://www.ozon.ru/robots.txt')
        self.assertEqual(robots['status'], 307)
        self.assertIn('__rr=1', robots['headers']['location'])
        self.assertEqual(robots['outcome'], 'redirect_challenge')
        self.assertTrue(robots['client_followed_redirects'])
        body = robots['body_excerpt'].encode()
        self.assertEqual(hashlib.sha256(body).hexdigest(), robots['sha256'])
        self.assertEqual(classify_response(
            robots['status'], robots['headers']['content-type'], robots['body_excerpt'],
            robots['headers']['location']), 'redirect_challenge')
        self.assertNotIn('User-agent:', robots['body_excerpt'])
        decision = access_permitted(robots['outcome'], robots['body_excerpt'], SHOP, USER_AGENT)
        self.assertFalse(decision.permitted)
        self.assertEqual(terms['outcome'], 'timeout')
        self.assertIn('timed out', terms['detail'])
        self.assertNotIn('body_excerpt', terms)
        observed = datetime.fromisoformat(robots['started_at'])
        self.assertIsNotNone(recent_block(records, 'www.ozon.ru', observed + timedelta(hours=1)))
        self.assertIsNone(recent_block(records, 'www.ozon.ru', observed + timedelta(hours=25)))
        self.assertEqual(shop['requested_url'], 'https://ozon.ru/s/mir-poleznyh-veshchey-5652967')
        self.assertEqual(shop['status'], 307)
        self.assertEqual(shop['headers']['location'], 'https://ozon.ru/s/mir-poleznyh-veshchey-5652967?__rr=1')
        self.assertEqual(shop['outcome'], 'redirect_challenge')
        self.assertFalse(shop['redirects_followed'])
        self.assertTrue(shop['set_cookie_present'])
        self.assertIsNone(shop['parsed_products'])
        self.assertEqual(hashlib.sha256(shop['body_excerpt'].encode()).hexdigest(), shop['sha256'])
        self.assertNotIn('product_id', shop['body_excerpt'])
        shop_time = datetime.fromisoformat(shop['started_at'])
        self.assertIsNotNone(recent_block(records, 'ozon.ru', shop_time + timedelta(minutes=5)))
        self.assertEqual(retry['started_at'], '2026-11-08T12:18:45.436692+00:00')
        self.assertEqual(retry['outcome'], 'redirect_challenge')
        self.assertFalse(retry['redirects_followed'])
        self.assertTrue(retry['set_cookie_present'])
        self.assertIsNone(retry['parsed_products'])
        self.assertEqual(retry['sha256'], shop['sha256'])
        self.assertNotIn('cookie=', json.dumps(retry).lower())


class SyntheticRobotsTests(unittest.TestCase):
    def test_rules_are_explicitly_synthetic(self):
        # This text is a unit-test fixture, not a copy of Ozon's robots.txt.
        text = 'User-agent: *\nDisallow: /search\nAllow: /seller/\n'
        self.assertTrue(robots_decision(text, SHOP, USER_AGENT).permitted)
        self.assertFalse(robots_decision(text, 'https://www.ozon.ru/search/?text=box', USER_AGENT).permitted)
        delayed = text + 'Crawl-delay: 20\n'
        self.assertFalse(robots_decision(delayed, SHOP, USER_AGENT).permitted)
        conflict = 'User-agent: *\nAllow: /seller\nDisallow: /seller\n'
        self.assertFalse(robots_decision(conflict, SHOP, USER_AGENT).permitted)
        self.assertEqual(classify_response(403, 'text/html', 'denied', None), 'blocked')
        self.assertEqual(classify_response(200, 'text/html', 'please solve the captcha', None), 'captcha')


class ProbeBehaviorTests(unittest.TestCase):
    def test_local_redirect_is_not_followed_and_cookie_is_not_stored(self):
        hits = {'count': 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits['count'] += 1
                body = b'<html><title>307 Temporary Redirect</title></html>'
                self.send_response(307)
                self.send_header('Location', '/robots.txt?__rr=1')
                self.send_header('Set-Cookie', 'secret=do-not-store')
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            observation = fetch_once(f'http://127.0.0.1:{server.server_address[1]}/robots.txt', USER_AGENT, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(hits['count'], 1)
        self.assertEqual(observation['outcome'], 'redirect_challenge')
        self.assertTrue(observation['set_cookie_present'])
        self.assertNotIn('secret', json.dumps(observation))
        self.assertIsNone(observation['parsed_products'])

    def test_rate_limit_and_unparsed_success_stop_without_products(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/limited':
                    body = b'busy'
                    self.send_response(429)
                    self.send_header('Retry-After', '60')
                else:
                    body = '<html><div data-product-id="999">样例</div></html>'.encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            limited = fetch_once(f'http://127.0.0.1:{port}/limited', USER_AGENT, timeout=5)
            page = fetch_once(f'http://127.0.0.1:{port}/page', USER_AGENT, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(limited['outcome'], 'rate_limited')
        self.assertEqual(page['outcome'], 'http_ok_unparsed')
        self.assertIsNone(page['parsed_products'])
        self.assertNotIn('product_id', page)

    def test_commands_do_not_fetch_when_blocked_or_fetch_flag_missing(self):
        records = load_evidence_records(EVIDENCE)
        now = datetime.fromisoformat(records[0]['started_at']) + timedelta(minutes=5)

        def boom(*args, **kwargs):
            raise AssertionError('should not fetch')

        self.assertFalse(run_robots(records, boom, now)['fetched'])
        self.assertFalse(run_terms(records, boom, now)['fetched'])
        self.assertFalse(run_page(SHOP, records, 'ok', 'User-agent: *\nAllow: /\n', boom, now)['fetched'])
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / 'none.json'
            evidence.write_text('{"requests": []}', encoding='utf-8')
            with patch('ozon_monitor.probe.fetch_once', side_effect=AssertionError('network')):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = probe_main(['--evidence', str(evidence), 'robots'])
        self.assertEqual(code, 2)


class ConfigAndReadinessTests(unittest.TestCase):
    def test_configured_shop_is_recorded_but_collector_cannot_parse(self):
        config = load_config(Path('config/shops.json'))
        self.assertEqual(len(config.shops), 1)
        self.assertEqual(config.shops[0].store_id, '5652967')
        self.assertEqual(config.shops[0].url, 'https://ozon.ru/s/mir-poleznyh-veshchey-5652967')
        self.assertEqual(config.shops[0].sort, 'newest')
        self.assertEqual(config.timezone, 'Asia/Shanghai')
        self.assertEqual(SUPPORTED_PARSERS, frozenset())
        readiness = assess_readiness(config, load_evidence_records(EVIDENCE))
        self.assertFalse(readiness.ready)
        self.assertTrue(any('5652967' in reason for reason in readiness.reasons))
        self.assertTrue(any('解析器' in reason for reason in readiness.reasons))
        self.assertFalse(any('未提供店铺 URL' in reason for reason in readiness.reasons))
        with self.assertRaises(RuntimeError):
            PublicShopCollector(readiness).collect()

    def test_urls_and_browser_impersonation_are_rejected(self):
        for url in ['http://www.ozon.ru/seller/a', 'https://example.com/seller/a',
                    'https://user:pass@www.ozon.ru/seller/a', 'https://www.ozon.ru/',
                    'https://www.ozon.ru.evil.com/seller/a', 'https://www.ozon.ru/seller/a?token=abc']:
            with self.assertRaises(ValueError):
                self._config([{'store_id': 'shop', 'url': url}])
        with self.assertRaises(ValueError):
            self._config([], user_agent='Mozilla/5.0 Chrome/120')

    def test_confirmed_measurement_still_cannot_enable_parser(self):
        config = self._config([{'store_id': 'shop', 'url': SHOP}])
        robots = {'requested_url': 'https://www.ozon.ru/robots.txt', 'outcome': 'ok',
                  'body': 'User-agent: *\nAllow: /\n'}
        measurement = {'store_id': 'shop', 'url': SHOP, 'confirmed': True, 'parser': 'guessed-v1'}
        readiness = assess_readiness(config, [robots], [measurement])
        self.assertFalse(readiness.ready)
        self.assertTrue(any('解析器' in reason for reason in readiness.reasons))
        with self.assertRaises(RuntimeError):
            PublicShopCollector(readiness).collect()

    def _config(self, shops, user_agent=USER_AGENT):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'shops.json'
            path.write_text(json.dumps({
                'timezone': 'Asia/Shanghai', 'origin': 'https://www.ozon.ru',
                'user_agent': user_agent, 'shops': shops,
            }), encoding='utf-8')
            return load_config(path)


class SafetyContractTests(unittest.TestCase):
    def test_timezone_dedupe_coverage_currency_and_baseline(self):
        self.assertEqual(report_day(_at(15, 59)), '2026-11-08')
        self.assertEqual(report_day(_at(16, 0)), '2026-11-09')
        with self.assertRaises(ValueError):
            report_day(datetime(2026, 11, 8, 16, 0))
        duplicate = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product(), _product()],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='未指定配送地区')
        self.assertTrue(duplicate.complete)
        self.assertEqual(len(duplicate.products), 1)
        conflict = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product(), _product(price='11.00')],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='未指定配送地区')
        self.assertFalse(conflict.complete)
        self.assertEqual(conflict.products, ())
        self.assertIn('冲突', conflict.error)
        partial = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product('new-sku')],
            coverage=_coverage(reached_end=False, truncated_by_limit=True, expected_pages=None),
            price_kind='public_listed', currency='RUB', region='莫斯科')
        self.assertFalse(partial.complete)
        self.assertIsNone(partial.error)
        self.assertEqual(partial.products[0].product_id, 'new-sku')
        mixed = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product(), _product('sku-2', currency='USD')],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='莫斯科')
        self.assertFalse(mixed.complete)
        self.assertIn('币种', mixed.error)
        unmeasured = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product()],
            coverage=_coverage(), price_kind='unmeasured', currency=None, region=None)
        self.assertFalse(unmeasured.complete)
        self.assertIn('未实测', unmeasured.scope)
        empty = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[], coverage=_coverage(),
            price_kind='public_listed', currency='RUB', region='莫斯科')
        self.assertFalse(empty.complete)
        self.assertIn('空目录', empty.error)
        confirmed_empty = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[], coverage=_coverage(),
            price_kind='public_listed', currency='RUB', region='莫斯科', explicit_empty=True)
        self.assertTrue(confirmed_empty.complete)
        self.assertEqual(confirmed_empty.products, ())
        blocked = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product()], coverage=_coverage(),
            price_kind='public_listed', currency='RUB', region='莫斯科', fetch_error='HTTP 307')
        self.assertEqual(blocked.products, ())
        self.assertFalse(blocked.complete)

    def test_failed_or_partial_results_do_not_occupy_snapshot_or_seen_set(self):
        first = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product('known')],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='莫斯科')
        failed = assemble_collection(
            store_id='shop', collected_at=_at(2), products=[_product('should-not-stick')],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='莫斯科',
            fetch_error='timeout')
        partial = assemble_collection(
            store_id='shop', collected_at=_at(3), products=[_product('partial-sku')],
            coverage=_coverage(reached_end=False, truncated_by_limit=True, expected_pages=None),
            price_kind='public_listed', currency='RUB', region='莫斯科')
        later = assemble_collection(
            store_id='shop', collected_at=datetime(2026, 11, 9, 1, tzinfo=timezone.utc),
            products=[_product('known'), _product('partial-sku'), _product('brand-new')],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='莫斯科')
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / 'db.sqlite3')
            self.assertEqual(store.save(failed), 'attempt')
            self.assertEqual(store.save(first), 'snapshot')
            self.assertEqual(store.save(first), 'replay')
            self.assertEqual(store.save(partial), 'attempt')
            self.assertEqual([item['day'] for item in store.history('shop', '2026-11-10')], [first.day])
            self.assertEqual(len(store.attempts('shop')), 2)
            delta = analyze(later, store.history('shop', later.day))
            self.assertEqual([item.product_id for item in delta['first_seen']], ['partial-sku', 'brand-new'])
            changed = assemble_collection(
                store_id='shop', collected_at=datetime.fromisoformat(later.collected_at),
                products=[_product('known', price='12.00')], coverage=_coverage(),
                price_kind='public_listed', currency='RUB', region='莫斯科')
            price_delta = analyze(changed, store.history('shop', changed.day))
            self.assertEqual(price_delta['price_changes'], [
                {'product_id': 'known', 'before': '10.00', 'after': '12.00', 'currency': 'RUB'}])
            self.assertEqual(store.save(replace(first, complete=False, error='later failure')), 'attempt')
            self.assertEqual(len(store.history('shop', '2026-11-10')), 1)
            with self.assertRaises(ValueError):
                store.save(replace(first, scope='另一个口径'))
            store.close()
        baseline = analyze(first, [])
        self.assertTrue(baseline['baseline'])
        self.assertEqual(baseline['first_seen'], [])
        self.assertIn('不计作当天上新', render(first, baseline))
        failure_text = render(failed, analyze(failed, [asdict(first)]))
        self.assertIn('无法判断', failure_text)
        self.assertNotIn('首次发现：0', failure_text)
        self.assertIn('不能解释为没有新品', failure_text)
        partial_text = render(partial, analyze(partial, [asdict(first)]))
        self.assertIn('至少 1', partial_text)
        self.assertIn('观测下限', partial_text)
        quiet = render(replace(partial, products=()), analyze(replace(partial, products=()), [asdict(first)]))
        self.assertIn('不能视为没有新品', quiet)
        self.assertNotIn('首次发现：0', quiet)
        other_region = replace(later, scope=first.scope.replace('莫斯科', '圣彼得堡'), day='2026-11-10')
        compared = analyze(other_region, [asdict(first)])
        self.assertEqual(compared['price_changes'], [])
        self.assertTrue(compared['baseline'])

    def test_second_writer_waits_instead_of_corrupting_snapshot(self):
        collection = assemble_collection(
            store_id='shop', collected_at=_at(1), products=[_product()],
            coverage=_coverage(), price_kind='public_listed', currency='RUB', region='莫斯科')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db.sqlite3'
            ready = SnapshotStore(path)
            ready.close()
            holder = sqlite3.connect(path, timeout=30, isolation_level=None)
            holder.execute('BEGIN EXCLUSIVE')
            result = {}

            def write():
                other = SnapshotStore(path)
                result['value'] = other.save(collection)
                other.close()

            thread = threading.Thread(target=write)
            thread.start()
            time.sleep(0.2)
            self.assertTrue(thread.is_alive())
            holder.rollback()
            holder.close()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result['value'], 'snapshot')
            store = SnapshotStore(path)
            self.assertEqual(len(store.history('shop', '2026-11-10')), 1)
            store.close()


class DailyReadinessTests(unittest.TestCase):
    def test_configured_shop_still_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = daily_main(['--config', 'config/shops.json', '--evidence', str(EVIDENCE)])
            self.assertEqual(code, 2)
            self.assertIn('5652967', output.getvalue())
            self.assertIn('未写入快照', output.getvalue())
            self.assertFalse((root / 'snapshots.sqlite3').exists())
            self.assertFalse(Path('data/snapshots.sqlite3').exists())
            empty = root / 'empty.json'
            empty.write_text(json.dumps({
                'timezone': 'Asia/Shanghai', 'origin': 'https://www.ozon.ru',
                'user_agent': USER_AGENT, 'shops': [],
            }), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily_main(['--config', str(empty), '--evidence', str(EVIDENCE)]), 0)
        self.assertEqual(report_day(datetime(2026, 11, 8, 1, tzinfo=ZoneInfo('Asia/Shanghai'))), '2026-11-08')


if __name__ == '__main__':
    unittest.main()
