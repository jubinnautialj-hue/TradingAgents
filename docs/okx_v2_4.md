# V2.4 官方发布证据、描述性统计与固定快照基础

基线 `8c6384332d25aa30132e7da08f0fcf93c8fc2b1e`；分支 `codex/okx-macro-events-signal-calibration`。默认关闭，不提交、不推送、不修改全局 MCP、不新增 Agent、不启用交易。

本批的工程范围是**官方发布标题及其时间证据、现有模拟记录的描述性统计、固定输入与证据差异审计**。不是完整经济日历/宏观数据终端，也未建立概率校准模型或前向模拟跟踪系统。缺口不能用模型记忆、推断值或当前新闻填补。

## 复用原生功能及发现的缺口

- 保留 Market Analyst、Bull/Bear、Research Manager、Trader、三位 Risk Analyst 和 Portfolio Manager。
- 原 News Analyst 本来提供股票新闻/FRED/预测市场工具，原 Sentiment Analyst 会抓 Yahoo、StockTwits、Reddit；它们不是 OKX 历史可知数据适配器，因此不能原样用于本次永续研究。
- `--events` 只启用**已经存在的 News Analyst 和 Sentiment Analyst**，总共十一位既有角色；没有创建新 Agent 类型或重建工作流。前者整理事实，后者只解释同一组标题的表述，不称为真实社交情绪或拥挤度调查。
- 原股票路径、V2.1—V2.3 独立开关和默认市场研究保持兼容。
- 复用 D.1 `okx_backtest.feedback` 过滤已知模拟结果；复用 D.5 `OKXSimulationMemory.load_feedback(as_of=...)` 和原 Markdown 存储/可用时间规则。没有新数据库、RAG、自动策略调整或新 Reflection 调用。
- D.5 模拟 Memory/Reflection **仍未接入实时研究 Graph 自动学习**。本批统计是离线、只读适配，不能把已有历史收益灌回当时研究。

## 免费官方来源与实际覆盖

| 来源 | 本批用途 | 实际公开验证/限制 |
|---|---|---|
| Federal Reserve press_monetary.xml | 货币政策发布、纪要标题 | HTTP 200；最多前 50 条，pubDate 含时区 |
| Federal Reserve speeches.xml | 官员讲话发布标题 | HTTP 200；不等同于讲话实际发生时间 |
| BLS cpi.rss / ppi.rss / empsit.rss | CPI/PPI/就业发布 | 三个均 HTTP 403；不绕过、不推断实际值 |
| SEC pressreleases.rss | 匹配加密/ETF/监管关键词的官方发布 | HTTP 200；非完整法律/诉讼/ETF档案 |
| Bitcoin Core en/rss.xml | BTC 软件官方发布 | HTTP 200；有历史标题，不是全部 Bitcoin 新闻 |
| Ethereum blog en/feed.xml | ETH 官方项目发布 | 使用官方明确的 /en/ 地址；旧地址重定向不自动跟随 |
| NEAR blog/rss.xml | 预留 NEAR 官方源 | 实测 HTTP 404，明确不可用，不伪造项目事件 |
| OKX /api/v5/support/announcements | 标的相关交易所公告 | 公开、无 Key、只取第一页 20 条；地域返回可能不同 |

官方资料：[Fed RSS](https://www.federalreserve.gov/feeds/feeds.htm)、[BLS RSS](https://www.bls.gov/feed/)、[SEC 新闻](https://www.sec.gov/newsroom)、[OKX Announcements API](https://www.okx.com/docs-v5/en/#announcement-get-announcements)。

仅固定 HTTPS 白名单；无认证头、无 .netrc、无任意 URL、无自动重定向。RSS 每源最多 1MiB、最多前 50 项，瞬时网络/429/5xx 最多两次，403 直接降级；每研究进程内缓存重复源。DTD/ENTITY 拒绝，不执行正文或标题中的指令。沿用 requests 和 OKXPublicClient；没有购买服务或安装新框架。

## 时间、来源与事件语义

采集在原市场 cutoff 冻结之前完成。每条入选事件都必须满足 publisher-reported published_at_ms<=cutoff 且 first_seen_ms<=cutoff，默认在之前 14 天内；同样适用于跨时区 pubDate，缺时区日期拒绝。

分别保存：

- `published_at_ms`：RSS/Atom 明示的首次发布时间；没有 published 的 Atom updated 不冒充首次发布。
- `first_seen_ms`、`fetched_at_ms`：本程序实际获取时间；不回填为历史发布时间。
- `publisher_display_time_ms`：OKX businessPTime，仅显示时间。
- `occurred_at_ms`、`scheduled_at_ms`：本批无法核实时为 null，不能拿发布时间代替。
- `actual_value`、`consensus_value`：本批无可靠结构化值/预期源，明确 null，不从标题、模型记忆或修订数据虚构。

OKX 使用实际首次发布字段 **pTime**，绝不用 businessPTime 替代；官方说明可能延迟约五分钟，first_seen 记录真正观察时间。来源返回有效不等于内容全面或市场影响已证实。

当前无完整历史版本/首次可访问档案。历史日期不拉当前 RSS，不将旧 pubDate 的今天版本当成当时信息。固定归档可审计已实际采集输入，但不声称补齐事件之前的历史版本。模型自身潜在知识污染也不能由提示词彻底消除。

## 标的关联、去重与质量

- Fed 全局宏观来源；SEC 只筛选明确加密/ETF/数字资产关键词。项目专属官方源按 BTC/ETH/NEAR 路由。
- OKX 公告按名称、独立 ticker 或 BTCUSDT 等合约符号匹配。NEAR 不把小写普通英文 near（如 near future）当作代币。
- 相同规范化 URL 或同 UTC 发布日的相同规范化标题归为一组，保留一个 event_id，记录重复数。最多 12 个最新合格组，超出显式记录。该方法不是完整跨语言语义聚类，可能遗漏关联报道或保守合并同名内容。
- 两位角色以及多空辩论读取**同一组 event_id**，不得把 News 与 Sentiment 的两次解读算成两个独立事实。程序不按新闻篇数直接加分或生成买卖动作。
- 官方确认只指官方确实发布该标题，不证明催化强度、价格因果或所有底层事实。非官方/未经证实源没有接入，不混成官方数据。
- 总状态至多 PARTIAL，表示不完整官方样本；全部不可用为 DATA_UNAVAILABLE，不转成 HOLD/BUY/SELL。
- 发布快照 TTL 900 秒（独立于原市场/订单流 TTL），过期清除有效视图中的事件，原始档案保留。没有延长 V2.1—V2.3 的市场证据有效期。

**尚无可靠覆盖**：完整预定经济日历、CPI/PPI/NFP 结构化实际值与一致市场预期、通用代币解锁/供应时序、安全事故全量档案、NEAR 新闻归档、所有 ETF/监管法律事项。这些字段/来源缺失必须显示，不能声称“无风险事件”。

## 原生流程与报告

`--events` → 原 preflight 前 `prepare_events` → 原市场质量闸门 → `bind_events` → state.event_evidence → Market + 原 News/Sentiment 并行 → 原多空辩论/经理/Trader/风险/PM → 原 final_gate。

News/Sentiment 的新分支只对固定事件快照调用模型。Graph 层将这两位角色的外部 tools 置空，旧 FRED/预测市场/股票社交抓取无法从该分支执行。并行启用时 Market 是该步骤唯一 llm_status/workflow_status 写入者，避免 LangGraph 并行写冲突；任一角色模型故障仍由原失败边界上报。

输出沿用原报告系统：HTML 详情区、Markdown、`events_snapshot.json` original/effective_view，以及交易预案 JSON 的 `evidence.events`。原预案 Schema、数值规则、证据 ID 白名单、回测和执行权限不变。

事件档案附带提示词版本 `okx-pinned-publication-v1`、事件/提示词文件哈希及允许列表中的模型名称、provider、角色、轮次等设置，不包含 endpoint、Key 或本地凭据。模型名称是配置标识，不是供应商模型权重的不可变版本。

## 描述性统计，不冒充概率校准

`python -m tradingagents.okx_signal_audit ledger ...`：复用原反馈结果时间选择器，核验候选/成交归属、方向、cutoff 和未来证据，区分 WAIT、数据故障、候选无已结算成交及生命周期事件。归档文件当时尚不存在时拒绝历史评估。

`memory ...`：只读原 D.5 Memory 已可用的 feedback，保留研究状态、动作、结局、模型/策略版本与快照 ID，不读取反思作为实时研究输入，不调用模型，不写入任何决策或收益。

本次实际旧开发集统计（未重新回测、未调参）：

| 项目 | 结果 |
|---|---:|
| 历史决策点 | 2316 |
| 多头候选 / 空头候选 | 49 / 230 |
| WAIT | 2037 |
| 候选无已结算模拟成交 | 265（不一律解释为策略成功拒绝） |
| 成交 | 14：多头2、空头12 |
| 盈利 / 亏损 | 4 / 10 |
| 逐笔净损益之和 | 约 −21.9648300348 USDT |
| 多头 / 空头净损益 | 约 −13.6099965310 / −8.3548335038 USDT |
| 原 regime UP / DOWN 样本 | 2 / 12，与原标签分组，不重标环境 |

逐笔求和与原权益法净损益约有 4e-12 USDT 浮点差，原账本不改。原合约历史参数、费用和资金结算假设仍适用。

D.5 读到 14 条反馈：6 COMPLETED（均 ACCEPT，关联4盈利2亏损）、8 MODEL_FAILED/NO_COMPLETE_DECISION。失败不算 ABSTAIN，更不算成功避险；不是自动算出的 Agent 组合收益。

原档案没有 V2.3 波动率标签或当时新闻特征，报告为 UNRECORDED/NOT_IDENTIFIABLE，不拿当前事件或新标签回填。没有固定持有期方向收益标签或正式发出的概率，因此不计算 Brier/ECE、不拟合成功率；`predictive_success_probability=null`，14 笔标记小样本不足。统计规则表现与多 Agent 筛选价值保持分离，不能把历史收益解释成未来胜率。

## 固定快照及对照基础

`freeze` 只读取允许的原研究 JSON 文件，要求有效多周期核心、父/子指纹、统一 cutoff 及已收盘数据，并检查事件来源快照、发布与首次观察时间。固定输入包含原研究结果和可用的事件/订单流/跨所/盘口档案，附独立内容哈希。最多组合 16MiB。不是重新查询来源或重跑模型，不把旧状态授权为当前入场。

`compare` 要求合约、核心 snapshot_id 和 cutoff 完全相同，验证归档及各 artifact 哈希，输出新增/移除事件 ID、变化档案和原动作。不同日期的 V2.3/V2.4 真实报告会被拒绝比较；不能用它们宣称准确率提高。没有自动构造旧模型结果或挑选有利输出。

本批只准备固定输入和差异协议，不实现自动快照重放模型实验、前向持仓模拟或持续跟踪。输出文件不可覆盖不同内容；相同内容重复运行幂等，否则选择新路径。

## 测试与运行命令

V2.4 新增专项 32 项通过：发布时间/时区、未来/未观察内容、官方来源、标的匹配、去重、历史禁替代、失效/哈希、原生角色、Graph 外部工具阻断、并行失败边界、CLI/HTML/JSON、HTTP限流/403、模拟账本归属/小样本、Memory时间隔离、固定快照与不同 cutoff 拒绝。

全量回归：**1874 passed、5 个既有 Windows 失败、4 skipped、13 deselected、100 subtests passed，62.09 秒**。五项仍为两个 POSIX 私密文件权限断言、两个升级命令和 yfinance 用户 home 缓存隔离；不能声称全绿。冻结 perpetual_plan、plan_lifecycle、okx_multitimeframe 和原146个实验文件校验均通过。

## 真实 BTC 组合研究与固定快照实测

唯一一次真实模型研究 `okx_BTC-USDT-SWAP_multi_716c64fd94e0`，四批开关均开启。**Model AVAILABLE / workflow COMPLETED**，原十一位角色均生成报告，News/Sentiment 没有 INCOMPLETE 标记。模型配置为 ChatGPT / gpt-6.1-sol，研究与风险各一轮；本轮没有为对照再跑第二次模型，也没有根据输出改提示词重试。

cutoff 为 **2026-10-24 02:19:44.796 UTC / 北京时间 10:19:44.796**，最终 **WAIT / plan_status=WAIT / plan_valid=False / allow_new_risk=False / execution_enabled=False**，无有效入场、止损止盈或 RR。

实际读到三个合格发布组：美联储贴现率会议纪要发布、官员监管讲话发布及 OKX 与 BTC 等资产有关的推广公告。News 明确区分“官方发布了标题”与“核实政策发生或市场影响”；Sentiment 明确为标题框架解读，不是社交情绪。两份报告逐项引用同一 event_id，并说明促销公告不证明新增 BTC 买盘或方向。没有补造 CPI、预期、解锁数据或政策利率变化。

原始/最终事件质量均 PARTIAL（三个 BLS 源仍 403，覆盖不全）。HTML 详情与研究 JSON 均显示事件质量及时间。低频发布证据使用独立900秒 TTL，并不使已过期的 V2.1—V2.3 市场证据重新有效。

已对公开数据报告 `okx_BTC-USDT-SWAP_multi_a196e757e7a0` 和完整研究分别封存固定快照；真实父/子指纹及发布时间校验通过，同一快照重复对照没有证据差异。这只证明封存和差异协议，不是新旧模型效果 A/B。不同 cutoff 的旧版报告不能作为同点对照。

实际报告、本轮快照/统计/测试记录位于 `C:\Users\jerry_su\.tradingagents\logs\okx_v2_4\` 及上述 reports 目录。SHA256、源代码指纹、数据缺口和验收结论保存为 docs/okx_v2_4_acceptance.json。当前输入已被查看，只是工程验收样本，不能再声称是未见的盲测；原盲测协议文件未改，也没有运行该区间绩效或调参。

**最终建议：有条件通过本轮有限范围工程验收。** 官方发布证据和描述性评价/固定快照基础已接通；完整宏观值/预期/日历、通用解锁/事故历史和正式概率校准仍未实现，不得在功能清单中报为完成。

```powershell
cd D:\_01trading_agents
# 公开数据，不调用模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --events --data-only
# 四批证据组合；十一位已有角色；研究执行权限仍关闭
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --orderflow --market-checks --price-depth --events
# 原有基线账本的只读统计，使用唯一输出文件
$auditAsOf=(Get-Date).ToUniversalTime().ToString('o')
$auditOutput="C:\Users\jerry_su\.tradingagents\logs\signal_audit_$((Get-Date).ToString('yyyyMMdd_HHmmss')).json"
.\.venv\Scripts\python.exe -m tradingagents.okx_signal_audit ledger --input 'C:\Users\jerry_su\.tradingagents\logs\okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee\result.json' --as-of $auditAsOf --output $auditOutput
# 将下列报告目录和输出路径替换为实际值；不会调用模型
.\.venv\Scripts\python.exe -m tradingagents.okx_signal_audit freeze --report-dir '<实际报告目录>' --output '<新快照.json>'
.\.venv\Scripts\python.exe -m tradingagents.okx_signal_audit compare --left '<相同核心的左快照.json>' --right '<相同核心的右快照.json>' --output '<新对照.json>'
.\.venv\Scripts\python.exe -m pytest tests/test_okx_events.py tests/test_okx_signal_audit.py -q
```

Python Graph 显式选择 `['market','news','social']` 并设置 okx_events_enabled=True。仅选择 market 时仍可读取快照但不会自动增加角色。去掉 --events 恢复 V2.3；其他三批开关独立。

## 文件清单与整体完成边界

新增：vendors/okx_events.py、agents/analysts/event_context.py（既有角色的共享函数，不是新 Agent）、okx_signal_audit.py、tests/test_okx_events.py、tests/test_okx_signal_audit.py、本说明、验收 JSON。

修改：cli/main.py、agents/context.py、agents/state.py、analysts/news_analyst.py、analysts/sentiment_analyst.py、dataflows/okx_research.py、vendors/okx.py（只加公开公告路径）、default_config.py、graph/trading_graph.py、graph/setup.py、reporting.py、report_html.py。

四批共同具备可选、可追溯的研究证据入口，但仍有条件：V2.1 非完整长期 CVD；V2.2 强平是公开样本，非热力图/全量；V2.3 未建立完整盘口录像；V2.4 官方发布不全、无完整值/预期/日历、无正式概率校准。四批都没有证明准确率或盈利提升，均不授权交易。

后续前向验证若获授权，可消费固定快照 ID、cutoff、source/first_seen、配置/提示词/模型标识及独立 outcome_known/available 时间；先冻结预测目标、持有期、标签、抽样协议和成本，再开始新前向样本。不要使用已查看开发集充当新盲测，也不要让 Reflection 自动改策略。本轮不实施该跟踪系统。
