"""Pick products from a table the user already has. This does not open Ozon."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
RUN_HOURS = (9, 11, 13, 15, 17, 19, 21)


@dataclass(frozen=True)
class Candidate:
    product_id: str
    title: str | None
    price: str | None
    currency: str | None
    product_url: str | None
    monthly_sales: int | None


@dataclass(frozen=True)
class PassRules:
    currency: str
    min_price: str
    max_price: str
    min_monthly_sales: int

    def __post_init__(self):
        if not self.currency or Decimal(self.min_price) < 0 or Decimal(self.max_price) < Decimal(self.min_price):
            raise ValueError('过审规则里的币种和价格区间不正确')
        if self.min_monthly_sales < 0:
            raise ValueError('月销量门槛不能为负')


def load_rules(path: Path) -> PassRules:
    raw = json.loads(path.read_text(encoding='utf-8'))
    return PassRules(raw['currency'], raw['min_price'], raw['max_price'], int(raw['min_monthly_sales']))


def load_candidates(path: Path) -> tuple[Candidate, ...]:
    raw = json.loads(path.read_text(encoding='utf-8'))
    rows = raw['products'] if isinstance(raw, dict) else raw
    found = []
    seen = set()
    for row in rows:
        product_id = str(row.get('product_id') or '')
        if not product_id or product_id in seen:
            raise ValueError('商品 ID 必填且不能重复')
        seen.add(product_id)
        sales = row.get('monthly_sales')
        found.append(Candidate(
            product_id, row.get('title'), row.get('price'), row.get('currency'), row.get('product_url'),
            None if sales is None else int(sales)))
    return tuple(found)


def review(candidates: tuple[Candidate, ...], rules: PassRules) -> dict:
    passed, rejected = [], []
    for item in candidates:
        reasons = []
        if not item.title:
            reasons.append('没有商品名')
        if not item.product_url:
            reasons.append('没有商品链接')
        if item.price is None or item.currency is None:
            reasons.append('价格或币种未知')
        elif item.currency != rules.currency:
            reasons.append(f'币种是 {item.currency}，规则要求 {rules.currency}')
        else:
            price = Decimal(item.price)
            if price < Decimal(rules.min_price) or price > Decimal(rules.max_price):
                reasons.append(f'价格 {item.price} 不在 {rules.min_price} 到 {rules.max_price}')
        if item.monthly_sales is None:
            reasons.append('月销量未知，不能过审')
        elif item.monthly_sales < rules.min_monthly_sales:
            reasons.append(f'月销量 {item.monthly_sales} 低于 {rules.min_monthly_sales}')
        if reasons:
            rejected.append({'product': item, 'reasons': reasons})
        else:
            passed.append(item)
    return {'passed': passed, 'rejected': rejected}


def new_passes(passed: list[Candidate], already: set[str]) -> list[Candidate]:
    return [item for item in passed if item.product_id not in already]


def due_at(moment: datetime) -> bool:
    if moment.tzinfo is None:
        raise ValueError('时间必须带时区')
    local = moment.astimezone(SHANGHAI)
    return local.hour in RUN_HOURS and local.minute == 0


def render(result: dict, fresh: list[Candidate], source_note: str) -> str:
    lines = ['# Ozon 选品过审结果', '', source_note,
             '过审只表示符合你写下的价格和月销量规则，不是 Ozon 官方审核。',
             f'过审：{len(result["passed"])}', f'未过审：{len(result["rejected"])}',
             f'这次新过审：{len(fresh)}', '', '## 这次新过审']
    if not fresh:
        lines.append('没有新过审的商品。')
    for item in fresh:
        lines.append(f'- {item.product_id}：{item.title}；{item.price} {item.currency}；月销量 {item.monthly_sales}；{item.product_url}')
    lines.extend(['', '## 未过审'])
    if not result['rejected']:
        lines.append('没有被挡下的商品。')
    for row in result['rejected']:
        item = row['product']
        lines.append(f'- {item.product_id}：{"；".join(row["reasons"])}')
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='根据商品表做选品过审。不打开 Ozon，不发送消息。')
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--rules', type=Path, default=Path('config/selection-rules.json'))
    parser.add_argument('--seen', type=Path)
    parser.add_argument('--output', type=Path, default=Path('reports/selection.md'))
    args = parser.parse_args(argv)
    result = review(load_candidates(args.input), load_rules(args.rules))
    already = set()
    if args.seen and args.seen.is_file():
        already = set(json.loads(args.seen.read_text(encoding='utf-8')))
    fresh = new_passes(result['passed'], already)
    note = '数据来自你提供的商品表，不是程序打开 Ozon 得到的。'
    text = render(result, fresh, note)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding='utf-8')
    if args.seen:
        args.seen.parent.mkdir(parents=True, exist_ok=True)
        args.seen.write_text(json.dumps(sorted(already | {item.product_id for item in result['passed']}),
                                        ensure_ascii=False, indent=2), encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
