# 阶段 B：1h / 4h / 1d 综合研究

2026-10-22，TradingAgents 0.7.0，分支 feature/okx-usdt-perpetual。
只扩展 trading_research；没有新 Agent、交易预案、订单、回测撮合、账户查询或 MCP 修改。
原有未提交代码与研究报告保留，不 commit/push。

## 用法

在 D:\_01trading_agents 的 PowerShell 中：

```powershell
# 公开行情与确定性证据验证，不调用 LLM
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --data-only

# 原多 Agent 流程，附加多周期研究
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives
```

--multi-timeframe 固定启用三个必需周期；--interval 仍表示兼容锚点，不改变 1d→4h→1h 的研究优先级。
推荐锚点4h；未指定时原默认1h仍有效，任何工具都显式输出三个周期。
不加 --multi-timeframe 保留原单周期；不加 --derivatives 不请求可选衍生品。
历史研究可加 --date 2026-10-01，含义为该 UTC 日结束时刻。

Python API 复用 TradingAgentsGraph(["market"], config=...)：
market_mode="okx_usdt_perpetual"、okx_interval="4h"、okx_multi_timeframe=True。
可选 okx_derivatives_enabled=True。其余模型、输出、报告配置保持原用法。

## 复用与数据流

preflight → 原 build_bundle 的模式分派 → build_multitimeframe →
原 OKXPublicClient（一次时间/身份、可选衍生品采集）→ 冻结共同服务器 cutoff →
_PinnedClient → 三次原 build_bundle / check_bundle（分别1h/4h/1d）→
analyze_timeframe / confluence → 总快照及指纹 → 原三个市场工具 →
原 Market Analyst → Bull/Bear → Research Manager → Trader → 三位风控 → Portfolio Manager →
原 final_gate → 报告输出再次检查。

没有重建请求层、指标引擎、Agent、辩论或模型客户端；没有新增公开接口或依赖。
公共客户端保持阶段 A 的 GET 白名单、限流、有限退避及无凭据请求。

## 时间和周期隔离

- 先取官方服务器时间与身份。若启用阶段 A，衍生品只采集一次，随后取服务器时间冻结共同 cutoff。
- 历史 cutoff 仍为 min(指定UTC日结束,服务器时间)，当前值不冒充历史。
- _PinnedClient 给三个原校验器返回同一个服务器时间与合约身份；每周期 K 线只请求一份。
- 每个子快照独立检查 confirm、UTC对齐、价格关系、三种成交量、缺柱、预热、指标有限值与新鲜度。
- 1d 明确使用1Dutc。各周期 latest_close_ms 是各自边界，不强求日线与小时线在同一时刻收盘。
- 三个 cutoff 必须完全相等；指标分别 wrap 各自的数据，绝不合并跨周期行情。
- 父快照保留显式锚点 OHLCV 作为兼容视图，timeframes 下才是完整三个独立序列。
- 父指纹绑定全部子快照、确定性结构、规则版本与参数、综合描述及可选衍生品。
- 历史衍生品只走历史端点，当前OI、盘口等标为不可用。衍生品历史按锚点周期采集，
  不假装同时拥有三份不同周期OI历史；当前衍生品共享，价格/OI对齐的周期有明确标签。
- 同次研究的工具/报告只读取固定数据；不存在模型触发的重新请求或隐式刷新。

默认每周期400根、至少250根预热，完整保留原阈值。1h的400根约16天16小时，4h约66天16小时，
1d约400天；这些是取样窗口，绝不表示完整任意历史区间。指标序列前249根仍不展示。

## 确定性结构规则

版本 closed-pivots-v1；配置 okx_pivot_window=2、okx_structure_window=20、okx_retest_atr_fraction=0.1。

- 趋势 UP：收盘>SMA50>SMA200，且SMA50较5根前上升；DOWN反向；其他MIXED。
- EMA10、SMA50/200、RSI14、MACD(12,26,9)、ATR14、VWMA14独立计算，单位和周期明确。
- 摆动高低点严格高/低于左右各N根。只有右侧N根全部收盘后才确认，并记录 confirmed_at_ms。
  不把尚需未来K线确认的点放入当前证据。相等极值不强行标作摆动点。
- 最后两组已确认高/低点共同升高为HH_HL，共同降低为LH_LL，否则混合或不足。
- 趋势与摆动结构同向标记延续候选、反向标记反转候选，均不是预测或已验证交易信号。
- 支撑/阻力候选来自已经确认且位于当前价格下/上方的摆动点，列出最近参考价位。
- 突破：最新收盘超出此前N根最高/最低价；影线越界但收盘收回标作候选拒绝突破。
- 回踩/反抽：前根收盘突破再之前N根区间，最新低/高与固定突破水平相距不超过ATR×容差，
  且收盘保持在突破方向。保留固定参考水平、容差和确认时刻，避免事后移动门槛。
- 成交量比为最新BTC成交量/之前N根均量，ATR变化为最新ATR/5根前ATR；不计算不存在的主动资金流。

综合状态：全部UP或全部DOWN才为TREND_RESONANCE；同时存在UP和DOWN为TREND_CONFLICT；
含MIXED且不满足前两者为INSUFFICIENT_EVIDENCE。这是保守描述，不是投票，也不是WAIT交易动作。
角色优先级固定1d环境→4h结构→1h确认；所有原角色通过共享上下文接收各周期状态、时间、子指纹及冲突。
Market Analyst 必须分周期说明证据、再综合讨论可选衍生品，不能将1h观点冒充全局结论。

## 安全及报告

任一必需周期缺失、过期、篡改或与统一cutoff不符，父状态RESEARCH_INVALID、allow_new_risk=False、
最终评级REVIEW，不把故障转换成HOLD/BUY/SELL；模型的Buy无法覆盖最终闸门。
前置、工具、最终节点及报告写入继续共用原检查入口。
有效数据但趋势证据不足不等于数据故障：仍可保留核心VALID，综合状态单独显示。
阶段A可选数据缺失或过期不改变核心行情有效性，但必须显示状态和原因，不作方向依据。
当前数据若在研究过程中跨过新小时收盘或超过900秒，最终可能被拦截，应重新研究，不延长有效期掩盖问题。
execution_enabled始终false；旧SELL语义仍未变成可执行做空指令，阶段C再处理。

独立随机报告目录使用multi标识。新增multitimeframe_snapshot.json完整保存三个周期及总指纹，
是研究证据归档，不是交易预案。保留阶段A derivatives_snapshot.json和Markdown/HTML。

## 验收记录

- 模拟：28项新增多周期测试；联合原OKX测试97 passed。
- 相关回归：291 passed，覆盖多周期、阶段A、原单周期、股票完整模拟工作流、结构化输出、CLI、报告及模型可靠性。
- 补充股票/上下文、Yahoo快照、现有回测回归：51 passed。
- 新增公开API测试：2 passed（17.87秒），覆盖当前及2026-10-01历史研究。
  每周期400根，独立手算SMA200与程序一致，确认UTC日线及无未来收盘。
  当前样例 latest_close_ms：1h=1792666800000，4h=1792656000000，1d=1792627200000。
  当时趋势1d=UP、4h=MIXED、1h=MIXED，综合INSUFFICIENT_EVIDENCE，没有强凑共振。
- 完整模拟LLM工作流通过；必需周期故障时模型不启动，终端模拟Buy也无法绕过最终校验。
- 真实完整LLM研究成功，退出码0：核心VALID、模型AVAILABLE、工作流COMPLETED、execution_enabled=False。
  统一cutoff=2026-10-22T11:32:17.284Z（北京时间19:32:17.284），原报告生成于北京时间19:40:09。
  1h/4h/1d均400根且VALID，最新收盘分别为11:00Z/08:00Z/00:00Z（北京时间19:00/16:00/08:00）。
  收盘分别82004.2/82202.0/84049.9 USDT；三周期SMA200独立复算与报告一致，子指纹及总指纹均复核匹配。
  Market Analyst、两位研究员、研究经理、交易员、三位风控、组合经理均有独立非空报告，研究/风控各1轮。
  最终旧评级Underweight，仅研究降低既有多头风险，未实现独立开空动作。
  原报告完整保留：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_1c56bb358f34\complete_report.html。
  质量表排版及可选降级原因展示修正后，同一已保存模型结果另存为：
  C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_review_1dd91beb95fe\complete_report.html。
  此次另存未重新调用模型或获取行情；输出时核心仍VALID，未覆盖原报告。
- 可选衍生品最终PARTIAL：当前OI、预测费率、三种现价和盘口超过300秒，被标为STALE_DATA并从有效数值视图移除；
  四种历史数据仍VALID。报告质量栏明确保留原因，正文的市场数值仍应按原11:32Z时点理解，不能当成当前报价。
- 展示修正后针对多周期、衍生品和报告复验：81 passed。Ruff与git diff --check通过。

## 本轮文件

新增：tradingagents/dataflows/vendors/okx_multitimeframe.py、tests/test_okx_multitimeframe.py、
tests/test_okx_multitimeframe_public.py、本文档。
修改：tradingagents/dataflows/vendors/okx.py、tradingagents/agents/context.py、
tradingagents/agents/analysts/market_analyst.py、tradingagents/default_config.py、
tradingagents/graph/trading_graph.py、tradingagents/reporting.py、cli/main.py，
以及阶段A的okx_derivatives.py（只补充摘要中的issues列，不修改采集或计算逻辑）。

回滚配置：移除--multi-timeframe或设置okx_multi_timeframe=False，原单周期与阶段A不受影响。
不删除报告或未跟踪文件，不修改虚拟环境、全局MCP、模型网络或密钥。

## 限制与阶段C建议（未执行）

结构规则是明确可复算的研究描述，未声称经过收益验证；阈值、摆动窗口仍需阶段D回测。
没有订单簿历史重建、跨周期全档案下载、自动扫描、BTC/ETH外部参考品种或新增Agent。
当前衍生品五分钟有效期可能早于长模型研究结束；过期应明确显示，不能刷新后混入原cutoff。
模型仍可能给出与保守趋势过滤不同的评级，应保留其反证与原始证据，不能当作机械交易信号。
本次模型将资金预测组件的settled状态描述为口径歧义：该状态实际针对另一个settFundingRate字段的结算周期，
并不代表forecast_rate已经结算。模型最终仍按预测处理，没有用其代替实际费用；该解释精度需后续改进。
模型继续输出未校准的仓位百分比，这是原有Trader/经理行为，不是本阶段验证出的策略参数。

阶段C建议：复用当前角色增加独立OPEN_LONG_CANDIDATE/OPEN_SHORT_CANDIDATE/WAIT及减仓意图字段，
将候选价位绑定本次周期证据与确认时间；用程序检查价格顺序、费用后风险收益比、有效期和持仓语义。
缺少证据时PLAN_INCOMPLETE；无账户资料时不得假定已有多/空仓；不新增任何实盘执行能力。
