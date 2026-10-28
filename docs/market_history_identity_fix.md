# Market 工具历史折叠证据身份最小修复

状态：READY_FOR_COMMIT（本地工程有条件通过，保留F3.1既有基线失败；未进行真实模型或Vultr验证）。基线56b94e6726baf6da25b859743ccd7b803c250cb6；分支codex/market-history-evidence-identity。
本轮仅本地开发和确定性测试；无Vultr部署、真实模型、正式账本初始化、授权恢复、Commit/Push。

## 根因及最小生产改动

原project_history仅以工具名和正文哈希比较，相同正文即使artifact来源或调用参数不同也会引用先前结果。新规则market-tool-identity-v1仅改变该请求前历史投影，不改变工具展示、原始图状态或任何证据档案。

允许折叠必须同时满足：

1. 原生已知Market工具、成功ToolMessage、正文超过2048字符。
2. artifact必须为None；即使非None artifact相同或为空字典/列表，也保守排除。只检查对象是否为None，不调用其bool、相等、repr或序列化。additional_kwargs或response_metadata非空也排除。
3. 可唯一关联到此前同名AI工具调用，call_id及result_id均无歧义；未知关联、前向引用、重复ID、名称不符不折叠。
4. 调用args是可确定序列化的纯JSON字典，按键排序规范化后完全一致；保留类型、列表顺序和所有参数，不把不同日期、来源、标的或快照参数当成相同。NaN、Inf、自定义对象、非字符串键、循环结构等身份未知时不折叠。
5. 正文具有原生pinned工具来源头部：source、exchange、symbol、interval、64位snapshot_id、fetched_at_ms、cutoff_ms及latest_close_ms。缺任一字段或身份未知不折叠。
6. 正文哈希相同。因此可见来源、标的、快照、实际范围、数据质量和风险正文不同不能折叠；被省略的表格仍在既有原始档案中。

首份完整证据保留在当前同一次请求内。后续引用包含原消息位置、首份tool_call_id、正文SHA-256、参数SHA-256和identity版本。各次AI参数、ToolMessage.tool_call_id、ID、顺序、artifact、原生消息协议不变，不跨研究或运行共享。Bull/Bear仍读取原market_report字符串接口；不新增证据库或其他Agent逻辑。

## 不变范围

30行展示上限、全范围摘要、完整OHLCV/衍生品档案、指标及结构计算均未改。bound_csv_tables、_table_summary、_moving_averages、market_tool四函数与基线AST相同；A/B工具输出逐轮字节/哈希相同。MA20/60/120、RSI、MACD、OI、资金费率、盘口、STALE/UNAVAILABLE及原生TTL规则不变。

11角色、辩论/独立风险审核、60万Token软阈值、10万操作余量、24逻辑调用、240HTTP、900秒及F1准入不变。预算守卫源文件和实现指纹算法不改；本地实际守卫指纹仍为e4d91175230e22cc04cb0bc1158d208602bf5484a42cdff762e610909d264f30，不替代Linux原生指纹；不得用旧失败账本或已消费授权重跑。

## 同快照、同九轮修复前后对照

快照文件SHA-256：bdd2b2074ca8b980eaf69be753b4efd6941cdc6f6e14960bbab723080dab7890。
仅比较56b94e版本折叠与本次折叠；两组都使用已验收的相同行情展示。九轮工具序列、快照及参数完全相同，且A/B脚本已将原空args占位替换为实际暴露的start/end、indicator、curr_date、look_back_days；不加入注入态大数据，不改变工具序列。

|轮次|工具|修复前累计工具正文字节|修复后累计工具正文字节|
|---|---|---:|---:|
|1|get_stock_data|23,256|23,256|
|2|get_verified_market_snapshot|58,981|58,981|
|3|get_indicators|123,155|123,155|
|4|get_stock_data|123,408|123,520|
|5|get_indicators|131,531|131,643|
|6|get_verified_market_snapshot|131,798|132,022|
|7|get_stock_data|132,051|132,387|
|8|get_indicators|140,256|140,592|
|9|get_verified_market_snapshot|140,523|140,971|

第9轮140523→140971，增加448字节，来自更完整的引用。原始数据、全部既有指标和每轮工具正文/原生调用ID不变；相同九轮仍满足明确身份的重复折叠。不含系统提示、模型正文和协议开销。实际Token/费用UNKNOWN，不将字节差异视为真实Token节省。

复核驱动读取基线Git blob中的project_history，以当前scripts.market_display_ab.evaluate的history_projector参数分别注入旧/新函数；网络连接禁止，指标、快照序列化前后相等。完整字符数、字节数、哈希及基线模块SHA-256见market_history_identity_ab.json。未保存提示词或认证信息。

## 测试记录

- 原Market专项43项及新增身份边界40项：83通过（最终可见身份门槛版本）。
- 边界覆盖非空/相同/不同/不可比较artifact，缺失artifact，参数差异，标的/时间/来源/快照差异，元数据来源差异，缺失头部，未知/歧义调用ID，原生协议及收尾转换。
- 相关预算/认证/宏观/OKX/完整mock工作流与F3.1组合：281通过、1项既有失败。失败仍为test_opening_observation_f31.py::test_init_is_opt_in_and_no_production_activation_of_uncommitted_code，预期Reviewed异常未抛出。未修改、跳过或隐藏该断言；此前已在固定父版本复现。
- 组合运行开始后补充了可见身份缺失时保守排除的条件；最终83专项已覆盖该条件（83通过，13.10秒），另重跑234项相关回归：234通过，68.55秒。各批有重叠，不相加宣称全量通过。

## 风险及后续

保守排除可能减少实际压缩率；尤其带artifact、工具元数据或丢失原始AI调用关联的历史将完整保留。证据身份正确性优先，不放宽预算去补偿。正文仍只展示尾部30行和确定性结构，早期所有形态顺序及真实模型判断质量未被本轮证明。没有真实模型或Vultr验证，不能保证完整11角色在60万预算内完成。

## 文件清单

1. tradingagents/agents/analysts/market_display.py：唯一生产文件，仅历史身份门槛与引用。
2. tests/test_market_display.py：原折叠用例使用明确的原生身份头与AI调用关联。
3. tests/test_market_history_identity.py：40项身份边界测试。
4. scripts/market_display_ab.py：可注入前后投影、记录实际工具参数。
5. docs/market_history_identity_ab.json：离线对照证据，无原始敏感数据。
6. docs/market_history_identity_fix.md：本说明。

原market_display_optimization.md及其原始A/B记录保持不变，作为56b94e原验收证据；本说明仅替代其历史折叠身份规则。

execution_enabled=False；最终allow_new_risk=False；simulation_admitted=False。
