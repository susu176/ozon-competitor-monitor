"""Shop configuration. An empty shop list is valid and means no URL was supplied."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


ALLOWED_HOSTS = frozenset({'www.ozon.ru', 'ozon.ru'})
ORIGIN = 'https://www.ozon.ru'


@dataclass(frozen=True)
class ShopConfig:
    store_id: str
    url: str
    sort: str = 'newest'


@dataclass(frozen=True)
class MonitorConfig:
    timezone: str
    origin: str
    user_agent: str
    shops: tuple[ShopConfig, ...]


def load_config(path: Path) -> MonitorConfig:
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('timezone') != 'Asia/Shanghai':
        raise ValueError('报告时区必须是 Asia/Shanghai')
    if raw.get('origin') != ORIGIN:
        raise ValueError('规则检查源站必须是 https://www.ozon.ru')
    if not isinstance(raw.get('shops'), list):
        raise ValueError('shops 必须是数组；没有店铺时使用空数组')
    user_agent = raw.get('user_agent')
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError('user_agent 必填，且不得伪装成浏览器')
    lowered = user_agent.lower()
    if 'mozilla/' in lowered or 'chrome/' in lowered or 'safari/' in lowered:
        raise ValueError('user_agent 不得伪装成浏览器')
    return MonitorConfig(raw['timezone'], raw['origin'], user_agent,
                         tuple(_shop(item) for item in raw['shops']))


def validate_shop_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS:
        raise ValueError('店铺 URL 必须是 https://www.ozon.ru 或 https://ozon.ru 上的公开页面')
    if parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('店铺 URL 不能携带凭据或非标准端口')
    if not parts.path or parts.path == '/':
        raise ValueError('店铺 URL 需要具体页面路径；站点根不是店铺')
    query = parts.query.lower()
    if any(token in query for token in ('token', 'key', 'auth', 'cookie', 'session')):
        raise ValueError('店铺 URL 不能携带凭据参数')
    return url


def _shop(item: dict) -> ShopConfig:
    store_id = str(item.get('store_id') or '')
    if not store_id or '/' in store_id or '\\' in store_id:
        raise ValueError('store_id 必填且不能是路径')
    if item.get('sort', 'newest') != 'newest':
        raise ValueError('当前只接受公开店铺页最新排序；其他排序尚未实测')
    return ShopConfig(store_id, validate_shop_url(item.get('url', '')), 'newest')
