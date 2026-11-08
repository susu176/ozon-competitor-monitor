"""Classify one public HTTP response and apply a fetched robots.txt.

This module does not retry, follow challenge redirects, store cookies, or
interpret a blocked response as permission to fetch shop pages.
"""
from __future__ import annotations

import hashlib
import http.client
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


BLOCKING_OUTCOMES = frozenset({
    'redirect_challenge', 'blocked', 'captcha', 'rate_limited', 'timeout', 'error', 'unexpected',
})
USER_AGENT = 'ozon-competitor-monitor/0.1 (public read; stops on block; no browser impersonation)'


@dataclass(frozen=True)
class RobotsDecision:
    permitted: bool
    reason: str


def classify_response(status: int | None, content_type: str | None, body: str,
                      location: str | None, retry_after: str | None = None) -> str:
    """Map one response to a stop/continue label. This is not a bypass."""
    if status == 429 or (status == 503 and retry_after):
        return 'rate_limited'
    if status in {401, 403, 451}:
        return 'blocked'
    if status in {301, 302, 303, 307, 308} and '__rr=' in (location or ''):
        return 'redirect_challenge'
    lowered = body.lower()
    if any(marker in lowered for marker in ('captcha', 'antibot', 'challenge page', 'access denied')):
        return 'captcha'
    if status == 200 and content_type and 'text/plain' in content_type.lower():
        if 'user-agent' in lowered or 'disallow' in lowered:
            return 'ok'
        return 'unexpected'
    if status == 200:
        return 'http_ok_unparsed'
    return 'unexpected'


def fetch_once(url: str, user_agent: str = USER_AGENT, timeout: float = 20.0,
               max_bytes: int = 50_000) -> dict:
    """Read one response and do not follow Location."""
    parts = urlsplit(url)
    local = parts.hostname in {'127.0.0.1', 'localhost'}
    if parts.scheme == 'https':
        connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout)
    elif parts.scheme == 'http' and local:
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    else:
        raise ValueError('只读取 https 公开地址；本地测试可使用 127.0.0.1')
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    started = datetime.now(timezone.utc).isoformat()
    try:
        connection.request('GET', path, headers={
            'User-Agent': user_agent,
            'Accept': 'text/plain, text/html;q=0.5',
            'Connection': 'close',
        })
        response = connection.getresponse()
        pairs = response.getheaders()
        body = response.read(max_bytes + 1)
        status = response.status
        final_url = url
    except (TimeoutError, socket.timeout, ssl.SSLError, http.client.HTTPException, OSError) as error:
        return _transport_failure(url, user_agent, started, error)
    finally:
        connection.close()
    truncated = len(body) > max_bytes
    body = body[:max_bytes]
    text = body.decode('utf-8', errors='replace')
    header_map: dict[str, str] = {}
    set_cookie = False
    for key, value in pairs:
        lowered = key.lower()
        if lowered == 'set-cookie':
            set_cookie = True
            continue
        header_map.setdefault(lowered, value)
    location = header_map.get('location')
    content_type = header_map.get('content-type')
    outcome = classify_response(status, content_type, text, location, header_map.get('retry-after'))
    kept = {key: header_map[key] for key in (
        'content-type', 'content-length', 'date', 'server', 'retry-after', 'location', 'cache-control')
        if key in header_map}
    return {
        'requested_url': url,
        'final_url': final_url,
        'started_at': started,
        'user_agent': user_agent,
        'status': status,
        'headers': kept,
        'set_cookie_present': set_cookie,
        'bytes_read': len(body),
        'truncated': truncated,
        'sha256': hashlib.sha256(body).hexdigest(),
        'body_excerpt': text[:500],
        'outcome': outcome,
        'redirects_followed': False,
        'parsed_products': None,
    }


def _transport_failure(url: str, user_agent: str, started: str, error: Exception) -> dict:
    timed_out = isinstance(error, (TimeoutError, socket.timeout)) or 'timed out' in str(error).lower()
    return {
        'requested_url': url,
        'started_at': started,
        'user_agent': user_agent,
        'outcome': 'timeout' if timed_out else 'error',
        'error': type(error).__name__,
        'detail': str(error)[:300],
        'set_cookie_present': False,
        'redirects_followed': False,
        'parsed_products': None,
    }


def robots_decision(robots_text: str | None, url: str, user_agent: str) -> RobotsDecision:
    """Decide from an already fetched robots body. Unusable text refuses access."""
    if not robots_text or 'user-agent' not in robots_text.lower():
        return RobotsDecision(False, '没有可用的 robots.txt 正文，拒绝请求店铺页')
    groups = _parse_robots(robots_text)
    agent = user_agent.split('/', 1)[0].lower()
    selected = _select_group(groups, agent)
    if selected is None:
        return RobotsDecision(True, '已读取的 robots.txt 没有匹配分组，按文件本身允许')
    crawl_delay = selected.get('crawl_delay')
    if crawl_delay:
        return RobotsDecision(False, f'robots 声明 Crawl-delay {crawl_delay}，未配置不低于该值的间隔，拒绝连续抓取')
    path = urlsplit(url).path or '/'
    if urlsplit(url).query:
        path += '?' + urlsplit(url).query
    allowed = _path_allowed(selected['rules'], path)
    if allowed is None:
        return RobotsDecision(False, 'robots 规则冲突，拒绝请求')
    if not allowed:
        return RobotsDecision(False, 'robots.txt 不允许该路径')
    return RobotsDecision(True, 'robots.txt 允许该路径')


def access_permitted(outcome: str, robots_text: str | None, url: str, user_agent: str) -> RobotsDecision:
    if outcome != 'ok':
        return RobotsDecision(False, f'robots 读取结果为 {outcome}，拒绝请求店铺页')
    return robots_decision(robots_text, url, user_agent)


def recent_block(records: list[dict], host: str, now: datetime | None = None,
                 window: timedelta = timedelta(hours=24)) -> str | None:
    current = now or datetime.now(timezone.utc)
    for record in records:
        record_host = urlsplit(record.get('requested_url', '')).hostname
        if record_host != host or record.get('outcome') not in BLOCKING_OUTCOMES:
            continue
        started = datetime.fromisoformat(record['started_at'])
        if current - started < window:
            return (f'{host} 在 {record["started_at"]} 的结果为 {record.get("outcome")}，'
                    '24 小时内不再请求')
    return None


def _parse_robots(text: str) -> list[dict]:
    groups: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        key, value = (part.strip() for part in line.split(':', 1))
        name = key.lower()
        if name == 'user-agent':
            if current is None or current['rules'] or current.get('crawl_delay'):
                current = {'agents': [], 'rules': [], 'crawl_delay': None}
                groups.append(current)
            current['agents'].append(value.lower())
        elif current is None:
            continue
        elif name in {'allow', 'disallow'}:
            current['rules'].append((name, value))
        elif name == 'crawl-delay':
            current['crawl_delay'] = value
    return groups


def _select_group(groups: list[dict], agent: str) -> dict | None:
    specific = [group for group in groups if any(item != '*' and item in agent for item in group['agents'])]
    if specific:
        return max(specific, key=lambda group: max(len(item) for item in group['agents'] if item != '*'))
    wildcard = [group for group in groups if '*' in group['agents']]
    return wildcard[0] if wildcard else None


def _path_allowed(rules: list[tuple[str, str]], path: str) -> bool | None:
    matches = [(len(rule), directive) for directive, rule in rules if rule and path.startswith(rule)]
    if not matches:
        blocked = any(directive == 'disallow' and rule == '/' for directive, rule in rules)
        return not blocked
    longest = max(length for length, _ in matches)
    kinds = {directive for length, directive in matches if length == longest}
    if len(kinds) != 1:
        return None
    return 'allow' in kinds
