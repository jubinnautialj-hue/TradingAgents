# V2.3 价格行为、动态订单簿与波动率环境

分支：`codex/okx-price-action-orderbook-volatility`。
基线：`076717e611bb30762df6122fefff74c0a4742a9a`。
默认关闭。没有新增 Agent、执行接口、交易策略参数、回测规则或全局 MCP 配置。execution_enabled=False。

## 官方接口核实与复用

- [OKX 官方 WebSocket 订单簿文档](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-ws-order-book-channel)：公开 `books` 为 400 档初始快照和约 100ms 增量，使用 `wss://ws.okx.com:8443/ws/v5/public`；同连接订阅公开 `trades`。不使用需账户权限的高频档位。
- [OKX 官方 2026-06-23 checksum 变更](https://www.okx.com/docs-v5/log_en/)：books 的 checksum 固定为 0、已弃用；不能把 0 当作有效 CRC 证明。依靠 TLS 和 seqId/prevSeqId 校验。
- 已有 `okx_multitimeframe.analyze_timeframe/structure_rules` 提供已确认枢轴、原突破/回踩事件；没有改动该冻结文件。
- 已有 `okx.indicator_frame` 提供 ATR，原核心/多周期 validator 保证闭柱、独立指标和单位。
- 已有 `okx_derivatives.parse_book` 提供价格档、单位、交叉盘口、价差、深度及估算成交校验；本批不复制原 REST book 计算。
- 已有 V2.1 `parse_trade/digest/evidence_view` 用于实际主动方向、币量换算、指纹和可选同窗成交参考。WS 运行时沿用已安装 websockets；缺少时降级，不自动安装。

## 价格行为规则

新 `price_action` 仅处理有效、至少 250 根已收盘连续 K 线。使用原枢轴计算器的 confirmed_pivots，**只取确认时间不晚于最后一根 K 线开盘的枢轴**。最后一根内才确认的枢轴不能反过来证明同根突破。

- 最近两组已知高点与低点同向上移为 UP、同向下移为 DOWN，其余 MIXED 或不足。
- 前收盘未越过已知阻力、本根收盘越过：UP 结构内为向上 BOS，DOWN 结构内为向上 CHoCH；向下对称。混合结构只标 UNCLASSIFIED_STRUCTURE_BREAK。
- 本根 High 越过阻力但收盘未站上，或 Low 跌破支撑但收盘收回，标 PRICE_SWEEP_REJECTION_CLUE。只证明价格形态，不证明止损实际成交、大资金扫单或操纵。
- 前一根收盘越过当时已确认枢轴、本根收回其内，标 BREAKOUT_FAILED_NEXT_BAR。只检查相邻两根，不宣称覆盖任意长的突破生命周期。
- 每条事件记录价格、收盘确认时间和失效条件。BOS/CHoCH 最早下一可观察 K 线开盘即该确认时刻，不能以本根未完成收盘提前交易。
- 收回突破位、重新越过被扫取位置等作为明确失效条件文字；本批输出最新研究证据，不新增持续盯盘或持久信号生命周期。
- 各周期保留独立支撑阻力、枢轴、原事件和快照 ID；出现 UP 与 DOWN 共存标 opposing_structure_conflict，混合或不足另标 unresolved，不投票或平均关键价格。

成交量比率=最后一根币量/之前 20 根平均币量。V2.1 Delta/CVD 只有在起止时间**精确等于某个已收盘 K 线**时才可生成该周期同柱联合证据；否则只保存独立来源、窗口和状态，不将一分钟 Delta 冒充小时确认。小时/日结构与秒级盘口不构造因果合取。

## 波动率规则

- ATR 比率=当前 ATR / 前 20 根 ATR 中位数；>=1.25 为 EXPANDING，<=0.8 为 CONTRACTING，其余 NORMAL。
- ATR 百分比=100×ATR/收盘价。
- 路径效率=20 根间隔的净价格变化绝对值/逐根变化绝对值之和；>=0.6 为 TRENDING，<=0.25 为 RANGING，其余 MIXED。静止路径效率为 0；ATR 历史不合法或无法计算时降级。
- 真波幅 TR 为 High−Low、|High−前收盘|、|Low−前收盘| 的最大值；TR>=3×前 20 根 ATR 中位数标异常波动。
- 波动率状态或环境标签跨周期不同分别记录冲突。标签描述环境与不确定性，不自动放宽/收紧入场、止损、止盈、杠杆或确认窗口；没有新的策略收益证明。

## 动态订单簿连续性和资源

`prepare_depth` 在核心 cutoff 冻结前执行，默认 10 秒、可配置 0–30 秒；历史日期或 0 秒不请求当前盘口。运行中的 notebook event loop 明确降级，不破坏其循环。

`BookTape.apply`：

1. 只接受先 snapshot、prevSeqId=-1，再 update；每个 update.prevSeqId 必须等于上个 seqId。
2. 序列倒退/重置、缺口、不带快照的更新、重复非空 seq、时间倒退/异常、非法价格/数量、重复价位、错误 tick、交叉/锁定盘口均拒绝。
3. 正常空更新 prevSeqId=seqId=旧 seq 是心跳，不虚构新指标点。
4. size=0 删除价位，正 size 替换该档；Decimal 价格为键，每侧最多 400 档，沿用原 parse_book 校验重建结果。数据通过全部校验才提交本地状态。
5. 断线或校验失败使该段 summary=RESEARCH_INVALID，抑制时序指标；最多重连一次，从新快照建立新段。不跨段求差、不静默 REST 拼接；序列重置保守地要求新段。

每研究最多两次连接、400 条消息、1000 条去重公开成交，每帧 1MiB、每侧 400 档、每段最多保留 200 条大额档位变化。变化总观测数和列表截断状态独立记录；列表不能被当作全量订单事件。连接超时受剩余预算限制，关闭超时约一秒；达到预算显式报告，不启动后台服务或长期缓存。

连续段指标：

- top20 买卖币量、价差 bps、mid、失衡 `(bid_base−ask_base)/(bid_base+ask_base)` 及其首尾变化。
- top20 总深度下降至少 25% 且价差超过初始 1.5 倍，标流动性恶化线索。这是变化价位范围内的可见深度，不是固定价格带总流动性。
- 大额阈值固定为本段初始 top20 各档名义金额中位数的 5 倍与 10000 USDT 的较大者。同价格档 size 变化仅称 DISPLAYED_SIZE_INCREASE/DECREASE，可能来自执行、撤单、替换或深度边界，不认定撤单主体、操纵、冰山单或意图。
- 只选择书本首末交易所时间之间实际收到的公开 trades。|买−卖|/(买+卖)>=0.6 且 mid 变化绝对值<=2bps，标 possible_absorption_clue。零成交不产生该判断。该成交流未逐分钟对账，跨频道到达顺序也非交易引擎因果顺序，因此只为潜在线索，不证明吸收。
- VALID 表示所记录公开流在该段内连续有效，不等于所有交易引擎状态、所有成交或全部市场深度都被观察。coverage_fraction=None。

## 原生流程与安全

`--price-depth` → preflight 前短时采样 → 原核心 cutoff/质量校验 → `collect_price_depth` → state.price_depth_evidence → 原共享 instrument context → Market Analyst、Bull/Bear、Research Manager、Trader、三位 Risk Analyst、Portfolio Manager → 原 final_gate → HTML/Markdown/JSON。

新增证据放在独立 `evidence.price_depth`，原交易预案 Schema/证据 ID/候选数值不动。原始档案在 `price_depth_snapshot.json`；模型上下文只接收摘要，不发送盘口重建和成交原始列表。

证据哈希不可由模型覆盖。TTL 固定 300 秒；过期清除 effective view 中的周期指标、盘口、同柱订单流及其派生冲突，不把过期数据当当前入场依据。原始闭柱研究保留在档案，历史不拿当前盘口替代。所有确认、成交、接收时间均不得超过核心 cutoff。

默认 `okx_price_depth_enabled=False`，关闭时不采集、不追加上下文；保留 V2.1/V2.2 独立开关。源码内无订单、账户查询或私有接口，execution_enabled=False。未改变冻结的 A—D 策略、状态机、历史账本、Memory/Reflection；保留盲测协议，当前功能通路验证不冒充策略样本外收益。

## 测试及初次公开验证

V2.3 专项 34 项通过；与 V2.1/V2.2 合计 94 passed、1 integration deselected（计数重叠，不重复相加）。覆盖 BOS/CHoCH 双方向、假突破、枢轴可知时间、波动率、路径效率、缺柱/未来/预热不足、快照/增量/删除/心跳/序列错误、tick/数值异常、可见大额变化、接收 cutoff、过期清理、报告、历史禁替代、断线恢复、原生 Mock Graph、CLI 默认开关及同柱成交窗口。

初次公开报告 `okx_BTC-USDT-SWAP_multi_4934d2f0926c`：核心 VALID，三周期证据 VALID，动态盘口 VALID；约 9.4 秒交易所时间内 95 个连续盘口点、46 条同窗公开成交，143 条消息、无重连。

- 1h 既知枢轴 UP、环境 MIXED、波动率 NORMAL。
- 4h 既知枢轴 DOWN、环境 RANGING、波动率 NORMAL，出现价格扫取拒绝形态。
- 1d 结构 MIXED、环境 RANGING、波动率 NORMAL，同样仅为价格形态线索。
- 观察卖量 2.275 BTC、买量 0.0869 BTC，样本 Delta −2.1881 BTC，mid 变化约 −0.1486bps；潜在吸收线索 True，不代表确认吸收。
- top20 深度下降约 41.774%，价差未显著放大，因此复合流动性恶化线索 False。不能仅凭挂单减少就宣称撤流或操纵。

初次公开运行后仅补充了大额变化列表截断/成交预算提示，未改计算或策略；最终专项复验通过。完整真实模型运行使用补充后的代码，最终结果及完整回归记录另存验收 JSON。

## 最终真实研究和验收

唯一一次真实完整研究：`okx_BTC-USDT-SWAP_multi_c37443bfea21`。Model AVAILABLE / workflow COMPLETED，退出码 0。研究 cutoff 为 **2026-10-24 01:23:01.275 UTC / 北京时间 09:23:01.275**。

V2.1、V2.2、V2.3 同时开启，实际生成了九个原角色的报告文件，没有新增角色。原 Market Analyst 明确引用附加波动率模块对日线、4h、1h 的环境分类，交易员及风险报告亦讨论盘口/波动率限制。Mock Graph 和共享上下文调用链验证九角色都能读取新增数据；不声称每个角色都显式引用所有新数值或全部 BOS 标签。

新增原始证据 VALID：三个周期独立有效；书本单一连续段有 **94 个盘口点、80 条同窗公开成交、无连续性错误**。同窗买量 1.0784 BTC、卖量 0.5432 BTC、Delta +0.5352 BTC；mid +1.0892bps，top20 深度 +48.5088%，潜在吸收线索 False、复合流动性恶化线索 False。检测到 1824 次满足阈值的可见档位数量变化，只保留前 200 条并标记 truncated=True，不视为 1824 个独立挂单或撤单。

最终 **WAIT / plan_status=WAIT / plan_valid=False / allow_new_risk=False / execution_enabled=False**。原始 V2.3 证据 VALID，最终 effective_view 和 research_result.evidence.price_depth 都为 STALE_DATA，清除派生指标，没有延长 TTL 或伪造当前价格。没有有效入场、止损止盈或 RR；单次研究没有证明准确率或收益提高。

实际 HTML 复核发现详情区原先未直接显示 V2.3 有效期，因此仅补充 report_html.py 的可选展示项，并加 HTML 断言。没有重复调用 LLM，也没有覆盖任何旧报告：从同一 cutoff 的真实完整 state 重新渲染，另存 **complete_report_v23_evidence.html**，展示 STALE_DATA、原 cutoff、快照 ID 及 TTL。原 complete_report.html、Markdown、JSON 均保留；该新文件是同一研究的展示补全，不是新模型研究或新数据。

完整实测与测试摘要、源码指纹、原版及展示补全报告指纹保存为 docs/okx_v2_3_acceptance.json。全量回归沿用的五个 Windows 失败包括两个 POSIX 私密文件权限断言、两个升级命令测试和一个 yfinance home 缓存隔离测试；四项跳过为 POSIX 相关与可选 langchain_aws 缺失。不能描述为全绿。冻结策略指纹及原 146 个实验文件均未改变。

包含 HTML 展示补全的最终全量回归：**1842 passed、5 failed、4 skipped、13 deselected、100 subtests passed，131.33 秒**。日志为 regression_release.txt/xml；之前的复验日志保留，不混加计数。

结论：**有条件通过研究增强验收**。条件是默认关闭、保持已知 Windows 失败披露、明确短时可见深度/成交样本与长研究 TTL 限制，不把形态、失衡或挂单变化当作机构意图证明。本轮不启用交易、不扩展 V2.4。

本地记录：`C:\Users\jerry_su\.tradingagents\logs\okx_v2_3\`。不把缓存、运行日志和原始盘口加入 Git。

## Windows 使用

```powershell
cd D:\_01trading_agents
# 公开数据，无模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --price-depth --data-only
# 一次原生九角色研究，三个增强批次组合；仅假设 Taker 费率每边 5bps
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --orderflow --market-checks --price-depth
# 只计算已有 K 线价格行为/波动率，禁用短时 WS
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --price-depth --depth-seconds 0 --data-only
.\.venv\Scripts\python.exe -m pytest tests/test_okx_price_depth.py tests/test_okx_market_checks.py tests/test_okx_orderflow.py -q
```

Python API 使用原 Graph/config：okx_price_depth_enabled、okx_price_depth_seconds。不给 --price-depth 恢复 V2.2。

## 文件清单与遗留限制

新增：vendors/okx_price_depth.py、tests/test_okx_price_depth.py、本说明、验收 JSON。
修改：cli/main.py、agents/context.py、agents/state.py、dataflows/okx_research.py、default_config.py、graph/trading_graph.py、reporting.py、report_html.py。无新增 REST 路径；盘口使用现有公共 WebSocket。

短时样本不代表长期环境，无法识别订单所有者或挂单真实意图。当前指标阈值是可解释研究定义，不是经过收益验证的策略参数。历史盘口不可用；BOS/CHoCH 定义依赖明确枢轴规则，不是唯一市场定义；事件只覆盖最新柱/相邻柱，不是全历史状态机。所有秒级和小时级指标窗口须分别解释。长模型工作流结束时新证据可能过期，不能靠延长 TTL 掩盖。

实测档案保留校验后的盘口指标点、有限档位变化和解析成交，不保留每帧完整 400 档原始 WS 报文。因此不能仅凭该档案离线独立重建全部实测盘口；序列和指标逻辑由运行时校验及模拟重放测试验证。这是后续独立复算审计的限制，不宣称已经建立完整原始行情录像服务。

V2.4 如获授权，可先准备独立带接收时间的公开数据档案、固定快照下的解释一致性对照、受控历史结构标签评价，再评估预测贡献。不自动开发、不自动调参、不启用交易。用户 AGENTS.md 保留；不自动 commit/push，不修改全局 MCP。
