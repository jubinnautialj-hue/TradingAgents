# Market指标工具参数契约最小修复

状态：READY_FOR_COMMIT（本地工程验收；未提交、未部署、未真实调用模型）。
当前分支：codex/f1-revalidation-admission-oi-diagnostics。
基线：8222a95fe645842e71d0dc2a144700ef7a507be2。

## 六个修改/新增文件

- tradingagents/agents/analysts/market_contract.py：新增Market参数模型、脱敏诊断、ToolNode注入前准入包装。
- tradingagents/agents/analysts/market_analyst.py：Market指标工具使用专用Schema；模型消息解析后记录结构诊断。
- tradingagents/graph/setup.py：仅market子图启用原生ToolNode的wrap_tool_call。
- tests/test_market_tool_contract.py：新增41项离线专项。
- tests/test_market_display.py：原“全部工具Schema完全相同”断言，明确改为指标工具使用新版受限Schema；其余工具仍要求原Schema一致，注入字段保留。未放宽身份、输出或计算断言。
- docs/market_tool_contract.md：本文。

生产变化仅限Market指标参数准入和可观测性。shared agents/tools.py、指标计算、SDK、Market展示/历史折叠、预算、TTL和F1/F2/F3源码未改。没有新Agent或调度系统。

## 契约前后

|输入|原行为|新Market行为|
|---|---|---|
|缺indicator/curr_date|ToolNode缺参error，错误正文包含原参数|恢复型error：MISSING_REQUIRED；不回显参数|
|空/空白指标|空字符串success|EMPTY_INDICATOR|
|未知指标|错误文字但success|UNKNOWN_INDICATOR|
|数组/错误字段类型|部分由Schema拒绝|ARGUMENT_TYPE|
|不存在/非法格式日期|可能被原日期工具兜底|INVALID_DATE，不自动补填|
|lookback为bool/float/字符串/None|可被宽松转换或拒绝|LOOKBACK_TYPE|
|lookback整数0、负值或超过3650|错误文字但success|LOOKBACK_RANGE|
|错拼/未知字段|默认忽略，可能默用30|UNKNOWN_PARAMETER|
|模型传symbol/trade_date/market_snapshot|可能与注入状态冲突|INJECTED_ARGUMENT_CONFLICT|
|合法整数1..3650、登记指标、合法日期|原生计算与展示|原生计算与展示逐字节兼容|

只在Market的工具副本使用新args_schema，原始共享工具不变。公开参数模型与包含原生InjectedState字段的工具模型分开，以避免LangChain生成模型可见子Schema时丢失自定义验证器。ToolNode包装在注入前检查公开参数，原工具仍执行原生注入和计算。
明确的收紧：数值字符串“30”和布尔值不再被当作整数；非法日期、空逗号段及未知字段不再自动宽容。整数输入的合法单项、逗号及并行输出保持不变。
不规范化/改写模型原参数；名称验证可识别大小写和周围空白，计算仍由原函数进行其既有trim/lower。缺省look_back_days=30是原生可选默认，不填补缺失必填字段。

## 两层诊断与错误流

MODEL_PARSED：take_turn返回AIMessage之后，观察tool_calls/invalid_tool_calls。不是原始SSE或SDK解码前边界。
TOOLNODE_ENTRY：原生wrap_tool_call接收到工具请求、InjectedState注入之前。
ARGUMENT_VALIDATION及TOOL_RESULT补充静态错误或执行状态。
同一call_id和结构指纹可关联两层；字段在后层丢失时present位图/结构指纹改变。结构指纹只描述允许字段的类型、存在性及未知字段数量，不记录值哈希，不能识别同类型的值漂移。

空参数示例（两个阶段实际结构指纹相同）：
- call_id=call_trace, tool=get_indicators, stage=MODEL_PARSED, code=OBSERVED
- call_id=call_trace, tool=get_indicators, stage=TOOLNODE_ENTRY, code=OBSERVED
- call_id=call_trace, tool=get_indicators, stage=ARGUMENT_VALIDATION, code=MISSING_REQUIRED
- present={indicator:false,curr_date:false,look_back_days:false}
- structure_sha256=d2d9ad1992f002827dfa86b32b8575fdac4d7d5a90dc506ff260987f6a0c0bb2

日志仅包含固定版本、阶段、静态代码、允许工具名、调用ID、允许字段位图和结构指纹。未知字段名、参数原值、任意异常文本、提示词和完整响应均不保存；非法字符串ID改用SHA256保证关联。
日志走标准logging INFO；受控研究若已设置TA_SINGLE_AUDIT_FILE，则同时复用该既有元数据文件，kind=MARKET_TOOL_DIAGNOSTIC，便于在默认INFO过滤时仍保留证据。未配置该文件的调用方须配置INFO日志采集才能持久保留日志。
文件单条追加、fsync、O_NOFOLLOW（平台支持时）、0600新文件权限，容量上限4MiB；满额/不完整写入明确失败，不无限写入或静默丢失。文件不可写的I/O错误保持可见；这不是可恢复的模型参数错误，不自动重试。不会创建新目录或访问认证材料。

非法参数不进入工具函数，返回ToolMessage(status=error)，保留原tool_call_id和协议，模型可以在后续自己的回合中更正。包装器只执行一次，不自动补齐、改参或重试；其他运行时/核心错误继续传播，不全局吞成参数错误。
错误消息不含原值，不修改冻结快照、采集范围或质量状态；F1仍只按原生冻结证据与原准入判断。恢复后也不能将错误输出当作有效市场证据。
原生11角色、多空辩论、独立风险审核和工具轮次数不变。F3独立重验未改。

## 离线证据

历史153字节签名：
1253ace65ba08833ece9931efd8893b95be5b94f656dbe99f5c8049e36e46318。
使用旧共享工具/旧ToolNode错误处理作基线，14次空参数调用均复现相同153字节error。新Market路径仍拒绝14次调用，但改用有限脱敏静态错误消息，不承诺旧错误文字逐字节相同。
这不证明历史模型原始arguments为空，SDK流式解析或消息转换层为何产生缺参仍UNKNOWN。新增诊断仅能帮助后续受控调用区分解析后与ToolNode入口，不能补造过去的原始响应。

专项覆盖：
缺字段、空/空白/未知指标、数组、非法日期、非法lookback类型和上下界、未知字段、注入冲突；合法7指标单项/并行/逗号输出与原工具逐字节比较；原始快照不变；错误不参与身份折叠；模型替身通过实际Market子图从错误恢复；双边界缺参/结构丢失关联；响应/参数/未知键/未解析原文脱敏；复用审计文件和容量保护；核心运行时错误不被降级。
所有场景使用本地fixture、原生图和模型替身，不调用真实模型或业务HTTP。正式trial-001/002、账本、报告与授权未触碰。

## 测试结果（保留完整失败）

1. 初轮Market专项：118 passed（当时35项新测试+83项既有专项）。
2. 首轮扩展相关回归：518 passed, 2 failed，覆盖Market、11角色图、OKX研究、预算、认证、正式准入和F1/F2/F3。
3. 诊断审计文件收尾修改后，最终受影响范围：261 passed，含最终41项新增专项、83项既有Market专项，以及图、预算、注册证明、观察资格。
4. 两项失败在独立git archive HEAD基线副本中原样复现：2 failed in10.27s。未修改/跳过/隐藏：
   - tests/test_opening_observation_f31.py:232，Reviewed断言DID NOT RAISE。
   - tests/test_opening_observation_f31.py:288，当前F1源码与更早b01436c逐字节比较失败。当前基线8222a95本就包含后续F1时效兼容变更。
   不能将第二项误报为本轮新增F1修改，也不能继续只声称存在一项基线失败。
没有宣称全量零失败。初轮扩展套件在收尾修改前运行，最后版本复测上述261项直接受影响范围，不冒充所有扩展用例均在最后一次运行。

原始记录位于本地分析目录：
C:/Users/jerry_su/.tradingagents/logs/trial002_market_offline_contract_20261103/
implementation-regression.txt、final-affected-regression.txt、baseline-failures.txt。
基线副本仅为离线测试，未创建分支或变更HEAD。

## 身份及停止边界

预算守卫implementation_hash的四个输入文件safety_budget.py、guarded_research.py、research_budget.py、auto_research.py均未改，算法未改；Policy/预算Schema及其配置身份未改。这里不把Windows原始字节指纹当成Linux指纹。
本轮研究源码发生了有意改动，未来固化和部署必须使用新的提交及源码清单，不能直接覆盖既有固定提交目录或混用旧运行授权。
新诊断与参数契约版本为market-tool-contract-v1，不更改旧F1/F2/F3协议解释或旧档案。
当前不提交、不推送、不部署Vultr，不初始化新研究窗口、不调用模型、不启用timer/F2/F3或交易。三个最终安全许可保持False。
