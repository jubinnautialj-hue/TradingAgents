# Market 模型展示与上下文最小优化验收

状态：READY_FOR_COMMIT（本地工程有条件通过，真实模型质量A/B待另行授权）。基线 d1793f664742473964edd301eef97d7b22899fea；分支 codex/market-context-bounded-display。
本轮仅本地开发、离线测试。无模型调用、无Vultr操作、无正式档案/账本修改、无Commit/Push。

## 已证实路径与证据边界

Market TOOLS → get_stock_data → _okx_snapshot 原生校验 → okx.render_prices → 按请求日期范围输出完整三周期CSV。get_indicators同样按范围输出各指标历史。Market私有子图保留工具消息；take_turn/_as_text收尾轮仍携带此前结果。原真实9调用没有工具参数或完整消息记录，故不能逐字段认定实际518719 Token的来源。

本改动只在Market工具元组包裹原工具，先完整执行原函数、原校验和计算，再对OKX数值表格进行展示压缩。共享tools.py、okx.py、指标/结构计算、News、Sentiment、其他角色、研究提示词、图拓扑不改。不是新的策略或Agent框架。

## 展示契约 market-display-v1

- 原始OHLCV/衍生品快照、完整指标计算输入和封存指纹保持不变；展示数据不是计算输入。
- 每张超过30行的数值表格保留最后30行，同时保留全范围首尾时间、行数、各字段首末值、最大最小及时间、所有缺失/非有限值及时间、成交量合计。完整原表SHA-256与原头部snapshot_id共同用于追溯。范围不足、未收盘排除、STALE_DATA等原非表格文本不裁剪。
- 未识别或短表格原样保留；不做盲目的字符截断。摘要由Decimal/确定性计算产生，不调用模型。
- MA20/60/120为全量收盘数据计算的补充简单移动均线，明确按各周期K线根数计，标记为补充统计，不参与既有策略阈值或信号判定。原有MA50/200、RSI、MACD等计算不变。
- get_verified_market_snapshot不变，完整保留原有多周期结构、共振/冲突、指标、OI、资金费率、标记/指数价格、盘口、覆盖/缺失/过期信息。数据质量快照allow_new_risk不代表最终交易许可，原语义不变。
- 仅在OKX Market发送模型请求前，折叠同一请求内同名、内容SHA完全一致且超过2048字符的重复工具结果。第一份完整证据始终保留，后续保留指向消息位置、tool_call_id及哈希的引用。不同内容或质量状态不合并，不跨运行记忆，不改图状态、原生工具ID或配对顺序。收尾转换保持原生兼容。
- 股票工具路径原样保留；News/Sentiment不使用该展示器。Bull/Bear及后续角色仍读取相同market_report字符串接口，角色和辩论/风险轮数不改。

## 同快照离线A/B

冻结快照文件SHA-256：bdd2b2074ca8b980eaf69be753b4efd6941cdc6f6e14960bbab723080dab7890。
快照ID：79e1d0c247dc13deddeae224ca4ccc9aabfe5d8cea598874d0478370edb9b477。
A调用未修改的原工具函数；B调用Market专用包装器及请求前重复投影。九轮是明确编排的测试工具序列，不是历史9次模型调用的复原。A/B使用同一快照、日期、参数和指标。冻结时钟仅供原校验离线回放，不延长实时TTL，不产生PROSPECTIVE登记。全部网络连接被禁用。

|轮次|工具|A累计工具内容字节|B累计工具内容字节|A粗估Token|B粗估Token|
|---|---|---:|---:|---:|---:|
|1|get_stock_data|138,307|23,256|34,577|5,814|
|2|get_verified_market_snapshot|174,032|58,981|43,508|14,746|
|3|get_indicators|312,174|123,155|78,044|30,789|
|4|get_stock_data|450,482|123,408|112,621|30,852|
|5|get_indicators|468,172|131,531|117,043|32,883|
|6|get_verified_market_snapshot|503,897|131,798|125,975|32,950|
|7|get_stock_data|642,205|132,051|160,552|33,013|
|8|get_indicators|660,200|140,256|165,050|35,064|
|9|get_verified_market_snapshot|695,925|140,523|173,982|35,131|

这里只统计工具内容及拼接换行，未包括相同系统提示、模型生成文本或服务端协议开销。字符、字节、哈希及每轮输出明细见 market_display_ab.json。
Token粗估严格定义为ceil(UTF-8字节/4)，可复现但不是实际Tokenizer、SDK计量或成本；数字CSV的真实分词可能差异很大。没有为此下载分词表或发起外部请求。

首轮价格工具输出138306→23255字节；八指标输出138140→64172字节；验证快照35723→35723字节。第9轮累计695925→140523字节（约79.8%字节减少），不得表述为实际模型Token减少79.8%。短范围工具输出可能因附加统计略增；不承诺所有调用均更小。

## 数据、证据和安全验证

原快照序列化字节、各周期全部既有指标计算值、snapshot_id均在A/B前后相等；验证快照原文相等。测试覆盖原生ToolNode注入/配对、重复消息顺序、STALE和指纹篡改拒绝、历史覆盖、极值/缺失/成交量、MA全输入计算及其他角色隔离。工具摘要保留原始快照和完整表哈希，封存报告和F1逻辑未改，不创建新的正式F1样本。

安全预算600000软阈值、100000调用前余量、24逻辑调用、240HTTP、900秒及所有TTL/准入规则不改。safety_budget/guarded_research/research_budget/auto_research四文件及实现指纹算法均未修改。该算法未包含Market展示代码，不能把相同守卫指纹误认为整个研究实现相同；未来运行需使用新Git版本和独立审批/账本，不恢复旧失败授权。

## 质量风险与后续审批

1. 尾部30行和全范围极值不能完整表达更早的震荡顺序、背离、局部形态；模型必须标注未展示历史模式未知，不得声称已检查所有旧行。原结构/pivot/指标和全量档案仍可追溯，但不能证明模型结论与旧版相同。
2. 精确重复引用可能降低模型对多次同文风险提示的注意力；第一份完整证据保留，尚无真实模型A/B质量证明。
3. MA20/60/120是明确新增的确定性补充统计，不替换已有MA50/200策略依据。
4. 未执行真实模型，不能保证11角色在60万软预算内完成，也不能声称交易判断或盈利改善。
5. 工程验收后如需模型A/B或Vultr隔离验收应另行授权。同历史快照的模型A/B必须标REPLAY/测试，不能混入正式前向绩效；原失败账本及消费标记保持封存。

## 文件清单

1. tradingagents/agents/analysts/market_analyst.py：仅Market接入。
2. tradingagents/agents/analysts/market_display.py：有界展示和精确重复投影。
3. tests/test_market_display.py：隔离专项测试。
4. scripts/market_display_ab.py：零网络、同快照离线复核。
5. docs/market_display_ab.json：有限、无提示词正文的离线测量证据。
6. docs/market_display_optimization.md：本说明及验收记录。

复现（已配置项目Python环境）：

```powershell
python -B -m pytest tests/test_market_display.py -q
python -B scripts/market_display_ab.py --snapshot <冻结快照路径> --output <独立输出路径>
```

## 测试记录

- 最终新增专项：43通过，包含原生图ToolNode注入、非法日期不附加有效补充证据、跨状态不去重等。
- 预算/认证/宏观/多周期/Market图与完整mock工作流相关组：234通过。
- 扩大兼容组：F1/F2/F3/F3.1、R1.1/R1.2/R1.3、衍生品与市场检查，既有测试384通过、1失败；失败为 test_opening_observation_f31.py::test_init_is_opt_in_and_no_production_activation_of_uncommitted_code（预期Reviewed异常未抛出）。固定d1793f基线的独立归档副本、同Git HEAD查询条件下复现相同断言失败。未修改或隐藏该测试。
- 扩大组初次运行还包含新增ToolNode测试的运行环境构造错误：直接invoke缺少图runtime；已改成真实StateGraph执行，后续专项复测43项全部通过。初次423项总结果为421通过、2失败，不将其改写成零失败。
- 最终专项与既有失败联合复测：43通过、1项上述既有失败。各批有重叠，不相加宣称独立测试总数，也未运行全仓全量回归。
- 基线副本首次缺少Git元数据导致另一环境错误；补上仅供git show/rev-parse读取的原仓库元数据引用后复现正式断言。未Commit、未变更原仓库Git数据。
- git diff --check通过；共享工具/OKX/预算/认证/归档协议源文件与基线无差异。
- Windows现有守卫实现指纹仍为 e4d91175230e22cc04cb0bc1158d208602bf5484a42cdff762e610909d264f30；无算法变更，不用它替代Linux指纹。
- 本地实际解释器Python 3.13.13，复用项目已安装依赖；未安装新依赖。所有测试使用隔离目录及网络阻断。


execution_enabled=False；最终allow_new_risk=False；simulation_admitted=False。
