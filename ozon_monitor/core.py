from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
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
    collected_at: str | None = None
    coverage_note: str | None = None

    def __post_init__(self):
        date.fromisoformat(self.day)
        if not self.store_id or not self.source or not self.scope:
            raise ValueError('店铺、数据来源、采集口径必填')
        if len({p.product_id for p in self.products}) != len(self.products):
            raise ValueError('单次采集不允许重复商品 ID')
        if self.complete and self.error:
            raise ValueError('失败采集不能标记为完整')
        if self.collected_at is not None:
            parsed = datetime.fromisoformat(self.collected_at)
            if parsed.tzinfo is None:
                raise ValueError('collected_at 必须带时区')
        if self.complete and not self.source.startswith('fixture'):
            note = self.coverage_note or ''
            if not self.collected_at or not note.startswith('已覆盖:'):
                raise ValueError('非模拟完整采集必须记录带时区采集时间与已覆盖分页')
            if '未实测' in self.scope:
                raise ValueError('价格口径未实测不能标记完整')
            if not self.products and 'explicit-empty' not in note:
                raise ValueError('空结果缺少已实测的空目录标记，不能标记完整')


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
        # Autocommit so writers can take an explicit immediate lock.
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            store TEXT NOT NULL, day TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(store, day))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL, day TEXT NOT NULL, complete INTEGER NOT NULL,
            error TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def close(self):
        self.db.close()

    def history(self, store: str, before: str) -> list[dict]:
        """Return comparable snapshots only. Attempt rows are not baseline history."""
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM snapshots WHERE store=? AND day<? ORDER BY day', (store, before))]

    def attempts(self, store: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT body FROM attempts WHERE store=? ORDER BY id', (store,))]

    def save(self, result: Collection) -> str:
        """Persist a complete success as the daily snapshot.

        Identical reruns do not write. Failed or partial results are appended
        to the attempt audit only, so they cannot occupy or replace the snapshot.
        """
        body = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
        if not result.complete or result.error:
            self._attempt(result, body)
            return 'attempt'
        self.db.execute('BEGIN IMMEDIATE')
        try:
            existing = self.db.execute(
                'SELECT body FROM snapshots WHERE store=? AND day=?',
                (result.store_id, result.day)).fetchone()
            if existing:
                same = existing[0] == body
                self.db.rollback()
            else:
                same = None
                self.db.execute('INSERT INTO snapshots VALUES (?,?,?)',
                                (result.store_id, result.day, body))
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if same is False:
            raise ValueError('同店同日快照已存在且内容不同；保留原始快照')
        return 'replay' if same else 'snapshot'

    def _attempt(self, result: Collection, body: str):
        created_at = datetime.now(timezone.utc).isoformat()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute(
                '''INSERT INTO attempts(store, day, complete, error, body, created_at)
                   VALUES (?,?,?,?,?,?)''',
                (result.store_id, result.day, int(result.complete), result.error, body, created_at))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


def analyze(current: Collection, history: list[dict]) -> dict:
    # Compare only successful complete runs with identical source and price scope.
    comparable = [h for h in history if h['complete'] and not h.get('error')
                  and h['source'] == current.source and h['scope'] == current.scope]
    previous = comparable[-1] if comparable else None
    known = {p['product_id'] for h in history if not h.get('error') for p in h['products']}
    first_seen = ([p for p in current.products if p.product_id not in known]
                  if previous is not None and not current.error else [])
    if current.error:
        discovery_status = 'unknown_failure'
        first_seen = []
    elif not current.complete:
        discovery_status = 'incomplete_lower_bound'
    elif previous is None:
        discovery_status = 'baseline'
    else:
        discovery_status = 'compared'
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
            'discovery_status': discovery_status, 'first_seen': first_seen,
            'price_changes': changes, 'category_counts': groups,
            'category_level_counts': [
                {name: sum(1 for p in first_seen if ' / '.join(c or '未知' for c in p.categories[:level]) == name)
                 for name in {' / '.join(c or '未知' for c in p.categories[:level]) for p in first_seen}}
                for level in (1, 2, 3)]}


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
        lines.append('首次建立可比基线；本次商品不计作当天上新。' if current.complete and not current.error
                     else '尚无完整可比基线；本次不建立基准、不报告商品变化。')
    else:
        lines.append(f'对比基线：{delta["previous_day"]}（可能不是昨日）')
    lines.extend(['', _discovery_heading(current, delta)])
    for p in delta['first_seen']:
        lines.append(f'- {p.product_id}：{p.title or "标题未知"}；价格 {p.price or "未知"} '
                     f'{p.currency or ""}；优惠条件 {p.offer_conditions or "未知"}；'
                     f'真实上架日期 {p.published_at or "未知"}；证据 {p.published_at_source or "无"}；'
                     f'链接 {p.product_url or "未知"}')
    lines.extend(['', f'## 可比价格变化：{len(delta["price_changes"])}'])
    for change in delta['price_changes']:
        lines.append(f'- {change["product_id"]}：{change["before"]} → {change["after"]} {change["currency"]}')
    if delta['discovery_status'] == 'unknown_failure':
        lines.extend(['', '## 首次发现商品的类目分布', '不适用：采集失败，不能解释为没有新品。'])
    else:
        lines.extend(['', '## 首次发现商品的类目分布（不代表真实集中上新）'])
        for level, counts in enumerate(delta['category_level_counts'], 1):
            lines.append(f'### {level} 级类目（保留父级路径）')
            lines.extend(f'- {name}：{count}' for name, count in sorted(counts.items()))
    if current.error:
        lines.append('本次失败结果不写入可比快照，不能解释为没有新品。')
    elif not current.complete:
        lines.append('采集不完整：首次发现与类目数量仅为本次观测下限，暂停价格比较。')
    return '\n'.join(lines) + '\n'


def _discovery_heading(current: Collection, delta: dict) -> str:
    count = len(delta['first_seen'])
    status = delta['discovery_status']
    if status == 'unknown_failure':
        return '## 首次发现：无法判断（采集失败，不能解释为没有新品）'
    if status == 'incomplete_lower_bound':
        if count:
            return f'## 首次发现：至少 {count}（采集不完整，不能据此认为没有其他新品）'
        return '## 首次发现：无法判断（采集不完整，本次未观测到新商品，不能视为没有新品）'
    if status == 'baseline':
        return f'## 首次发现：{count}'
    return f'## 首次发现：{count}（本次完整采集范围内的新商品）'
