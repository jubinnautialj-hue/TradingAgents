# A—D 完整功能清单与验证层级

口径：已实现不等于已完成真实全链路验证；MOCK 不等于发现真实交易机会；未接入不等于源码不存在。

| 阶段 / 功能 | 实现及入口 | 验证层级 / 当前边界 |
|---|---|---|
| 基础：合约身份 | okx.validate_symbol/validate_instrument；SWAP、USDT、linear、ctVal、ctMult、tickSz、lotSz、minSz、live | 源码+MOCK+真实公开数据；保留 BTC-USDT-SWAP，不转现货。当前参数不证明全部历史参数。 |
| 基础：OHLCV | OKXPublicClient.candles/validate_candles；1H/4H/1Dutc、分页 | MOCK+真实；confirm=1、收盘<=cutoff、去重/顺序/缺口/非法值/预热/新鲜度。 |
| 基础：单位与同源指标 | Volume 为基础币，另存 contracts、quote；indicator_frame | MOCK+真实手工 SMA 对照；SMA/EMA/RSI/MACD/ATR/VWMA 从同一周期快照，400 根为指标上下文而非全档案历史上限。 |
| 基础：闸门 | preflight/final_gate、reporting.checked_report_state | MOCK全图及真实数据；失败为无效研究，不伪造 HOLD。CLI 与 Python API/报告入口共享校验。 |
| A：当前 OI | okx_derivatives 当前 OI、单位换算 | 真实公开 GET；TTL 后降级，不作为历史 OI。 |
| A：历史 OI | rubik open-interest-history 适配、覆盖与时间检查 | 有限窗口真实接口验证；未重建 386 天完整历史 OI，不填补缺失。 |
| A：资金费率 | 当前预测与历史 realized 分开 | 真实公开 GET/官方档案；预测不得冒充实际费用。 |
| A：标记/指数/行情 | 当前与历史组件、行情核验快照 | 真实公开验证；精确资金结算瞬间标记价格仍不确定。 |
| A：盘口/流动性 | 当前 books、价差、深度、假设规模下滑点 | 真实公开验证；仅当前显示深度估算，不是真实成交保证、全历史盘口或强平数据。 |
| B：三周期统一数据 | okx_multitimeframe，统一 cutoff、独立检查 | MOCK+本轮实时/历史公开测试；不混周期，不使用未来收盘。 |
| B：市场结构 | 已确认枢轴、HH/HL/LH/LL、突破回踩、共振/冲突 | 明确规则+MOCK+真实保存输出；是研究描述，不是已验证盈利形态。 |
| B：原多 Agent 流程 | Market → Bull/Bear → Research Manager → Trader → 三风险 → PM | 真实完整研究记录+MOCK；没有新增 Agent，OKX 默认只用 market 分析师，不代表新闻/社交/基本面分析均已适配。 |
| C：动作语义 | PerpetualChoice/PerpetualResearchResult | OPEN_LONG/OPEN_SHORT_CANDIDATE、WAIT、REDUCE_LONG/SHORT、EXIT_CANDIDATE；未知持仓不假设持仓，旧 SELL 不自动开空。 |
| C：预案价位/成本 | candidates/validate_prices/finalize_result | 有效多空候选、持仓减仓、错误方向等主要 MOCK 验证；真实完整结果 WAIT，未有真实有效开仓候选全链验证。 |
| C：JSON/报告 | Pydantic Schema，Markdown/HTML/JSON | 本轮真实行情+模拟意见通过，历史两份 JSON 当前可解析；Schema 演进仍用 v1，需治理。 |
| C：实时重验 | revalidate_live/compare_revalidation | 真实公开重验与 MOCK重大变化/过期；保留原快照，不偷偷换研究依据。 |
| C.1：时间状态机 | plan_lifecycle.evaluate_signal/entry_evidence_ready | MOCK及历史模拟；PENDING_BREAKOUT→PENDING_RETEST→ENTRY_ELIGIBLE，支持 EXPIRED/INVALIDATED/WAIT。不是自动盯盘器。 |
| C.1：历史模板 | prepare_historical_signal/HistoricalAssumptions | MOCK+后续 D 使用；缺盘口可按显式保守假设模拟，策略明确依赖缺失数据时阻断。 |
| D.1：离线账本 | okx_backtest.simulate | 固定预案MOCK+真实历史；下一根开盘、不利滑点、双边手续费、实际资金时刻、保守同柱止损优先、持仓期限。 |
| D.2：信号漏斗 | okx_diagnostics | MOCK+真实历史；分清过滤、无候选、未突破、未回踩、过期、失效、持仓冲突，不为制造交易调参。 |
| D.3：长历史/连续性 | okx_continuous.fetch_history/continuous_simulation | 386 天真实公开输入；缓存分页、档案校验、跨批次连续性；恢复为确定性重放，不是逐柱持久执行器。 |
| D.3：冻结/盲测 | strategy_fingerprint/validation_protocol | 源码+测试+保存协议；未来区间未打开。无自动调参或扫描。 |
| D.3.1：费用审计 | okx_cost_audit.audit/reprice | 本轮原14笔复核通过；低/基准/高成本场景是敏感性，历史规格与精确结算价仍有假设。 |
| D.4：候选对照 | okx_agent_comparison.run_case | 原3例真实研究无成交；不能评价收益。无工具/Memory、固定候选与三态 Schema。 |
| D.4.1：成交条件样本 | okx_traded_comparison seal/lineage/continuous_replay | 14例中6真实完整ACCEPT、8失败；全部279候选层保留，连续B重放并非简单删除交易。 |
| D.4.2：服务恢复 | reliability/recovery/okx_llm_recovery | MOCK重试、限流、完整角色缓存/断点、quota停止通过；原8例未恢复成真实成功，旧服务细因仍未知。 |
| D.5：反馈存储 | OKXSimulationMemory、build_feedback/import_experiment | 实际封存14例导入；PIT/幂等/来源验证通过；与股票日志隔离，不把失败记成功。 |
| D.5：Reflection | 原 Reflector 新方法+settle_simulation_feedback | 固定 MOCK端到端与失败测试；现有2条MOCK示例，不注入正式上下文；真实 Reflection 内容质量尚未验证。 |
| D.5：后续上下文接口 | get_past_context(as_of=...) | 程序级过滤已实现；正式实时 Graph **尚未接入**，冻结D.4明确禁用。没有自动学习/策略更新。 |
| 股票兼容性 | 原 vendors、Graph、Backtest、Memory | 全默认离线回归未发现新增核心失败；5项已在HEAD复现；本轮没有真实Yahoo/股票LLM外部验证。 |
| MCP与账户 | 独立公开REST；不使用研究Agent调用live MCP | 未接入私有账户数据，不验证账户费率或权限，不依赖 API Key。 |
| WebSocket/扫描/执行 | 未实现本项目相关阶段入口 | 无自动盯盘、批量机会发现、实盘/模拟盘订单执行、账户风控、保证金与强平完整模型。 |

## 关键时钟与状态

- K线/指标：统一研究 cutoff、各周期独立 latest_close，1d 为 UTC 日线。
- 实时衍生品：默认 TTL 300 秒，过期降级；核心 bundle 默认 900 秒，新收盘柱也可能使其提前失效。
- C.1：默认六根小时柱的信号确认窗口，与交付 TTL 不同；入场重验默认 60 秒，不能把技术确认当作可执行状态。
- D.5：反馈可用=max(研究实际记录、结果可知、来源可用、写入时间)，反思再受生成时间限制。档案发布时间不明时按实际导入时间保守处理。
- data_quality=VALID 不等于 plan_valid，更不等于 execution_enabled。所有 OKX 阶段执行状态保持 false。

详细源码函数与行号索引见验收证据目录 `source_map.json`；完整运行记录、测试和限制见 [final_acceptance.md](D:/_01trading_agents/docs/acceptance_20261023/final_acceptance.md)。
