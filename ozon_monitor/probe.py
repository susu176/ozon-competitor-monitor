"""Manual one-shot probe. Refuses another request for 24 hours after a block."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .access import USER_AGENT, access_permitted, fetch_once, recent_block
from .config import ORIGIN, validate_shop_url
from .prep import load_evidence_records


TERMS_URL = 'https://docs.ozon.ru/legal/terms-of-use/site/'
DEFAULT_EVIDENCE = Path('docs/evidence/2026-11-08-public-access.json')


def load_evidence_file(path: Path) -> list[dict]:
    return load_evidence_records(path)


def run_robots(records: list[dict], fetcher=fetch_once, now: datetime | None = None) -> dict:
    blocked = recent_block(records, 'www.ozon.ru', now)
    if blocked:
        return {'fetched': False, 'reason': blocked, 'parsed_products': None}
    observation = fetcher(ORIGIN + '/robots.txt', USER_AGENT)
    return {'fetched': True, 'observation': observation, 'parsed_products': None}


def run_terms(records: list[dict], fetcher=fetch_once, now: datetime | None = None) -> dict:
    blocked = recent_block(records, 'docs.ozon.ru', now)
    if blocked:
        return {'fetched': False, 'reason': blocked, 'parsed_products': None}
    observation = fetcher(TERMS_URL, USER_AGENT)
    return {'fetched': True, 'observation': observation, 'parsed_products': None}


def run_page(url: str, records: list[dict], robots_outcome: str, robots_text: str | None,
             fetcher=fetch_once, now: datetime | None = None) -> dict:
    validate_shop_url(url)
    host = urlsplit(url).hostname or ''
    blocked = recent_block(records, host, now)
    if blocked:
        return {'fetched': False, 'reason': blocked, 'parsed_products': None}
    decision = access_permitted(robots_outcome, robots_text, url, USER_AGENT)
    if not decision.permitted:
        return {'fetched': False, 'reason': decision.reason, 'parsed_products': None}
    observation = fetcher(url, USER_AGENT)
    return {'fetched': True, 'observation': observation, 'parsed_products': None,
            'note': '响应未解析为商品；解析器尚未实现'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='低频公开页探针：不跟随挑战跳转，不解析商品，不使用代理')
    parser.add_argument('--evidence', type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument('--fetch', action='store_true', help='实际发出一次请求；省略时不访问网络')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('robots')
    sub.add_parser('terms')
    page = sub.add_parser('page')
    page.add_argument('--url', required=True)
    page.add_argument('--robots-outcome', default='missing')
    page.add_argument('--robots-body', default='')
    args = parser.parse_args(argv)
    records = load_evidence_file(args.evidence) if args.evidence.is_file() else []
    fetcher = fetch_once if args.fetch else _fetch_disabled
    try:
        if args.command == 'robots':
            result = run_robots(records, fetcher)
        elif args.command == 'terms':
            result = run_terms(records, fetcher)
        else:
            result = run_page(args.url, records, args.robots_outcome, args.robots_body or None, fetcher)
    except RuntimeError as error:
        result = {'fetched': False, 'reason': str(error), 'parsed_products': None}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get('fetched'):
        return 2
    outcome = result['observation'].get('outcome')
    if outcome in {'redirect_challenge', 'blocked', 'captcha', 'rate_limited', 'timeout', 'error', 'unexpected'}:
        return 3
    return 0


def _fetch_disabled(url: str, user_agent: str):
    raise RuntimeError('需要 --fetch 才会发出一次请求')


if __name__ == '__main__':
    raise SystemExit(main())
