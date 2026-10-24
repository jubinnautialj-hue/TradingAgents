# V2.2：公开强平采样、多空拥挤度、跨所核验

基础版本：`dd37d8f7e8c5be957f4f488ce5a3b873860d7a24`。
开发分支：`codex/okx-liquidation-crowding-cross-exchange`。
本轮不提交、不推送、不新增 Agent、不接账户或交易接口。execution_enabled 始终 False。

## 数据来源与边界

先核实官方文档及公开连通性，再实现。没有付费数据，没有读取币安或 OKX API Key。

- [OKX 官方接口文档](https://www.okx.com/docs-v5/en/)：Rubik 合约账户比例、大户账户比例、大户持仓比例、历史 OI；沿用原 funding-rate-history 和已验证 K 线。
- [OKX 2023-04-03 变更记录](https://www.okx.com/docs-v5/log_en/)：历史强平 REST 已停止，不调用旧 liquidation-orders REST。
- OKX public WebSocket `wss://ws.okx.com:8443/ws/v5/public`，订阅 `liquidation-orders`、instType=SWAP，仅筛选请求的合约。官方明确它不代表 OKX 全部强平；字段 bkPx 是强平标记价格，并非普通市场成交价。
- [Binance 官方市场数据文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)：/fapi/v1/time、exchangeInfo、klines、fundingRate；/futures/data/openInterestHist、globalLongShortAccountRatio。
- [Binance 官方强平流说明](https://developers.binance.info/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Mini-Tickers-Stream)：单合约每 1000ms 最多最近一条快照，不是完整成交记录。使用 `wss://fstream.binance.com/market/ws/<symbol>@forceOrder`。
- Binance 大户 account/position 比例新文档标为 MARKET_DATA、需要 Key，本批不查询这些接口。OKX 对应免费公开接口可用，不将 OKX top 5% 样本与 Binance top 20% 样本混为一谈。无需用户提供密钥。

OKX 合约比例每页上限 100、可访问最近 1440 条；本批仅请求 3 个小时点，不承诺完整历史。OI 沿用已安装适配器与单位校验。Binance 本批也是少量最近点，不实现无限分页历史或绕过地域/权限限制。官网留存上限不等于本适配器承诺的历史覆盖。

## 实现与复用

新模块 `tradingagents/dataflows/vendors/okx_market_checks.py`：

- `prepare_liquidations`：当前 UTC 研究日才允许按需 WS 采样，默认 10 秒，范围 0–30 秒。两所并发、每所最多两次连接，连接超时受剩余预算限制；无后台服务，不补造断线期间事件。
- `_listen`、`liquidations`：明确订阅、严格筛选合约和方向、去重、记录接收时间/事件时间/连接段/重连/故障，公开样本一直为 PARTIAL。运行中的 notebook event loop 不被打断，显式降级。
- `BinancePublicClient`：只允许六个固定公开 GET，复用现有 requests；禁止私有路径、凭据参数、.netrc、重定向；0.25 秒节流、瞬时错误最多三次退避、Retry-After 最多 30 秒；401/403/418/451 不重试；每研究缓存相同请求。
- `collect_checks`：复用 OKXPublicClient、parse_oi_history、parse_funding_history、multiplier、已验证 1h 快照及已有资金历史。独立附加证据，不改核心快照。
- `evidence_view`、`render_checks`：快照哈希验证、300 秒证据 TTL、上下文去除逐笔强平事件，只保留摘要；原始事件保留在档案。

原 preflight 在核心截止时间冻结前采样强平，之后采集按 cutoff 约束的小时观测；当前接口的元数据不冒充历史参数。历史日期不连接实时 WS，collect_checks 对历史/无效核心不请求新实时源。未重建历史发布时点档案，因此本批不支持可靠历史拥挤度/强平研究。

`market_checks_evidence` 经原共享 context 到原九角色，原 final_gate 写入 `research_result.evidence.market_checks`；原 reporting 输出 Markdown/HTML 和 `market_checks_snapshot.json` 的 original/effective_view。默认开关 False；关闭时无新增采集或上下文，V2.1 行为保留。CLI、Python Graph、日志均记录配置。

## 口径和计算规则

1. OKX 合约 sz×ctVal×ctMult 转币量；样本名义金额=币量×bkPx，标记为 mark proxy。Binance 用 z（累计已成交数量）×ap（快照平均成交价），不使用原委托 q。仅去除完全相同事件；没有唯一订单 ID 时不能保证不同更新完全不重叠。因此不能把样本金额之和当交易所精确强平总额、总损失或跨所可加总市场份额。
2. sell 强平对应 LONG，buy 对应 SHORT；OKX posSide 若与 side 矛盾则拒绝。币安明确排除 CM 标识。非法价格/数量、NaN/Inf、换算溢出和未来时间拒绝。
3. 采集段表示传输在线区间，不是市场事件覆盖率。coverage_fraction=None、exchange_complete=False；空样本不是市场零强平。延迟事件保留其原始发生时间和实际接收时间，不推断其在采集段内发生。
4. 多空 account ratio 是账户数之比；top_positions 是 OKX 按持仓价值排名前 5% 用户的持仓价值比，不是账户比。OI 没有单边方向，不证明主动开多/开空。
5. 小时点必须 UTC 对齐、无重复、最近两个连续、最后点距 cutoff 不超两小时。OI 比较币量变化，排除随价格变化的 USD 分母。两所时间点不一致时不计算跨所 OI 对比。
6. 同一已收盘 UTC 小时比较 close、收益率、币量与 USDT 成交量。Binance K 线提供 taker-buy-base/quote，主动卖量=总量−主动买量，Delta=2×买量−总量。验证方向量不大于总量。OKX 的小时主动方向不从 K 线颜色推断，也不把 V2.1 一分钟 Delta 当成小时方向。
7. 价差 `(Binance close / OKX close - 1) × 10000`，这是相同窗口收盘对照，不是同步可成交套利报价。只有 OI 两个时间点刚好对应这根小时柱时，才生成 price/OI 符号背离观察，不解释成因果或策略触发。
8. 资金仅用实际已结算记录，OKX realizedRate 与 Binance fundingRate，不混入预测值。列出真实时间差，不预设八小时；时间差也可能意味着缺失记录，不承诺档案连续。仅在两所前后结算时刻均完全相同的条件下比较单期费率；毫秒不同也不强行对齐、年化或填补。
9. 原策略、证据 ID、候选价格、预案 Schema、状态机和历史账本不改。可选缺失不能覆盖核心质量闸门或授权新增风险。TTL 过期清除 effective view 的数据和比较，保留原档案用于审计，绝不悄悄更新研究依据。

## 模拟测试与回归

新增 31 项专项：方向、张数/币量、Binance 累计成交量、异常和未来时间、重复/缺失/对齐、跨所映射、资金周期、不匹配不对比、空样本、过期/篡改、WS 断线去重、订阅失败、HTTP 限流/重试/缓存/拒绝私有路径、CLI、原生九角色 Mock Graph、默认关闭、核心失败硬闸门、报告归档。

V2.2 + V2.1 专项合计 60 passed、1 integration deselected。Windows asyncio 建立本地 socketpair 时被测试网络隔离拦截的两项测试已修正：仅在创建 event loop 的作用域允许其 loopback 构造，实际 WS 仍 Mock，外部网络仍由全局夹具阻断。

全量回归：1808 passed、5 个原有 Windows 失败、4 skipped、13 deselected、100 subtests passed，90.11 秒。五项仍为两个 POSIX 私密文件权限断言、两个升级命令测试及 yfinance home 缓存测试。不宣称全绿。冻结 perpetual_plan、plan_lifecycle、okx_multitimeframe 原字节哈希全部匹配。

## 首次真实公开验证

报告 `okx_BTC-USDT-SWAP_multi_5a5e34ec4bf9`：核心 VALID，execution_enabled=False；新增证据 PARTIAL。

- OKX 三类比例、实际资金历史有效；Binance OI、账户比例、实际资金历史、同小时行情/主动成交量有效。
- 两路 WS 均成功建立约十秒连接，无 BTC 事件；只验证通路，不宣称真实非零事件的金额计算已经实测。
- 首次 OKX 小时 OI 报 OI contract/base units mismatch，保留 DATA_UNAVAILABLE；后续同 cutoff 独立公开查询可通过原解析器，首次故障未复现，不能断定根因或静默修正首次报告。原校验未放宽。
- 两所实际资金结算 timestamp 有毫秒差异，程序没有强行生成对齐费率差。

本地证据：`C:\Users\jerry_su\.tradingagents\logs\okx_v2_2\` 中的 special_tests.txt、regression.xml/txt、public_cli.txt、source_audit.json、real_research.txt。旧 V2.1 报告和 AGENTS.md 均保留。完整真实模型运行的最终结果另存验收 JSON，不能把公开通路成功等同于多 Agent 或策略准确率提高。

## 最终真实多 Agent 验收

唯一一次完整真实模型研究成功：`okx_BTC-USDT-SWAP_multi_f6f23ec85374`，Model AVAILABLE / workflow COMPLETED，退出码 0。cutoff 为 **2026-10-24 00:48:08.026 UTC / 北京时间 08:48:08.026**。已读取实际 HTML、Markdown、结构化 JSON、Market Analyst 原文和快照。

本次九个新增 REST 组件全部 VALID（包括 OKX OI），公开强平短采样两边均无 BTC 事件。原 Market Analyst 实际引用了 OKX 普通账户/大户账户/大户持仓的不同口径，并引用同一 23:00–00:00 UTC 小时的 OKX OI −1.2360%、Binance OI +0.6891% 分歧，没有据此宣称因果领导或市场全面加仓。

最终 **WAIT / plan_status=WAIT / plan_valid=False / allow_new_risk=False / execution_enabled=False**，无有效入场、止盈止损或 RR。本次新增原始证据 PARTIAL（强平公开覆盖不完整），最终 effective view 与 research_result.evidence.market_checks 都为 STALE_DATA。五分钟 TTL 没有延长。已有交易预案质量闸门照常执行。

真实初始接口和完整原生流程分别验证，不把首次 OI 失败抹去。非零强平事件仍只有 Mock 测试，不能声称真实金额统计已通过非零样本验证。单次研究不证明模型稳定性或准确率改善；本轮不再运行第二次。当前实时功能验证不作为冻结策略的未来样本外回测或调参数据。

最终结论：**有条件通过研究证据增强验收**，条件是保持默认关闭、明确公开采样不全/历史不可用/TTL 过期/既有五项 Windows 失败。未达到完整历史强平服务或盈利能力验证。源码、测试、实测报告指纹和限制保存在 `docs/okx_v2_2_acceptance.json`。

## Windows 命令

```powershell
cd D:\_01trading_agents
# 官方公开源，不调用模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --market-checks --data-only
# 原生九角色研究：只运行一次，不会交易
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --orderflow --market-checks
# REST 核验但不做 WS 采样
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --market-checks --liquidation-seconds 0 --data-only
.\.venv\Scripts\python.exe -m pytest tests/test_okx_market_checks.py tests/test_okx_orderflow.py -q
```

不传 --market-checks 即回到 V2.1。Python config 使用 okx_market_checks_enabled 和 okx_market_checks_liquidation_seconds。

## 文件清单及限制

新增：vendors/okx_market_checks.py、tests/test_okx_market_checks.py、本说明、验收 JSON。
修改：cli/main.py、agents/context.py、agents/state.py、dataflows/okx_research.py、vendors/okx.py（仅三条公开路径）、default_config.py、graph/trading_graph.py、reporting.py。

有条件验收范围是研究证据接入与安全降级，不是全量强平数据服务。未实现历史强平回补、持续采集、潜在强平热力图、跨所自动套利、币安大户 Key 接口、策略优化或交易执行。短采样极易无事件，模型结束时证据可能过期；历史发布可用性无档案，历史功能明确关闭。研究准确率/收益提高尚无对照证据。

不修改全局 MCP，不购买服务，不安装新框架。websockets 使用现有已安装依赖；缺少时降级，不自动安装。后续可研究带接收时间的长期公开采集档案，但本轮不进入 V2.3。
