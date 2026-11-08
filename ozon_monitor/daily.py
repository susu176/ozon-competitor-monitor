"""Daily readiness check. It does not fetch pages, write snapshots, or send messages."""
from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .prep import assess_readiness, load_evidence_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='检查每日公开采集是否可运行；不访问网络，不发送消息')
    parser.add_argument('--config', type=Path, default=Path('config/shops.json'))
    parser.add_argument('--evidence', type=Path, default=Path('docs/evidence/2026-11-08-public-access.json'))
    args = parser.parse_args(argv)
    config = load_config(args.config)
    records = load_evidence_records(args.evidence)
    readiness = assess_readiness(config, records)
    for reason in readiness.reasons:
        print(reason)
    print('未写入快照，未请求店铺页面，未发送飞书。')
    if readiness.ready:
        print('采集条件已满足，但当前进程仍不自动抓取。')
        return 0
    return 0 if not config.shops else 2


if __name__ == '__main__':
    raise SystemExit(main())
