# 阶段 C：独立多空研究与结构化条件预案

2026-10-22，TradingAgents 0.7.0，feature/okx-usdt-perpetual。
仅 trading_research；不增加执行、盯盘、账户风控、Agent或阶段D撮合/回测。
保留原未提交代码、报告和配置，无commit/push、依赖安装、MCP修改或账户接口调用。

## 运行

在 D:\_01trading_agents 的 PowerShell：

```powershell
# 只验证公开数据与Schema，不调用模型；不会伪造完整研究
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --data-only

# 真实研究：自动启用三个周期及阶段A衍生品
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan

# 明确给定成本情景的研究示例：5bps是测试假设，不是OKX账户实际费率
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5
```

不提供费率时不假设免费；候选开仓无法形成完整成本预案，返回PLAN_INCOMPLETE。
--maker-fee-bps可记录maker情景，但本版本两腿保守按显式taker费率计费，不计maker返佣收益。
--liquidity-notional 10000控制参考规模，绝非订单数量或账户仓位建议。
--portfolio 指定调用者自己的PortfolioContext JSON；不访问OKX账户。quantity只用于辨认多/空符号，
不把通用quantity单位擅自转换成真实合约持仓。省略文件为UNKNOWN；显式空positions为FLAT。

Python API复用TradingAgentsGraph(["market"],config=...)和propagate(...,portfolio=PortfolioContext(...))。
配置需显式开启market_mode="okx_usdt_perpetual"、okx_multi_timeframe=True、
okx_derivatives_enabled=True、okx_trade_plan_enabled=True；不兼容模式在模型启动前拒绝。
API第二返回值仍为REVIEW（不映射为股票评级）；新动作从state["research_result"]读取。
不加--trade-plan时，原股票及阶段A/B行为不变。

## 动作与状态

| 研究动作 | 含义与条件 |
|---|---|
| OPEN_LONG_CANDIDATE | 选择程序提供的做多条件模板；非买单 |
| OPEN_SHORT_CANDIDATE | 独立做空研究候选；绝不从SELL或Underweight转换 |
| WAIT | 独立的观望结论；不表示持有已有仓位 |
| REDUCE_LONG | 仅在调用者明确提供LONG持仓背景时保留 |
| REDUCE_SHORT | 仅在明确SHORT背景时保留 |
| EXIT_CANDIDATE | 仅在明确LONG/SHORT背景时保留，不生成退出订单 |

UNKNOWN、FLAT、LONG、SHORT严格区分；同标的多个仓位/对冲状态不擅自净额化，降为UNKNOWN。
已有仓位的减仓/退出为POSITION_REVIEW，仍需账户资料和人工决策，不生成开仓价格预案。

data_quality只描述核心行情。plan_status独立使用VALID_CANDIDATE、WAIT、PLAN_INCOMPLETE、
RESEARCH_INVALID、EXPIRED、RESEARCH_CHANGED、POSITION_REVIEW、LLM_UNAVAILABLE。

plan_valid=True仅表示数值、来源、成本、方向及当前证据满足条件研究模板，不表示触发已发生。
所有开仓候选都requires_revalidation=True：未来收盘及回踩确认后必须重新验证，原预案过期即失效。
本系统不具有识别未来触发后直接放行的执行流程，因此阶段C交付的allow_new_risk和execution_enabled
始终False。核心market_snapshot.allow_new_risk仍保留原数据闸门含义，不应与交付层混淆。
WAIT没有可入场预案，plan_valid=False；行情有效时这不是数据失败，CLI正常退出。
数据故障仍非方向信号，不能用WAIT掩盖：plan_status与data_quality同时明确标为故障。

## 原能力复用与调用关系

- 原Market Analyst、Bull/Bear、三位风险分析师及多周期证据不重建。
- 原create_research_manager / create_trader / create_portfolio_manager仅在阶段C分支使用PerpetualChoice；
  三者都必须给出long_case、short_case、wait_case，不再使用股票评级选择永续动作。
- 原get_instrument_context_from_state向所有角色补充UNKNOWN持仓与独立多空语义。
- 原preflight建立research_id及程序候选；原Data Final Gate调用final_gate(...,revalidate=True)。
- record_decision、save_reports、Markdown/HTML/JSON输出继续执行最终闸门；只有图中的最终节点可发起一次公开复核。
- 原脚本、Provider、模型网络、研究员轮次、股票BUY/SELL/HOLD分支均保留。

程序路径：build_bundle/check_bundle → candidates → 原角色选择candidate_id →
finalize_result → validate_prices/calculate_costs → 必要的revalidate_live/compare_revalidation →
PerpetualResearchResult → 原报告树。

## Schema与数值来源

agents/perpetual_schemas.py为单一Schema来源。角色只输出PerpetualChoice：六种动作、固定模板ID、
独立多/空案例、观望理由及证据ID。Schema禁止额外字段，模型无权填写价格、数量、费率、plan_valid或执行权限。
结构化输出失败时不从自由文本SELL提取方向，不把格式失败伪装为有效研究。

每次报告输出research_result.json与research_result.schema.json（Pydantic生成JSON Schema），同时保留
complete_report.md/html、multitimeframe_snapshot.json及derivatives_snapshot.json。
JSON包含research_id、exchange、instrument、research_cutoff_utc、snapshot_id、market_regime、
timeframe_analysis、long_case、short_case、wait_case、proposed_action、model_proposed_action、
entry_conditions、entry_reference、stop_loss、take_profit、invalidation_conditions、estimated_costs、
risk_reward、evidence、missing_data、blocking_reasons、data_quality、plan_status、plan_valid、
requires_revalidation、expires_at_utc、research_expires_at_utc、allow_new_risk、execution_enabled等。

PriceEvidence带来源子指纹、计算规则和字段引用。missing_data保留可选历史/实时证据缺失诊断，
blocking_reasons单独决定是否可形成完整候选；可选历史不足不冒充核心行情故障。
这些是交付研究证据，不含交易所订单类型、账户授权、杠杆或保证金设置。
模型自由文本是观点，不作为程序数值来源或已验证事实；关键数字只由程序生成。

## 当前支持的条件研究模板

版本v1仅提供LONG_BREAKOUT_RETEST和SHORT_BREAKOUT_RETEST，不宣称覆盖所有策略。

- 入场参考：4h此前20根已收盘区间的高/低边界加/减一tick。
- 止损参考：最近有效4h摆动支撑/阻力，外加0.25倍1h ATR缓冲。
- 止盈参考：入场加/减3倍4h ATR的显式扩展计算，不将其声称为已验证阻力/支撑。
- 入场需后续1h收盘越界及后续收盘回踩确认；不把影线触及或当前报价当作触发。
- 开仓必须通过方向一致的多周期TREND_RESONANCE；冲突或混合证据不能被模型投票覆盖。
- 缺少有效摆动价位时不虚构；返回WAIT/PLAN_INCOMPLETE。
- 上述ATR参数可配置，默认只是研究规则，未经收益回测验证。

validate_prices硬性检查LONG:stop<entry<target，SHORT:target<entry<stop，以及有限正价格与tickSz。
入场朝突破方向取整，止损远离入场取整，目标朝入场取整；参考张数按lotSz向下取整且不少于minSz。
参考基础币量=参考张数×ctVal×ctMult，严格区分contracts、BTC、USDT。

## 成本与风险收益比

手续费=基础币数量×(入场价+退出价)×显式taker_bps/10000，盈利/止损退出分别计算。
用阶段A完整盘口对同一参考基础币量模拟买入/卖出，含半价差及展示深度的中间价偏离，
不足深度或异常价差时阻断。未来退出成本仅是当前盘口比例的情景代理，绝不保证实际成交。

持有窗口默认4小时，可配置okx_plan_horizon_hours。只使用当前已知upcoming fundingTime的预测费率；
若窗口超出下一事件且下一事件费率未知，不复制当前费率或假设固定八小时，直接判预案不完整。
LONG预测现金流=-基础币量×入场名义价格×预测费率；SHORT反向。正值为收入。
净RR不计入预测收入，只扣除潜在支出；实际历史费用仍来自realizedRate，绝不以forecast替代。
已补充settState的字段口径说明：它描述settFundingRate对应的结算周期，不代表forecast_rate已结算。

毛RR=毛盈利/毛亏损。净盈利=毛盈利-盈利退出手续费-滑点-保守资金支出；
净亏损=毛亏损+止损退出手续费+滑点+同口径资金支出；净RR=净盈利/净亏损。
默认最小净RR1.5，可配置；不达标不得保留有效候选。
结果Schema除字段验证外，还交叉复核数量/合约面值、tick/lot、手续费及毛净RR的算术一致性。

## 实时过期及重大变化

原多周期、原OI/盘口、cutoff、指纹始终保留。工具/报告读视图继续使用阶段A的300秒TTL，未延长。
研究有效期保留核心900秒并受新的K线收盘边界约束；预案期限另受原cutoff+okx_plan_ttl_seconds
（最多900秒）及关键实时证据期限的更小值约束。新的条件确认若晚于有效期，必须新研究，不能延续旧授权。

若最终模型提出本来具备方向与价位条件的开仓候选，最终节点可用原公开客户端重新获取合约规格、
OI、预测费率、标记/指数/成交价、100档盘口及服务器时间。原历史K线不会被替换。
复核对象单独保存于entry_revalidation，绑定原snapshot_id、合约、时间和独立validation_id；报告输出不重复请求。
若没有可用开仓候选（例如WAIT或周期冲突），不为制造有效预案而无意义刷新。

重大变化阈值（v1研究规则，非统计保证）：成交/标记/指数任一价偏移>原1h ATR的0.5倍、OI变化>5%、
下一资金结算事件改变或费率偏移>1bp、盘口失衡变化>0.3或价差异常。
变更则RESEARCH_CHANGED并WAIT，要求重新研究；复核失败/过期也不会放行。
即使价格变化较小，仍用新的公开证据再次计算成本，并保留新旧两份数据。
公开请求耗时也计入时间：网络复核结束后再次校验核心数据及预案期限，防止慢请求把旧研究变成有效预案。

## 测试记录

- 新增26项模拟测试通过：有效多空、WAIT、减多/减空/退出、未知/空仓/对冲背景、错误止损/tick/lot、
  毛净RR、手续费和资金费用方向、缺价位、缺费率、参考规模不足、旧SELL不可转换、过期、重大变化、
  复核不替换原快照、不重复请求、慢复核越过研究有效期、Schema算术篡改及完整原Agent流程多空候选。
- 最终相关回归368 passed，23.08秒（股票、阶段A/B、CLI、报告、模型可靠性、Yahoo快照和原回测）；
  中间慢复核保护及报告复验47 passed，最终报告展示复验50 passed；这些范围有重叠，不能相加。
- 最终实际公开API复核1 passed，16.56秒。原cutoff=2026-10-22T12:28:38.783Z，
  单独复核完成于12:28:48.057Z；原快照未修改，Schema交付正常。
  此测试使用明确的模拟WAIT意见与真实行情，不能当成真实LLM研究。
- 真实完整多Agent研究成功，退出码0：模型AVAILABLE、workflow COMPLETED、核心VALID。
  research_id=b8ca9c4c622e48479e5b5379c7e8858d；统一cutoff=2026-10-22T12:15:57.045Z（北京时间20:15:57.045）。
  最终proposed_action=WAIT、model_proposed_action=WAIT、plan_status=WAIT、plan_valid=False、
  position_context=UNKNOWN、allow_new_risk=False、execution_enabled=False。不是数据/模型故障被伪装成观望。
  独立多头案例保留日线上行证据、空头案例保留4h下行证据，1h混合与跨周期冲突支持等待。
  所有预期角色均有非空报告；Research Manager/Trader/Portfolio Manager均使用新动作Schema，没有SELL转开空。
  当前衍生品到结束时已过300秒，六项实时观察标STALE_DATA，四类历史仍有效；WAIT不需要为了入场强行刷新。
  本次requires_revalidation=False表示没有待入场的候选要复核，绝不表示实时盘口仍新鲜；任何新入场研究都需重新获取证据。
  实际全流程未产生有效开仓候选；有效多空候选与重大变化刷新路径分别由完整模拟工作流及真实公开API复核验证，
  不把它们混称为真实市场发现了交易机会。
  原完整报告：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_bd172b4aba88\complete_report.html。
  复核后将同一已保存模型意见另存为更易读报告，并明确记录5bps测试费率及4小时成本窗口：
  C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_plan_review_2127a4fe576a\complete_report.html。
  另存未调用模型或行情接口，核心当时仍VALID，未覆盖原报告。该目录同时有research_result.json和Schema。
  5bps是假设，不是实际账户费率；模型旧意见中“完整成本/账户背景缺失”不能解释为程序没有收到测试费率。
  已补充后续角色提示词，明确向模型传递成本假设并区分实际账户费用；数值可行性仍由程序决定。

## 本轮文件

新增agents/perpetual_schemas.py、agents/perpetual_research.py、dataflows/perpetual_plan.py、
tests/test_perpetual_plan.py、tests/test_perpetual_plan_public.py及本文档。
修改cli/main.py、default_config.py、agents/state.py、agents/context.py、
agents/managers/research_manager.py、agents/managers/portfolio_manager.py、agents/trader/trader.py、
dataflows/okx_research.py、dataflows/vendors/okx_derivatives.py、graph/setup.py、graph/trading_graph.py、
reporting.py、report_html.py。

## 限制、回滚与阶段D建议

没有实际入场触发监控、订单、账户风控或实际费率查询。参考规模不是账户风险预算；
没有保证金/杠杆/强平适用性审查，条件候选仍需人工判断与后续新验证。
只支持保守的突破/回踩模板，趋势冲突时可能长期WAIT；不将少量模拟样例当作策略盈利证据。
实时深度对未来成交的代理、入场名义金额对资金费用的代理都有局限，不能作为真实历史回测结果。
报告保留模型的定性观点，程序不声称验证所有自然语言判断。

回滚只需移除--trade-plan或设置okx_trade_plan_enabled=False，原股票和阶段A/B路径恢复。
不需要删除报告、个人配置、虚拟环境或API Key。

阶段D建议：复用原backtest/memory/reflection，将本阶段规则版本、配置、原快照与独立复核记录作为输入；
使用真实历史资金结算、明确maker/taker/滑点假设与保守K线内止盈止损顺序；
不能用当前订单簿/OI替代历史。先做可复现的多空撮合单测，再做样本外、回撤、费用敏感性与误差分类。
阶段C到此停止，不自动实施阶段D。
