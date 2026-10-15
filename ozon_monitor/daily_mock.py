"""Generate today's explicitly mock input; no external data or messaging."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

raw = json.loads(Path('fixtures/day2.json').read_text(encoding='utf-8'))
raw['day'] = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
Path('/tmp/ozon-today-mock.json').write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
