# Market工具终态安全适配（本地方案B）

状态：MARKET_SAFE_ADAPTER_READY_FOR_LINUX_ACCEPTANCE，仅表示本地离线完成，尚未Linux验收、部署或调用真实模型。

基线：0faf0ace5a588baca7df677ac8590455b5290ab4。现有分支不变，无Commit/Push。

## 五文件范围

- tradingagents/agents/analysts/market_analyst.py：仅现有ChatGPTPlanChatOpenAI的Market调用进入独立作用域；其他角色、其他既有模型路径不变。
- tradingagents/llm_clients/openai_client.py：作用域存在时，以每次调用的浅复制模型及只读客户端代理观察原始Responses流；没有作用域则走原路径。原SDK客户端不被修改，不全局patch第三方聚合器。
- tradingagents/agents/analysts/market_safe_adapter.py：新增有限终态验证器和同步流观察代理。
- tests/test_market_safe_adapter.py：TEST_FIXTURE离线SSE、Graph/ToolNode、原生Schema及真实预算守卫联动测试。
- docs/market_safe_adapter.md：本说明。

不修改第三方依赖、锁文件、模型、11角色、提示词、预算、TTL、OI、F1/F2/F3或Shadow。保持langchain-openai1.7.2、langchain-core1.6.13、openai3.36.1。

## 执行边界

Market节点 → 原take_turn和bind_tools → 项目ChatGPTPlanChatOpenAI同步_stream → 每调用ObservedRoot/ObservedResponse → SDK原始事件观察 → 原SDK转换与原生Token/账本对账 → invoke返回 → Evidence.admit → MODEL_PARSED → 原ToolNode/Market Schema。

不发送parallel_tool_calls=False，合法并行仍可使用。工具请求始终要等整个响应合法完成、SDK用量确认后才返回Market节点供ToolNode执行。

原始证据保存在本次调用内存中，按item_id、call_id、output_index唯一关联。支持单项、逗号批量、6/7项并行、交错分片、跨item完成事件乱序、delta先于added，以及没有delta但拥有完整done和completed终态参数的合法响应。

只有终态满足以下条件，才从最终完整参数建立AIMessage.tool_calls：

1. response.completed状态合法，响应ID一致；若存在created/in_progress也必须对应同一ID。SDK不总在AIMessage metadata中保留响应ID，因此不捏造该字段，使用每调用原始响应身份，已存在的metadata ID仍必须一致。
2. 每个最终工具均有唯一added和output_item.done，ID、名称、索引与完成数组位置一致。工具item的可选status缺失不会替代完整事件要求；显式非完成终态仍拒绝。
3. delta按明确item_id和索引关联，串接结果必须严格等于终态JSON对象；不推测缺少的片段。added非空前缀也参与核对。没有delta时，完整done和completed必须一致。
4. 若有function_call_arguments.done，其完整参数也必须相同，重复该终态拒绝。
5. JSON不允许重复键、非对象或NaN等非常量；比较保留JSON类型差异，不能将true当成1。
6. 工具必须是Market实际提供的工具，参数仅允许公开Schema字段，不允许模型覆盖注入的symbol/快照等；指标再经过原argument_error和严格Schema。验证结果不回填默认值或改写原参数。
7. 复用原生normalize_usage和normalized_matches，原始SDK用量必须有效且与SDK汇总一致。正式guarded路径已在invoke返回前完成原有账本对账；UNKNOWN仍暂停。

验证不是“修补任意损坏的JSON”：只有完整终态及所有已出现的参数证据一致，才用该完整值替换已证实出错的第三方聚合tool_calls。任一冲突、缺失或身份不明即拒绝，不重试工具或模型，不补默认参数。

事件/参数有界：最多8192个观察事件、64个工具、单参数65536字节、delta合计1MiB；超限失败关闭。原有模型/HTTP预算及审计4MiB限制不变。

## 对账和异常传播

观察器只记下协议错误，不提前打断原SDK的最终用量处理。协议拒绝发生于invoke返回后，已经确认的用量仍在原账本中；研究失败按原worker收尾。SDK本身提前解析失败或用量不完整仍走原UNKNOWN/PAUSED规则，绝不计为零。

真实守卫+MockTransport测试确认：一次7项工具响应仍是1次逻辑模型调用、1次HTTP及100个fixture Token；参数冲突拒绝仍保留100已确认Token；用量缺失进入PAUSED/UNKNOWN。没有新增自动重试、授权消费或研究机会。

## 审计与隔离

入场和拒绝诊断调用现有market_contract.diagnose → append_audit。事件为MARKET_SAFE_ADMISSION/TERMINAL_VERIFIED或MARKET_SAFE_REJECTION/静态错误码；只记既有调用ID处理、字段位图和结构指纹，不记录参数值、提示词、正文或凭据。拒绝摘要不虚构工具调用ID。

原0600、永久侧锁、文件身份/符号链接检查、容量及并发保护全部复用，审计失败传播并阻止工具放行。Windows不能证明POSIX模式权限，必须由Linux专项复验。

ContextVar按调用隔离；复制模型只替换本次同步请求的root_client代理，不修改共享SDK客户端。非Market调用不启用。当前固定ChatGPT同步Responses模式覆盖；include_response_headers、gateway等未验收模式显式拒绝，不静默绕行。其他10角色逻辑不变。

## 离线证据

最终专项：59通过。包括原生Market节点、SDK真实SSE解析、调用ID/索引/参数、公共Schema、并发作用域、部分流、终态冲突、用量不明、审计失败、缺失证据和正常文本WAIT兼容。

|原失败fixture|原SDK聚合|本适配|
|---|---|---|
|7项，先added再交错delta|7个空参数具名调用+7个参数孤片|7个正确ID和完整参数，无孤片|
|6项，同序列|6个空参数具名调用+6个参数孤片|6个正确ID和完整参数，无孤片|

使用不同指标及lookback逐项核对，避免相同参数掩盖错配。重复ID、索引冲突、完成数组位置冲突、delta/done/arguments.done/最终参数矛盾、缺少done/completed、非法Schema、未知用量和流中断均拒绝。模拟服务端响应明确标记TEST_FIXTURE，不是补造历史缺失事件。

相同冻结行情下，原正常顺序调用与新并行路径的7份原生get_indicators输出逐字节一致，调用ID/工具状态一致，原始快照不变；单项及逗号批量结果也一致。未人为改变模型研究方向。

相关联合回归：254通过、6项Windows/POSIX平台跳过、8个子测试通过（107.83秒）。该轮包含55项专项；之后补充两种其他Market工具各自的合法/注入参数测试，最终59项专项全部通过。范围涵盖诊断、流可靠性、预算、Market契约、正式准入配置、F1/F2.1、审计锁及观察器。

独立F3.1兼容复测：46通过、2项既有失败（117.28秒）：

- test_init_is_opt_in_and_no_production_activation_of_uncommitted_code：Reviewed DID NOT RAISE。
- test_no_core_or_legacy_protocol_bytes_modified：对旧b01436cc协议字节比较失败。

相关F3.1测试及其比较的历史协议文件不在本次diff中，不修改、跳过或隐藏失败；不宣称项目全量零失败。

日志：C:/Users/jerry_su/.tradingagents/logs/market_safe_adapter_20261104/final-regression.log、specialist.log。全部测试使用临时目录、冻结fixture及替身，未请求真实模型或业务HTTP。

## 指纹与后续Linux验收

四个原生预算守卫指纹输入文件和算法没有修改，因此本地原生守卫指纹仍为4c5bb55316405ced2bf12c13a61bf4e0c8c4088b455edca376a228d703d72007（Windows字节实测）。不能将该值当作Linux指纹，也不能以守卫指纹未变声称完整研究代码未变。新增适配代码及两个生产修改文件应独立按Git blob/LF规则核验，并在将来Linux环境重新实测守卫指纹。

Linux须复验59项专项、同步Responses路径、真实守卫对账、线程隔离、POSIX0600/文件身份/锁/容量/符号链接安全和已知F3.1失败。完整真实模型行为、费用与未来成本效果尚未验证，不报告节省比例。本轮只具备申请Linux离线验收条件，不是正式研究或部署授权。

不修改旧window账本/报告/claim/T0；未连接服务器、创建研究窗口、启动服务、F2/F3、timer或交易。execution_enabled=False、allow_new_risk=False、simulation_admitted=False。
