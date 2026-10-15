"""Git data branch persistence, fail closed on missing/corrupt history.

Uses a separate disposable Git directory, never a checkout of application code.
No force pushes; a concurrent writer causes failure rather than lost history.
"""
import argparse
import json
import sqlite3
import subprocess
from pathlib import Path


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def validate(path):
    if not path.is_file():
        raise ValueError('历史数据库丢失，拒绝重置基准')
    connection = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        rows = connection.execute('SELECT store, day, body FROM snapshots').fetchall()
        if not rows:
            raise ValueError('历史数据库为空，拒绝重置基准')
        from .core import Collection, Product
        for store, day, body in rows:
            raw = json.loads(body)
            products = tuple(Product(**{**p, 'categories': tuple(p['categories'])})
                             for p in raw.pop('products'))
            record = Collection(products=products, **raw)
            if record.store_id != store or record.day != day:
                raise ValueError('历史快照索引与内容不一致')
    finally:
        connection.close()


def restore(code, destination, branch='monitor-data-mock', bootstrap=False):
    if destination.exists():
        raise ValueError('恢复目录必须不存在，避免覆盖本地文件')
    # Exact ref query: authentication/network failures must never imply an empty DB.
    refs = git(code, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}')
    if not refs and not bootstrap:
        raise ValueError('历史分支缺失；仅首次初始化可显式启用 bootstrap')
    destination.mkdir(parents=True)
    git(destination, 'init', '--quiet')
    # Reuse checkout's normal Git authentication config, including Actions extraheader.
    # Only copy the named public Git settings needed for remote access; never log values.
    remote = git(code, 'remote', 'get-url', 'origin')
    git(destination, 'remote', 'add', 'origin', remote)
    for name in ['http.https://github.com/.extraheader']:
        result = subprocess.run(['git', '-C', str(code), 'config', '--get-all', name],
                                capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError('Git authentication configuration unavailable')
        for value in result.stdout.splitlines():
            git(destination, 'config', '--add', name, value)
    git(destination, 'config', 'user.name', 'ozon-monitor[bot]')
    git(destination, 'config', 'user.email', 'ozon-monitor@users.noreply.github.com')
    if refs:
        git(destination, 'fetch', '--depth=1', 'origin', f'refs/heads/{branch}')
        git(destination, 'checkout', '-b', branch, 'FETCH_HEAD')
        validate(destination / 'snapshots.sqlite3')
    else:
        git(destination, 'checkout', '--orphan', branch)


def publish(destination, branch='monitor-data-mock'):
    validate(destination / 'snapshots.sqlite3')
    if git(destination, 'branch', '--show-current') != branch:
        raise ValueError('数据分支不匹配')
    git(destination, 'add', 'snapshots.sqlite3', 'reports')
    if git(destination, 'diff', '--cached', '--name-only'):
        git(destination, 'commit', '-m', 'Save mock monitoring history and reports')
    git(destination, 'push', 'origin', f'HEAD:refs/heads/{branch}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['restore', 'publish'])
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--code', type=Path, default=Path.cwd())
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    try:
        if args.operation == 'restore':
            restore(args.code, args.directory, bootstrap=args.bootstrap)
        else:
            publish(args.directory)
    except (ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        # Do not print commands/config/remote output that might contain credentials.
        parser.exit(1, f'历史状态操作失败（{type(error).__name__}），未自动重置历史。\n')


if __name__ == '__main__':
    main()
