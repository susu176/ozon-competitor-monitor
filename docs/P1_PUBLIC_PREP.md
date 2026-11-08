# P1 准备记录：公开访问未确认，真实采集器未实现

日期：2026-11-08（UTC）。范围是核对 P0，并在收到卖家 `5652967` 的公开地址后做一次店铺页读取。该次读取被 307 跳转拦住，因此没有字段实测，也没有生产解析器。

## 核实结论

P0 模拟版与仓库现状一致，真实 Collector 仍然不存在：

| 项目 | 状态 |
|---|---|
| 商品对比、首次发现、价格与类目汇总 | 已有，见 `ozon_monitor/core.py` 的 `analyze` |
| SQLite 每日快照、同日完整快照冲突拒绝覆盖 | 已有。本轮起失败和不完整结果不再占用快照行 |
| Markdown 与飞书文本 JSON | 已有，只写文件，不发送 |
| `FixtureCollector` | 已有，仍是唯一会返回商品的采集器 |
| Actions `01:00 UTC` 与 `monitor-data-mock` | 已有，并且仍然只跑模拟数据。本轮没有新增定时，也没有改工作流 |
| 真实店铺采集器 | 没有。`PublicShopCollector.collect()` 固定拒绝 |

`config/shops.json` 现有一家店：`store_id` 为 `5652967`，URL 为使用者提供的 `https://ozon.ru/s/mir-poleznyh-veshchey-5652967`。同一卖家的 `https://ozon.ru/s/5652967` 已记录但未请求。

## 真实请求

共三次读取。识别用的 User-Agent 为 `ozon-competitor-monitor/0.1 (one-shot public rules check; no browser impersonation)`。没有保存 Cookie 或令牌，没有浏览器伪装、代理或重试。

1. `2026-11-08T11:50:35.435110+00:00`，`GET https://www.ozon.ru/robots.txt`。当时使用的客户端会跟随临时跳转，最终停在 `https://www.ozon.ru/robots.txt?__rr=9`：HTTP 307，`Content-Type: text/html`，`Server: nginx`，`Location: https://www.ozon.ru/robots.txt?__rr=1`。正文 164 字节，是 nginx 的 “307 Temporary Redirect” 页面，SHA-256 `8f03491247cbfa8a2e60e0f7ec62d63b5070659f60383a1c81abeb2b20221be3`。这不是 robots 规则，不能用来判断店铺页、排序或分页是否允许。按停止规则，之后没有再请求 `www.ozon.ru`。
2. `2026-11-08T11:51:21.203708+00:00`，`GET https://docs.ozon.ru/legal/terms-of-use/site/`，不跟随跳转。TLS 握手超时，没有状态码、响应头或正文。没有重试，因此没有条款摘录，也不能判断条款是否允许自动化访问。
3. `2026-11-08T12:08:41.634514+00:00`，`GET https://ozon.ru/s/mir-poleznyh-veshchey-5652967`，不跟随跳转。HTTP 307，`Content-Type: text/html`，`Server: nginx`，`Cache-Control: no-store`，`Location: https://ozon.ru/s/mir-poleznyh-veshchey-5652967?__rr=1`。响应带有 `Set-Cookie`，值未保存。正文 164 字节，SHA-256 与第 1 次的 nginx 跳转页相同：`8f03491247cbfa8a2e60e0f7ec62d63b5070659f60383a1c81abeb2b20221be3`。没有商品 ID、标题、价格、图片或类目。因此未再请求 `https://ozon.ru/s/5652967`。

原始记录在 [evidence/2026-11-08-public-access.json](evidence/2026-11-08-public-access.json)。检索结果里还有第三方对 robots、条款和 Seller API 的转述；那些不是这次对官方地址的成功读取，没有写进解析器，也不当作许可。Seller API 需要卖家凭证，本次不收集凭证，也不假设它能读取竞店。

以下各项没有实测证据：

- 商品 ID、标题、价格、币种、优惠、图片、链接、三级类目是否出现在公开 HTML 中。本次店铺响应只有跳转页
- 最新排序的参数和分页形态
- 重复请求下的稳定性
- 价格是公开标价、卡价还是地区价

本地 HTTP 测试只证明探针在 127.0.0.1 上遇到 307 时不跟随、不保存 `Set-Cookie`，遇到 429 时停止，200 HTML 也不会解析成商品。那不是 Ozon 实测。

## 本轮代码

- `ozon_monitor/access.py`：单次读取、响应分类、只在真正拿到 robots 正文后才做路径判断。失败、验证码、限速、`__rr` 跳转都拒绝后续店铺请求。
- `ozon_monitor/probe.py`：手动探针。默认不访问网络；`--fetch` 才发出一次请求，并且阻断记录的 24 小时内即使带 `--fetch` 也不再请求。不解析商品。
- `ozon_monitor/config.py` 与 `config/shops.json`：空店铺列表合法。店铺 URL 必须是用户日后提供的 `https://www.ozon.ru` 或 `https://ozon.ru` 具体路径，拒绝凭据和浏览器 User-Agent。
- `ozon_monitor/prep.py`：分页覆盖、去重、币种口径、上海日期和就绪判断。`SUPPORTED_PARSERS` 为空，所以即使填入确认过的实测文件也不会开始解析。
- `ozon_monitor/daily.py`：只打印未就绪原因。不写数据库，不请求店铺，不发送飞书。
- `ozon_monitor/core.py`：非模拟来源要标记完整，必须带时区采集时间和 `已覆盖:` 分页说明；空目录还需要 `explicit-empty`。失败和不完整写入 `attempts`。报告在失败时写“无法判断”，避免把 `首次发现：0` 读成没有新品。

这些约束来自要接入的安全边界，不是线上页面已经证明的字段结构。

## 边界如何落地

- **分页与覆盖**：最新排序、读到声明的末页、页数一致且没有触达上限，覆盖说明才是 `已覆盖:`。缺任何一项都不能标记完整。Ozon 的实际翻页方式还没有看到。
- **完整性**：请求错误、跨页字段冲突、多种币种、未实测价格口径、没有空目录标记的空结果，都不能成为基线。
- **时区**：报告日由带时区的采集时间转换到 `Asia/Shanghai`。`2026-11-08 15:59 UTC` 仍是 11 月 8 日，`16:00 UTC` 进入 11 月 9 日。
- **去重**：同一商品且字段相同只保留一条；字段冲突则整次采集失败，不挑选其中一条。
- **价格口径**：币种、价格类型和地区都写入 scope。口径不同不比较涨跌；缺币种、缺地区或混合币种不能成为完整快照。
- **首次运行**：没有可比完整快照时只建立基线，首次发现为 0，并写明不计作当天上新。
- **失败与部分结果**：不进入 `snapshots`，`history()` 也不会把它们算作已见商品。失败报告写“无法判断”，不写 `首次发现：0`。不完整但有观测时写“至少 N”，并保留“观测下限”。
- **并发**：SQLite 使用 30 秒 busy timeout 和 `BEGIN IMMEDIATE`。数据分支仍拒绝非快进推送，这部分沿用 P0，本轮测试再次跑过。

直接调用 `analyze()` 并手工塞入一条没有 error 的不完整记录时，仍沿用 P0：该记录会计入已见集合。每日入口只用 `history()`，不会这样做。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m ozon_monitor.daily --config config/shops.json
python3 -m ozon_monitor.probe --evidence docs/evidence/2026-11-08-public-access.json robots
git diff --check
```

结果：30 项测试全部通过（原有 18 项，新增 12 项），约 1.95 秒。`git diff --check` 通过。每日检查退出码 0，打印未提供店铺 URL、两次公开读取未成功、没有解析器，并说明未写快照、未请求店铺、未发送飞书。探针退出码 2，因 11:50 UTC 的 307 记录拒绝在 24 小时内再次请求 `www.ozon.ru`。

## 尚未完成

1. 卖家 `5652967` 的名称地址被 307 `__rr` 跳转拦住。冷却结束后只能再做一次不跟随跳转的复核；若结果相同，继续停止，不保存 Cookie，不伪装浏览器。
2. `https://ozon.ru/s/5652967` 尚未请求。
3. robots 和条款仍没有可读正文。
4. 商品字段、最新排序、分页终点、价格口径和稳定性都还没有页面证据。
5. 只有出现可读且规则允许的公开 HTML 后，才实现对应解析器并加入 `SUPPORTED_PARSERS`。
6. 真实数据分支、生产定时、飞书发送都还没启用。模拟定时仍在现有工作流里，不会因此变成真实采集。
