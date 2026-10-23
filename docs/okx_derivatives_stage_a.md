# 阶段 A：OKX 永续合约数据增强

实施与验收日期：2026-10-22。仅公开 GET，无账户认证、交易接口、新 Agent 或执行模块。
当前分支 `feature/okx-usdt-perpetual`；原有未提交内容、个人配置及报告全部保留。未 commit/push。

## 复用与差距

| 原有能力 | 源码与入口 | 本阶段处理 |
|---|---|---|
| 公共行情请求、进程级节流、有限重试 | vendors/okx.py: OKXPublicClient.get | 复用；增加十个明确的公开 GET 路径 |
| SWAP 身份、UTC 已收盘 OHLCV、成交量校验 | validate_instrument / build_bundle / validate_candles | 保留；使用 ctVal×ctMult 做 OI、盘口换算 |
| 市场工具与固定快照 | agents/tools.py: _okx_snapshot / get_verified_market_snapshot | 直接复用注入的 market_snapshot，无新工具或二次行情请求 |
| 技术指标 | indicator_frame / render_indicator | 计算来源、单周期行为不变 |
| 多空、经理、交易员、风控工作流 | graph/setup.py: GraphSetup.setup_graph | 不新增 Agent、不改变评级及执行语义 |
| 前置、终止、报告质量检查 | okx_research.py: preflight / final_gate；reporting.py: checked_report_state | 保留确定性核心闸门 |
| 回测、历史决策、反思 | backtest.run_backtest；TradingMemoryLog；Reflector | 不重复开发；永续撮合、费用回测仍留阶段 D |

已有配置可控制单周期、K 线根数、预热和核心新鲜度。新增衍生品只需显式开启配置；
解析、历史覆盖、资金费用方向和盘口估算确需新代码，集中到 okx_derivatives.py。
阶段 B/C/D 未实施；尤其没有解决 SELL 动作语义、自动仓位或账户风险适用性。

## 运行

PowerShell，项目目录 D:\_01trading_agents：

```powershell
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --data-only --derivatives --liquidity-notional 10000
```

需要原有多 Agent 研究时移除 `--data-only`，保留 `--derivatives`。
不指定 `--derivatives` 时保持原行情研究行为；默认配置关闭增强数据。
`--liquidity-notional` 是盘口估算规模（USDT），不是订单或仓位。

Python API 使用原 TradingAgentsGraph，配置 `market_mode="okx_usdt_perpetual"`、
`okx_derivatives_enabled=True`、`okx_interval="4h"`，selected_analysts=["market"]。
其他设置：okx_derivatives_history_points=100（2..300）、okx_derivatives_ttl_seconds=300（1..900）、
okx_liquidity_notional_usdt=10000、okx_max_spread_bps=50。

报告保存在独立随机目录，含 Markdown、HTML、derivatives_snapshot.json。
该 JSON 是证据归档，不是阶段 C 的标准化交易预案，不能直接发给执行系统。

## 接口及核心函数

官方依据：https://www.okx.com/docs-v5/en/ （2026-10-22 查询官方文档并以公开 API 实测）。

| GET 路径 | 用途 | 解析入口 |
|---|---|---|
| /api/v5/public/open-interest | 指定合约当前 OI | parse_oi |
| /api/v5/rubik/stat/contracts/open-interest-history | 指定合约历史 OI | history_pages / parse_oi_history |
| /api/v5/public/funding-rate | 当前预测费率及下次时间 | parse_funding |
| /api/v5/public/funding-rate-history | 实际结算费率与结算时间 | history_pages / parse_funding_history |
| /api/v5/public/mark-price | 当前标记价格 | collect_derivatives 内 price_loader |
| /api/v5/market/index-tickers | BTC-USDT 指数价格（非现货替代） | 同上 |
| /api/v5/market/ticker | 原 SWAP 当前成交价 | 同上 |
| /api/v5/market/books | 100 档静态盘口 | parse_book / estimate_fill |
| /api/v5/market/history-mark-price-candles | 已收盘标记价格历史 | history_pages / parse_price_history |
| /api/v5/market/history-index-candles | 已收盘指数价格历史 | 同上 |

调用关系：build_bundle → collect_derivatives → 原 OKXPublicClient.get → 各解析器 →
固定 bundle + SHA256 指纹 → 原工具/Market Analyst → 原研究及风控流程 → 原最终闸门 → 报告。
evidence_view 在工具和报告读取时非破坏性地重新判断过期、截止时间；不重新请求。

## 单位、时间与计算

- OI：contracts=张、base_quantity=BTC、notional_usd=USD；绝不把 USD 当 USDT。
  base_quantity 必须匹配 contracts×ctVal×ctMult。OI 变化率使用连续同周期张数；零分母、缺口不计算。
- price_oi_evidence 仅将 OI 精确时间戳与已收盘 K 线的收盘时间相连，不插值、不作因果开仓归因。
- 预测 fundingRate 与 realizedRate 分开。历史费用只使用 realizedRate，不以预测值补缺。
  LONG 的事件现金流=-基础币数量×该次结算价格×费率；SHORT 相反。正值是收入。
  归档中每 1000 USDT 的费用只是事件名义金额示例，不是实际账户收益或回测。
- 当前 fundingTime 与 nextFundingTime 的差给出观察到的结算间隔；不假定固定八小时。
  历史记录保留每次实际结算时间，不补造事件。
- 当前价格均有独立时间戳。仅三种价格均有效且时间差≤5秒时输出价格偏差，明确非原子快照。
  标记价格只能提供强平相关市场证据，无法在缺少账户资料时计算账户强平价。
- 盘口数量为张，换算 BTC 与 USDT 深度。失衡为 (买方BTC深度-卖方BTC深度)/两者之和。
  按指定 USDT 规模换算统一基础币量，并按 lotSz、minSz 向上取整；展示取整后张数和名义金额。
  买单消耗 asks、卖单消耗 bids，计算 VWAP、相对最优价滑点、相对中间价成本；不足深度时不报告完整成交滑点。
  挂单不是主动成交量；估算未包含手续费、撤单、网络延迟、隐藏/RPI 流动性，不保证真实成交。
- 所有毫秒时间均为 UTC Unix epoch；1d 显式使用 1Dutc。历史价格只接受 confirm=1 且 close≤cutoff。
- 实时增强采集后以官方服务器时间冻结研究 cutoff；复用原 K 线再次检查。若已跨过新的 K 线收盘而原数据不足，核心闸门阻断，不偷换快照。
- 历史研究只调用历史端点，当前 OI、预测费率、盘口等标为 DATA_UNAVAILABLE，禁止实时冒充历史。

## 覆盖与质量边界

官方 OI 历史接口有限制（最新 1440 条）；本实现每次只取配置数量，不宣称完整档案。
资金费率官方历史范围最多三个月。标记/指数历史也只取限定窗口；本阶段不做长期档案下载。
每项保留来源、合约/指数、单位、采集时间、as-of UTC、范围、状态及 issues。
历史默认 100 点，分页至多四页；价格额外取一条，以便排除当前未收盘 K 线后仍保留所需根数。
返回空、异常值、重复/不前进分页、单位不符、非法盘口都按数据故障处理；不填补缺失值。

核心行情 VALID 与可选衍生品 VALID/PARTIAL/DATA_UNAVAILABLE/STALE_DATA 独立。
可选项缺失不会改成看多/看空，也不直接否定已经有效的纯技术研究；核心失效仍 allow_new_risk=False。
allow_new_risk=True 仅表示核心数据闸门通过，绝非完整衍生品或账户风险准入。
当前可选数据默认五分钟有效；长期模型研究中可变为 STALE_DATA，并从工具/报告视图移除其数值，
不自动刷新或更改研究截止时间。静态历史窗口仍可用，但必须结合 coverage 理解。
execution_enabled 始终 false。

## 实际验证

1. 先完成模拟验证后才访问公开市场。新增测试覆盖转换、正负资金费率、多种周期、分页、缺口、异常盘口、
   深度不足、过期、未来数据、快照篡改、可选故障、完整模拟 Agent 流程及 CLI/Python 一致闸门。
2. 核心及新增 OKX 模拟测试：69 passed。
3. 相关回归：263 passed（含原股票完整模拟工作流、结构化输出、报告、CLI、模型连接可靠性）。
4. 公开 API 集成：8 passed，38.05 秒。覆盖原 1h/4h/1d 与历史 K 线、三个周期衍生品、历史衍生品隔离。
5. 追加过期派生值复验：93 passed（OKX、报告相关测试），修复重新读取旧证据视图时可能保留过期价格比较的问题。
6. 新增接口最终真实 CLI 验证：核心 VALID、衍生品 VALID、allow_new_risk=True、execution_enabled=False。
   最终报告：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_4h_bee0f6f8709f\complete_report.html。
   cutoff=2026-10-22T11:10:26.577Z，北京时间19:10:26.577。
   当时 OI=3,025,342.46张≈30,253.4246 BTC，USD名义价值约24.7924亿；
   标记价81949.2、指数81988.5 USDT；盘口81949.0/81949.1，价差约0.01220bps；
   10000 USDT参考规模取整12.21张，两侧展示深度足够，相对最优价滑点0（不等于零交易成本）。
   预测费率约0.005909%，最近实际结算费率0.01%；当次观察到的后续间隔8小时，未硬编码。
   四类历史各100条，最后as-of为08:00Z；精确对齐价格/OI变化证据99组。
7. 未重跑付费真实多 Agent 模型研究；已验证真实公开证据和完整模拟 Agent 接入。不给模拟模型结果冒充真实研究。

本轮修改：cli/main.py、default_config.py、graph/trading_graph.py、agents/analysts/market_analyst.py、
reporting.py、dataflows/vendors/okx.py、新增dataflows/vendors/okx_derivatives.py、
新增tests/test_okx_derivatives.py、扩展tests/test_okx_public_integration.py、本文档。
不安装或升级依赖，不修改全局 MCP、网络设置或 API Key。

## 下一阶段建议（未执行）

阶段 B 继续使用原 Market Analyst：在一个服务器 cutoff 下构建分别验证的 1h/4h/1d bundle，
为每周期保留指纹、根数与预热后覆盖；只有全部必需周期通过才进入综合研究。
衍生品当前快照一次采集共享，历史按周期缓存；绝不把不同周期的指标混进同一列。
先确定并测试无未来数据的摆动高低点、突破与回踩规则，再交给分析师解释证据；
周期冲突由明确的大周期/结构/触发优先级处理，不使用简单投票。
阶段 C 再处理独立多空动作及交易预案；阶段 D 再处理资金费用、滑点和保守撮合回测。

回滚：不使用 --derivatives 或将 okx_derivatives_enabled=False，原行情流程即恢复。
无需删除旧供应商、报告、虚拟环境、密钥或个人配置。本轮到阶段 A 停止。
