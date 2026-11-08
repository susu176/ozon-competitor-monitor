"""Safety shell for a future public-page collector.

No HTML parser is implemented. Collection stays disabled until a measured
field profile and a real parser version exist; both are empty in this change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .access import BLOCKING_OUTCOMES, access_permitted
from .config import MonitorConfig, ShopConfig
from .core import Collection, Product


SHANGHAI = ZoneInfo('Asia/Shanghai')
# Field profiles have not been measured, so no parser may claim support.
SUPPORTED_PARSERS: frozenset[str] = frozenset()
PUBLIC_SOURCE = 'public-shop-page'


class CollectorNotReady(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]):
        super().__init__('；'.join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PageCoverage:
    sort: str
    pages_fetched: int
    reached_end: bool
    truncated_by_limit: bool
    expected_pages: int | None = None


def report_day(collected_at: datetime) -> str:
    if collected_at.tzinfo is None:
        raise ValueError('采集时间必须带时区')
    return collected_at.astimezone(SHANGHAI).date().isoformat()


def dedupe_products(products: list[Product]) -> tuple[tuple[Product, ...], str | None]:
    seen: dict[str, Product] = {}
    order: list[str] = []
    for product in products:
        previous = seen.get(product.product_id)
        if previous is None:
            seen[product.product_id] = product
            order.append(product.product_id)
        elif previous != product:
            return (), f'商品 {product.product_id} 跨页字段冲突，拒绝猜测保留哪一条'
    return tuple(seen[product_id] for product_id in order), None


def assess_coverage(coverage: PageCoverage) -> tuple[bool, str]:
    if coverage.sort != 'newest':
        return False, '未实测的排序不能标记完整'
    if coverage.pages_fetched < 1:
        return False, '未读取到任何页面'
    if coverage.truncated_by_limit or not coverage.reached_end:
        return False, '未覆盖到本次声明的分页边界'
    if coverage.expected_pages is None or coverage.expected_pages != coverage.pages_fetched:
        return False, '页数与覆盖声明不一致'
    return True, f'已覆盖:newest:pages={coverage.pages_fetched}'


def scope_text(kind: str, currency: str | None, region: str | None, *, empty: bool = False) -> str:
    if empty:
        if not region:
            return '价格口径未实测'
        return f'空目录已确认;地区:{region}'
    if kind == 'unmeasured' or not currency or not region:
        return '价格口径未实测'
    return f'公开页面标价口径:{kind};币种:{currency};地区:{region}'


def currency_conflict(products: tuple[Product, ...]) -> str | None:
    currencies = {product.currency for product in products if product.price is not None}
    if len(currencies) > 1:
        return '同一采集出现多种币种，不能当作同一价格口径'
    return None


def assemble_collection(*, store_id: str, collected_at: datetime, products: list[Product],
                        coverage: PageCoverage, price_kind: str, currency: str | None,
                        region: str | None, fetch_error: str | None = None,
                        explicit_empty: bool = False) -> Collection:
    """Build a Collection from already parsed records. Does not read HTML or the network."""
    day = report_day(collected_at)
    stamp = collected_at.isoformat()
    covered, coverage_note = assess_coverage(coverage)
    if fetch_error:
        return _incomplete(store_id, day, stamp, '价格口径未实测', fetch_error, '未覆盖:请求失败')
    deduped, dedupe_error = dedupe_products(products)
    if dedupe_error:
        return _incomplete(store_id, day, stamp, '价格口径未实测', dedupe_error, coverage_note)
    conflict = currency_conflict(deduped)
    if conflict:
        return _incomplete(store_id, day, stamp, '价格口径未实测', conflict, coverage_note)
    if not deduped and not explicit_empty:
        return _incomplete(store_id, day, stamp, '价格口径未实测',
                           '空结果缺少已实测的空目录标记，拒绝建立基线', coverage_note)
    scope = scope_text(price_kind, currency, region, empty=not deduped)
    if not covered or '未实测' in scope or not coverage_note.startswith('已覆盖:'):
        note = coverage_note
        if note.startswith('已覆盖:'):
            note = '未覆盖:价格或分页口径不足'
        elif not note.startswith('未'):
            note = f'未覆盖:{note}'
        return Collection(store_id, day, deduped, False, PUBLIC_SOURCE, scope,
                          error=None, collected_at=stamp, coverage_note=note)
    note = coverage_note + (':explicit-empty' if not deduped else '')
    return Collection(store_id, day, deduped, True, PUBLIC_SOURCE, scope, collected_at=stamp, coverage_note=note)


def _incomplete(store_id: str, day: str, stamp: str, scope: str, error: str | None,
                coverage_note: str) -> Collection:
    note = coverage_note if coverage_note.startswith('未') else f'未覆盖:{coverage_note}'
    return Collection(store_id, day, (), False, PUBLIC_SOURCE, scope, error=error,
                      collected_at=stamp, coverage_note=note)


def load_evidence_records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(raw, list):
        return raw
    return list(raw.get('requests', []))


def assess_readiness(config: MonitorConfig, evidence_records: list[dict],
                     measurements: list[dict] | None = None) -> Readiness:
    """Report why live collection must stay off. Ready only if every gate passes."""
    reasons: list[str] = []
    if not config.shops:
        reasons.append('未提供店铺 URL')
    blocking = [record for record in evidence_records if record.get('outcome') in BLOCKING_OUTCOMES]
    successful_robots = [record for record in evidence_records
                         if record.get('outcome') == 'ok' and str(record.get('requested_url', '')).endswith('/robots.txt')]
    if blocking and not successful_robots:
        observed = '、'.join(f'{record.get("requested_url")}={record.get("outcome")}' for record in blocking)
        reasons.append(f'公开规则或条款读取未成功：{observed}')
    elif not successful_robots:
        reasons.append('没有成功的 robots.txt 正文，拒绝请求店铺页')
    if not SUPPORTED_PARSERS:
        reasons.append('没有基于实测字段实现的公开页解析器')
    for shop in config.shops:
        reasons.extend(_measurement_gaps(shop, measurements or [], successful_robots))
    unique = tuple(dict.fromkeys(reasons))
    return Readiness(ready=not unique, reasons=unique)


def _measurement_gaps(shop: ShopConfig, measurements: list[dict], robots_records: list[dict]) -> list[str]:
    gaps = []
    matched = [item for item in measurements if item.get('url') == shop.url and item.get('store_id') == shop.store_id]
    if not matched:
        gaps.append(f'{shop.store_id} 没有与 URL 对应的字段实测记录')
        return gaps
    item = matched[-1]
    if item.get('confirmed') is not True:
        gaps.append(f'{shop.store_id} 的字段实测尚未确认')
    if item.get('parser') not in SUPPORTED_PARSERS:
        gaps.append(f'{shop.store_id} 的解析器版本没有实现')
    if not robots_records:
        gaps.append(f'{shop.store_id} 不能在 robots 未成功时抓取')
    else:
        decision = access_permitted('ok', robots_records[-1].get('body'), shop.url, 'ozon-competitor-monitor')
        if not decision.permitted:
            gaps.append(f'{shop.store_id}：{decision.reason}')
    return gaps


class PublicShopCollector:
    """Collector implementation that refuses to invent a page parse."""

    def __init__(self, readiness: Readiness):
        self.readiness = readiness

    def collect(self) -> Collection:
        reasons = list(self.readiness.reasons)
        if not SUPPORTED_PARSERS:
            reasons.append('没有基于实测字段实现的公开页解析器')
        raise CollectorNotReady(tuple(dict.fromkeys(reasons)) or ('采集器未就绪',))
