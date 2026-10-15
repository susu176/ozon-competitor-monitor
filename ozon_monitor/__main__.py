import argparse
import json
from pathlib import Path

from .core import FixtureCollector, SnapshotStore, analyze, render


def main():
    parser = argparse.ArgumentParser(description='仅支持显式模拟数据；不执行真实采集或推送')
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--db', type=Path, default=Path('data/snapshots.sqlite3'))
    parser.add_argument('--output', type=Path, default=Path('reports'))
    args = parser.parse_args()
    current = FixtureCollector(args.fixture).collect()
    store = SnapshotStore(args.db)
    try:
        report = render(current, analyze(current, store.history(current.store_id, current.day)))
        store.save(current)
    finally:
        store.close()
    args.output.mkdir(parents=True, exist_ok=True)
    # Store IDs are data, never filesystem paths.
    name = current.store_id.encode().hex() + '-' + current.day
    (args.output / f'{name}.md').write_text(report, encoding='utf-8')
    payload = {'msg_type': 'text', 'content': {'text': report}}
    (args.output / f'{name}.feishu-mock.json').write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(report)


if __name__ == '__main__':
    main()
