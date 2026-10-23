# 第一批增强：主动成交、Delta/CVD 与现货/永续联动

开发分支：`codex/okx-orderflow-spot-perpetual`，基于已验收的 `feature/okx-usdt-perpetual`。不新增 Agent、订单或后台行情服务，execution_enabled=False。原未跟踪 AGENTS.md 保留，不属于本轮新增实现。

## 复用与集成

复用 OKXPublicClient 的公开 GET 白名单、进程内 0.25 秒请求间隔及有限重试；不使用 API Key、MCP、账户或订单接口。新增唯一 API 路径为 `/api/v5/market/history-trades`，其余复用 public/instruments 与 market/history-candles。

在原 preflight 完成核心校验后按需采集一次，写入独立 `orderflow_evidence`，不改变核心 market_snapshot 指纹、已验证K线或指标。所有原角色（包括 Market Analyst、多空研究员及风险团队）通过既有 get_instrument_context_from_state 读取同一份有界证据视图。没有新增角色或图节点；默认关闭时原提示词路径和研究模式保持不变。

final_gate 保持所有原闸门，在原研究 JSON 的 evidence 中增加可选 orderflow 视图。原报告系统增加 HTML/Markdown 摘要和 `orderflow_snapshot.json`，同时保存原始完整快照与按输出时刻计算的 effective_view。报告输出不再次采集成交，不偷换研究 cutoff。交易方向、候选价位、成本和状态机算法不读取新指标，不改变冻结规则。

## 官方免费接口检查

参考 [OKX 官方 V5 文档](https://app.okx.com/docs-v5/en/#order-book-trading-market-data-get-trades-history)：

- GET market/trades：最近最多500条，不能视为任意窗口完整历史。
- GET market/history-trades：文档历史范围约3个月、每页最多100条，20次/2秒/IP；先 type=2 按时间定位窗口末端，随后 type=1 按 tradeId 向过去翻页，避免同毫秒成交被时间游标跳过。
- side 是 taker 方向。SPOT sz 是基础币数量，SWAP sz 是合约张数。source=0/1 等来源原样留档，不用K线颜色或涨跌猜方向。
- WebSocket trades 是可聚合成交频道，trades-all 位于 business 路径；只核对文档，本轮不实现或启动 WebSocket，也不据此宣称无限免费历史。真实验证使用公开 REST。

官方接口可获取不意味着本轮支持全部历史。**第一批仅对当前研究启用短窗口**；历史研究直接 DATA_UNAVAILABLE，不发送当前逐笔/现货请求，也不假设当前合约参数曾适用于历史。完整历史CVD、长期累积、历史OI/盘口补齐均未实现。

## 窗口、计算与完整性

默认最近一个已收盘 UTC 分钟，`end=floor(cutoff/60000)*60000`，窗口 `[end-minutes*60000,end)`；现货和永续完全相同。可选1–60分钟，每市场最多1–100页，默认60页。预算不足明确 PARTIAL，不自动扩大请求或购买数据。400根1h/4h/1d仍只服务原趋势分析，不能将一分钟CVD冒充这些周期的CVD。

1. 现货身份必须由官方 SPOT 合约信息确认：live、同基础币、quote=USDT。永续继续使用原 validate_instrument；不调用现货 instrument() 的SWAP校验，也不将SWAP变成现货替代源。
2. 永续基础币数量=`sz*ctVal*ctMult`，现货基础币数量=`sz`。USDT 名义量=`基础币数量*成交价`。保留永续张数，不能与现货币数量直接比较。
3. 主动买入/卖出分别按实际 side 累加。Delta=买入量−卖出量；币数量和USDT口径分别计算。delta_imbalance=Delta/(买入+卖出)。
4. CVD从窗口起点0开始累计，提供10秒/30秒/60秒分桶；每个序列累计原点相同。空桶仅在完整分钟成交核验成立时代表该桶无成交，而不是用虚构成交补缺。无成交市场不计算价格、联动或方向。
5. 分页要求游标前进、ID与时间顺序合理，重复ID内容必须一致。未来成交、非法方向/数值、数量换算溢出、身份错误均阻断该市场证据。
6. 对窗口内每一分钟的确认K线核验基础币量及USDT量；分钟柱缺失、重复、未收盘或张数换算不符则失败。只有翻页确实越过起点且每分钟成交量匹配，才标VALID并输出正式Delta/CVD。覆盖率=观察到的基础币量/分钟柱基础币量；这不是对交易所每个撮合事件绝对完整性的证明，也不能靠1.0掩盖边界或分页问题。
7. 不完整市场保留原始样本、页数、覆盖及问题，但 statistics/cvd=null，禁止给出伪完整CVD；任一市场不完整时不生成联动结论。
8. 价格/CVD提供两种描述：窗口价格涨跌与Delta方向相反；两个半窗口价格极值及其同时CVD的比较。后者只是短窗口背离候选，不是确认的摆动背离或交易信号。
9. 永续/现货溢价使用相同完整窗口的VWAP：`(VWAP_swap/VWAP_spot-1)*10000` bps，不使用异步最后成交价假装同步报价；对比两边Delta符号、价格变化及成交量。相关性/符号相反不能证明因果或谁主导价格。
10. 现有OI和资金费率保留各自时间与预测/结算语义，可由原角色联合讨论，但不能把不同采样窗口假装同步的变化。模型解释与观测、计算指标明确分开。

## 质量、TTL与成本

快照记录核心snapshot_id、cutoff、窗口、组件获取时间、单位、原始逐笔、成交来源、分钟量核验、覆盖及SHA-256。再次输出先验指纹，不符时 RESEARCH_INVALID；历史/核心无效时不采集。默认TTL 300秒且配置最多300秒；从窗口末端计龄，过期后 effective_view=STALE_DATA，剥离Delta/CVD、联动和背离有效指标，原始证据留档。

可选证据缺失不改变原核心质量判定，也不能覆盖任何原交易预案校验。LLM不能把缺失数据解释成单方向证据。完整研究时间较长时，早期一分钟流量可能在报告生成前过期，这是正常降级，不延长TTL、不自动刷新或改写原研究基础。

原始逐笔不进入LLM上下文；提示只显示有界摘要及最后六个30/60秒桶，完整曲线在JSON。按研究共享一次快照，不启动采集守护进程，也没有跨研究的持久逐笔缓存。实际页数决定可覆盖窗口，不能保证BTC高成交时一分钟总能在默认预算内完整。

## 文件

新增：

- `tradingagents/dataflows/vendors/okx_orderflow.py`：采集、分钟对账、计算、有效视图与渲染。
- `tests/test_okx_orderflow.py`：模拟、原生图、公开接口验证。
- 本文。
- `.gitattributes`：固定原D.3数值策略文件既有的换行格式，见下节。

修改：cli/main.py、tradingagents/default_config.py、agents/state.py、agents/context.py、dataflows/okx_research.py、dataflows/vendors/okx.py、graph/trading_graph.py、reporting.py。

未改动原 perpetual_plan、plan_lifecycle、okx_backtest、okx_continuous 的逻辑；okx_multitimeframe 只恢复原冻结LF字节，没有数值变更。

## 换行与冻结保护

首次全量回归出现两个新增冻结检查失败。证据显示 Windows Git 检出将原LF的 okx_multitimeframe.py 改成CRLF。该文件内容与HEAD按文本完全相同，但冻结采用字节SHA-256，因此拒绝。

仅在校验 LF 结果等于原 docs/okx_d3_strategy_freeze.json 期望哈希后恢复其LF；同时用文件级 .gitattributes 保留另两份原本冻结为CRLF的规则文件。未更改冻结清单、哈希算法或数值策略。恢复后 strategy_fingerprint 仍为 `b61aa92741f23a9db0120211654ab56317490b22878622adeb77fe538eca79cf`。验收父分支未提交任何变更。

## Windows 使用方式

```powershell
cd D:\_01trading_agents
# 真实公开数据，不调用模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --orderflow --data-only

# 完整原多Agent研究，假设双边Taker费率每边5bps；不会交易
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --orderflow

# 请求更长窗口可能降级，不保证覆盖
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --orderflow --flow-minutes 5 --flow-max-pages 60 --data-only

.\.venv\Scripts\python.exe -m pytest tests/test_okx_orderflow.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_okx_orderflow.py -m integration -q
```

Python API使用原TradingAgentsGraph，config增加 okx_orderflow_enabled=True、okx_orderflow_minutes、okx_orderflow_max_pages、okx_orderflow_ttl_seconds。默认False；去掉--orderflow即可使用旧模式。--trade-plan会开启原多周期/衍生品，--orderflow本身不隐式更改策略或强制开启其他组件。

## 已完成测试与证据

- 初始专项22项通过，之后补充分页重叠/冲突、换算溢出与预算测试，最终专项29项通过；公开接口1项通过（显式允许真实覆盖不足降级，不把降级伪装通过完整CVD）。
- 本轮真实 data-only 验证确实得到VALID，不只是允许降级的测试：永续1444条、现货206条，币量与USDT量逐分钟均匹配，覆盖率均1.0。窗口永续Delta约−15.0694 BTC，现货约−1.0850814 BTC；窗口VWAP永续折价约4.9598bps。仅该历史分钟快照，不是当前交易方向。
- 全量最终回归1770 passed、5 failed、4 skipped、13 deselected，100 subtests passed，100.85秒。该次收集时含22项新增离线测试；随后补7项并重跑29项专项，计数有重叠不能直接相加。5个失败仍是已验收时复现的Windows权限/升级命令/用户home缓存测试；不声称全绿。
- 冻结连续回测相关与新增专项合计47 passed（当时22项专项），冻结检查恢复；原D.4.1实验与历史报告不覆盖。
- 真实数据-only报告：`C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_570f3c6fcb1d`。
- 完整BTC真实研究成功，退出码0，Model AVAILABLE / workflow COMPLETED，核心VALID，最终 WAIT / plan_valid=False / execution_enabled=False。研究 cutoff 为2026-10-23 08:24:48.418 UTC（北京时间16:24:48.418），原始成交窗口08:23–08:24 UTC（北京时间16:23–16:24）。永续1221条，主动买7.3363 BTC、卖15.2578 BTC、Delta/CVD −7.9215 BTC；现货152条，买0.40199634 BTC、卖0.32624724 BTC、Delta/CVD +0.07574910 BTC。两边原始覆盖率1.0，现货与永续Delta方向相反，不表示现货主导价格。
- 该研究结束时订单流 effective_view 和 research_result.evidence.orderflow 均为 STALE_DATA；原始VALID采集快照仍保留。实际报告中原角色分析了时效冲突，经理/Trader/风险团队明确不将过期CVD当当前方向证据。真实HTML/JSON已读取核对，无有效入场价、止损、止盈或RR，不补造。
- 完整真实报告：`C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_8da1872bee47`，含 complete_report.html、research_result.json、orderflow_snapshot.json。结构化多空 evidence IDs 继续使用原冻结ID集合；新增成交证据通过独立evidence.orderflow与各角色文字引用呈现，不修改冻结REFS或价格规则。

## 比较与第二批建议

旧规则没有更改，新增证据只是让原角色看到同一窗口的实际主动成交与现货对照。没有在相同冻结数据/模型/提示词下重复做A/B，因此不能将新旧不同时间报告的方向差异归因于CVD，也不能声称提高收益。

本批未实现长期连续CVD、1h/4h/1d完整逐笔历史、历史参数重建、WebSocket重连回补、自动盯盘或扫描。下一批若获授权，可优先研究完整小时成交量核验的缓存分段、明确窗口内的已确认价格/CVD摆动背离、时间对齐的OI变化，以及预算受控的固定快照对照。应先解决数据覆盖与可用时间，再评价策略贡献；不自动实施，不购买数据或改冻结策略。
