# ozon-competitor-monitor

独立的 Ozon 俄罗斯站竞店监控项目。当前为 P0 **模拟数据基础版**，不含真实 Ozon 采集器，不请求收费 API、不调用模型、不发送飞书消息。真实店铺字段可获取性、合法性及稳定性尚待小样本验证，不能据此宣称数据源可用。

## 云端开发与验证

使用现有独立 checkout，不需要额外 worktree。Python 3.12，仅标准库，无需安装第三方依赖。

```bash
cd /workspace/ozon-competitor-monitor
python -m unittest discover -s tests -v
python -m ozon_monitor --fixture fixtures/day1.json --db /tmp/ozon-demo.sqlite3 --output /tmp/ozon-reports
python -m ozon_monitor --fixture fixtures/day2.json --db /tmp/ozon-demo.sqlite3 --output /tmp/ozon-reports
```

输出中文 Markdown 和飞书文本消息 JSON（仅写文件）。默认数据库与报告位于 `data/`、`reports/`。报告、快照均包含模拟标识，无真实店铺数据。不需要服务、Webhook、Cookie 或密钥。

## 数据与比较约定

- `Collector` 隔离来源。`FixtureCollector` 仍是唯一会返回商品的实现；`PublicShopCollector` 在字段实测和解析器完成前只会拒绝。一次采集明确标记店铺、日期、来源、完整性、价格/配送地区口径及错误。
- 商品 ID 必填；标题、价格、币种、优惠条件、图片 URL、商品 URL、三级类目均可为空。未知不补猜；价格使用十进制字符串。当前仅验证模拟字段流转，不证明线上字段存在。
- `published_at` 只有同时提供 `published_at_source` 才允许保存；真实来源仍须人工/采集器核实。首次发现表示本系统首次观测，绝不等同真实上架日期。
- SQLite 的 `snapshots` 只保存完整且无错误的采集。同日完全相同的完整快照重跑幂等，另一份完整内容冲突时拒绝覆盖。失败或不完整结果只追加到 `attempts`，不占用当日快照，也不进入后续比较。已有完整快照不能靠删库重试替换。
- 首次完整采集只建立基线，首次发现和类目提醒均为零；失败记录不触发商品变化，也不计入历史已见商品。首次发现只查询 `snapshots` 中该店以往的完整记录；价格比较使用同来源、同口径的最近一次完整快照，报告明确基线日期。币种或优惠条件变化不作同口径涨跌；不完整采集不比较价格。
- 首次基线不计作当天上新；商品消失不推断下架；类目统计只表示首次发现商品分布，不能证明集中上新。
- 日期由采集输入明确传入。未来真实采集器应以 `Asia/Shanghai` 生成报告日期，并记录 UTC 采集时间及分页覆盖证据；当前不伪造采集时间。

## 定时与持久化方案

GitHub Actions `01:00 UTC` 对应北京时间 09:00，可能因排队延迟；定时仅在默认分支生效。当前工作流每天按北京时间生成模拟输入，恢复历史后处理当日模拟快照，**不是每日真实采集**。push/PR 仅运行测试；schedule/手动触发才执行写入数据分支的模拟日报任务。报告 artifact 保留 14 天，仅用于验收，不作为永久数据源。

SQLite 与日报保存在独立 `monitor-data-mock` 分支，每次恢复到新目录后再生成当日报告；完整 SQLite 校验及记录解析成功才继续。恢复失败、分支丢失、空数据库、损坏及推送拒绝都会令任务失败，不会静默重置基线。正常运行不接受新建历史，只有第一次人工 workflow_dispatch 可开启 `bootstrap`；已有分支时此开关也不会跳过历史校验。不要在历史意外删除后使用 bootstrap，应从已确认的历史提交恢复。

写任务显式申请 `contents: write`，使用固定 concurrency 串行化且不取消正在运行任务；非快进冲突不强推。数据分支 push 不触发此工作流。GitHub 仍可能替换排队中的任务，不能保证每次定时都运行。`monitor-data-mock` 仅含模拟数据，与未来真实数据分支隔离。Git 分支历史是低成本持久化，不是独立灾备，误删时需从已知提交或离线备份恢复；长期二进制 SQLite 历史将增长，后续制定归档策略。

云端已用真实 GitHub Git 通道验证两天快照推送与第三个干净目录恢复。GitHub Actions API 目前返回 Forbidden，尚未验证实际 runner 的 GITHUB_TOKEN 写权限或真实定时触发。Git 推送权限不代表 runner 权限。合并到默认分支后仍需手动运行及观察下一次定时任务，核对历史行数、基线日期及模拟报告。详细证据见 [P0 验收报告](docs/P0_ACCEPTANCE.md)。

同日相同的完整快照重复执行幂等；同日另一份完整快照内容不同仍拒绝覆盖。失败后的完整成功可以写入，因为失败记录不在 `snapshots`。当前快照和飞书模拟 JSON 为文件生成，不是实际消息发送去重保证。

## P1 准备（尚未开始真实采集）

2026-11-08 只请求了 `https://www.ozon.ru/robots.txt` 和 `https://docs.ozon.ru/legal/terms-of-use/site/`。前者返回 nginx 307 跳转页，后者 TLS 握手超时。没有读到规则正文或条款，没有店铺 URL，因此没有店铺页字段、分页稳定性或生产解析器。证据与边界见 [P1 准备记录](docs/P1_PUBLIC_PREP.md)。

```bash
python -m ozon_monitor.daily --config config/shops.json
python -m ozon_monitor.probe --evidence docs/evidence/2026-11-08-public-access.json robots
```

第二条在阻断后的 24 小时内不会再请求；即使过了冷却，也必须额外加上 `--fetch` 才会发出一次请求。当前 `config/shops.json` 的 `shops` 为空。每日检查只打印未就绪原因，不写快照、不访问店铺、不发送飞书。GitHub Actions 仍只跑既有模拟任务，本阶段没有新增生产定时。

## 下一阶段验收与成本

1. 收到真实店铺 URL 后，先复核官方条款、robots 和公开页可访问性；在未被封禁、验证码或限速打断的前提下，少量请求验证 ID、标题、价格、图片、链接和三级类目。不绕过验证码或访问控制。2026-11-08 的规则读取未成功，不能省略这一步。
2. 免费公开页面与付费数据提供商分别评估覆盖率、区域/会员价格、限流、许可和更新延迟；目前未实测，不推荐购买，不编造付费报价。卖家接口是否支持竞店数据必须验证，不能假设可用。
3. 实现选定来源、分页完整性、限速、重试、失败隔离、区域价格口径及真实店铺配置。
4. 数据分支恢复/保存已实现并通过云端 Git 集成测试；补足实际 Actions 执行证据、失败重试版本与备份方案，再扩展至 10～30 店。
5. 安全配置飞书 Webhook 后实现分段、超时、业务错误检测及去重；在此之前仅模拟消息。Webhook 不写入 Git 或日志。

当前代码无第三方依赖，无 API/模型费用。公开仓库标准 Actions runner 通常免费；私有仓库消耗账户分钟与 artifact 配额，超额取决于 GitHub 当前套餐。真实采集量、执行分钟和付费 API 成本待数据源验证后测算；不能保证生产运行零成本。第一版实施顺序为：数据源 P0 → 真实采集 → 远端历史持久化 → 差异与日报实测 → 飞书联调。
