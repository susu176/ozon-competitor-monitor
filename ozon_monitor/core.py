from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Product:
    product_id: str
    title: str | None = None
    price: str | None = None
    currency: str | None = None
    offer_conditions: str | None = None
    image_url: str | None = None
    product_url: str | None = None
    categories: tuple[str | None, str | None, str | None] = (None, None, None)
    published_at: str | None = None
    published_at_source: str | None = None

    def __post_init__(self):
        if not self.product_id or len(self.categories) != 3:
            raise ValueError('商品 ID 必填；类目必须保留三个层级（未知用 null）')
        if self.price is not None:
            value = Decimal(self.price)
            if not value.is_finite() or value < 0 or not self.currency:
                raise ValueError('价格必须为非负有限数且有币种')
        if bool(self.published_at) != bool(self.published_at_source):
            raise ValueError('真实上架日期与来源证据必须同时提供')


@dataclass(frozen=True)
class Collection:
    store_id: str
    day: str
    products: tuple[Product, ...]
    complete: bool
    source: str
    scope: str
    error: str | None = None

    def __post_init__(self):
        date.fromisoformat(self.day)
        if not self.store_id or not self.source or not self.scope:
            raise ValueError('店铺、数据来源、采集口径必填')
        if len({p.product_id for p in self.products}) != len(self.products):
            raise ValueError('单次采集不允许重复商品 ID')
        if self.complete and self.error:
            raise ValueError('失败采集不能标记为完整')


class Collector(Protocol):
    def collect(self) -> Collection: ...


class FixtureCollector:
    def __init__(self, path: Path):
        self.path = path

    def collect(self) -> Collection:
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        products = tuple(Product(**{**p, 'categories': tuple(p.get('categories', [None] * 3))})
                         for p in raw.pop('products'))
        return Collection(products=products, **raw)


class SnapshotStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            store TEXT NOT NULL, day TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(store, day))''')
        self.db.commit()

    def close(self):
        self.db.close()

    def history(self, store: str, before: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM snapshots WHERE store=? AND day<? ORDER BY day', (store, before))]

    def save(self, result: Collection):
        body = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
        existing = self.db.execute('SELECT body FROM snapshots WHERE store=? AND day=?',
                                   (result.store_id, result.day)).fetchone()
        if existing and existing[0] != body:
            raise ValueError('同店同日快照已存在且内容不同；保留原始快照')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',
                            (result.store_id, result.day, body))


def analyze(current: Collection, history: list[dict]) -> dict:
    # Compare only successful complete runs with identical source and price scope.
    comparable = [h for h in history if h['complete'] and not h.get('error')
                  and h['source'] == current.source and h['scope'] == current.scope]
    previous = comparable[-1] if comparable else None
    known = {p['product_id'] for h in history for p in h['products']}
    first_seen = [p for p in current.products if p.product_id not in known]
    old = {p['product_id']: p for p in previous['products']} if previous else {}
    changes = []
    if current.complete and not current.error:
        for p in current.products:
            prior = old.get(p.product_id)
            if (prior and prior['price'] is not None and p.price is not None
                    and prior['currency'] == p.currency
                    and prior['offer_conditions'] == p.offer_conditions
                    and Decimal(prior['price']) != Decimal(p.price)):
                changes.append({'product_id': p.product_id, 'before': prior['price'],
                                'after': p.price, 'currency': p.currency})
    groups: dict[str, int] = {}
    for p in first_seen:
        category = ' / '.join(c or '未知' for c in p.categories)
        groups[category] = groups.get(category, 0) + 1
    return {'baseline': previous is None, 'previous_day': previous['day'] if previous else None,
            'first_seen': first_seen, 'price_changes': changes, 'category_counts': groups}


def render(current: Collection, delta: dict) -> str:
    lines = [f'# Ozon 竞店日报 {current.day}', '', f'店铺：{current.store_id}',
             f'数据来源：{current.source}；口径：{current.scope}',
             f'采集状态：{"完整" if current.complete else "不完整"}；观测商品：{len(current.products)}',
             '说明：首次发现不代表真实上架；未知字段保留为空。缺失商品不推断为下架。']
    if current.source.startswith('fixture'):
        lines.append('【模拟数据】本报告不代表真实 Ozon 店铺。')
    if current.error:
        lines.append(f'采集异常：{current.error}')
    if delta['baseline']:
        lines.append('首次建立可比基线；本次商品不计作当天上新。')
    else:
        lines.append(f'对比基线：{delta["previous_day"]}（可能不是昨日）')
    lines.extend(['', f'## 首次发现：{len(delta["first_seen"])}'])
    for p in delta['first_seen']:
        lines.append(f'- {p.product_id}：{p.title or "标题未知"}；价格 {p.price or "未知"} '
                     f'{p.currency or ""}；优惠条件 {p.offer_conditions or "未知"}；'
                     f'真实上架日期 {p.published_at or "未知"}；证据 {p.published_at_source or "无"}；'
                     f'链接 {p.product_url or "未知"}')
    lines.extend(['', f'## 可比价格变化：{len(delta["price_changes"])}'])
    for change in delta['price_changes']:
        lines.append(f'- {change["product_id"]}：{change["before"]} → {change["after"]} {change["currency"]}')
    lines.extend(['', '## 首次发现商品的类目分布（不代表真实集中上新）'])
    lines.extend(f'- {name}：{count}' for name, count in sorted(delta['category_counts'].items()))
    if not current.complete:
        lines.append('采集不完整：首次发现与类目数量仅为本次观测下限，暂停价格比较。')
    return '\n'.join(lines) + '\n'
