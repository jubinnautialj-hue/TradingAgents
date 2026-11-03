# 首次日常研究失败后的最小诊断增强

本地状态：DIAGNOSTICS_READY_FOR_LINUX_ACCEPTANCE。仅离线开发与测试；不是新研究启动批准。

基线：37cdb710bf7cb6e4717215e9061d482118ca302b。继续当前分支，未创建分支、提交、推送或连接Vultr。

## 修改范围（7个文件）

1. tradingagents/guarded_research.py：在原有调用/HTTP计量路径中设置诊断关联上下文；旁路观察原生Responses转换函数，返回值和异常原样传递。区分SDK返回与用量对账。
2. tradingagents/llm_clients/openai_client.py：在原有流循环中记录首个chunk、正常流结束或错误，以及原有连接尝试序号；重试条件、次数和超时不变。
3. tradingagents/llm_clients/reliability.py：在原有错误分类完成后附加关联元数据；transport_http_status不参与分类，避免用HTTP 200推定流成功。
4. tradingagents/dataflows/vendors/okx_derivatives.py：仅在历史OI单位检查失败时补充结构摘要和审计事件；单位容差、解析映射、分页及DATA_UNAVAILABLE行为不变。
5. tradingagents/llm_clients/diagnostics.py：小型元数据构造及现有append_audit调用，不提供研究、网络或账本入口。
6. tests/test_research_diagnostics.py：27项离线专项。
7. docs/research_diagnostics.md：本验收说明。

Market的现有market_contract.py、Schema、MODEL_PARSED/TOOLNODE_ENTRY事件、工具错误恢复和历史折叠逻辑不改。audit_file.py、role_observer.py、Policy、TTL、F1/F2/F3及11角色均不改。

## 状态及安全审计

新事件kind=RESEARCH_DIAGNOSTIC、version=research-diagnostics-v1，包含at_ms和明确阶段：

- HTTP_RESPONSE/HTTP_ERROR：原生call_id、wire_id、transport_http_status、request_id_sha256。每次发送前清除旧HTTP状态，避免连接失败误用上一次响应状态。
- STREAM_FIRST_CHUNK、STREAM_END、STREAM_ERROR：chunk_received、stream_completed、attempt。收到chunk不等同于收到有效正文；completed也不等同于SDK解析及Token已确认。
- SDK_PARSE_ERROR、SDK_RESULT：分别表示事件转换失败、原生_generate_with_cache返回。SDK_RESULT不替代流完成证明。
- USAGE_CONFIRMED：原生对账结果；False仍沿原有路径暂停。没有用该事件替代原生Usage/账本。
- CALL_ERROR：异常类别链（最多12层、静态类别名单）及既有错误码白名单，不记录异常文本。
- RAW_TOOL_ARGUMENTS：下述工具参数结构。
- OI_UNIT_CHECK：下述单位失败结构。

诊断仅复用TA_SINGLE_AUDIT_FILE和append_audit，4MiB、0600、永久侧锁、身份绑定、符号链接拒绝不变。不设置独立日志、不创建额外锁协议、不吞掉追加失败、不重放。没有配置该审计环境变量时不创建文件。原生失败摘要仍可保存安全关联信息。

provider code/type仅使用reliability.safe_diagnostics既有有限名单。请求ID仅存SHA256。未保存请求头、URL查询、认证文件、提示词、响应正文、参数原始值或任意异常字符串。未保留的字段保持未知，不补造。

## 三类TEST_FIXTURE诊断样例

### Bull：HTTP 200后失败

同一call_id/wire_id可依次出现：

```json
{"stage":"HTTP_RESPONSE","transport_http_status":200}
{"stage":"STREAM_FIRST_CHUNK","chunk_received":true,"stream_completed":false,"attempt":0}
{"stage":"STREAM_ERROR","chunk_received":true,"stream_completed":false,"exception_types":["APIError"],"error":{"provider_code":"server_error"}}
{"stage":"CALL_ERROR"}
```

上例不是历史Bull原始响应。若没有最终用量，账本仍UNKNOWN_OR_FAILED_USAGE并PAUSED，不产生USAGE_CONFIRMED=true，不再发送下一调用。HTTP头状态和流内错误分别保留，不将200改写为推测的500。

### Market：原始空对象与转换丢失

在SDK已解码的response.output_item.done/function_call/get_indicators事件进入LangChain原生转换器前，记录call_id、tool_call_id和arguments结构。只观察最终done参数，不把尚未完成的delta误判为完整参数，不改SDK。

arguments.state包括EMPTY_OBJECT、OBJECT、EMPTY_STRING、MISSING、NON_OBJECT、INVALID_JSON、LIMIT_UNKNOWN。JSON观察上限65536字符；超限只记录UNKNOWN，不拒绝、截断或改写原工具调用。

空对象的结构SHA256为d2d9ad1992f002827dfa86b32b8575fdac4d7d5a90dc506ff260987f6a0c0bb2，与现有MODEL_PARSED诊断算法一致。通过tool_call_id关联现有MODEL_PARSED.call_id，比较字段存在位图与结构哈希：

只有调用ID可唯一对应时才归因；ID重复、缺失或记录不完整时关联结果必须为UNKNOWN，不能据正文或长度强行配对。

- 原始SDK事件为空、解析后为空：证据表明空参数已存在于转换器之前，不能归因于ToolNode。
- 原始SDK事件字段存在、解析后为空：可以定位到该事件边界之后的转换/聚合区间，但不自动补参。
- 没有done事件、JSON无效或过大：不能证明模型输出为空，保持UNKNOWN。

该边界不是网络原始字节，也不能证明SDK解码之前没有丢失。既往缺失的原始参数不能追溯补全。

### OI：单位一致性失败

```json
{"stage":"OI_UNIT_CHECK","units":{"stage":"CONTRACT_BASE_COMPARISON","present":{"oi":true,"oiCcy":true,"oiUsd":true,"ts":true},"contracts_unit":"CONTRACTS","base_unit":"BTC","contracts_range":"ONE_TO_MILLION","base_range":"ONE_TO_MILLION","expected_base_range":"ONE_TO_MILLION","multiplier_range":"LT_ONE","matches":false}}
```

仅记录ZERO/LT_ONE/ONE_TO_MILLION/ABOVE_MILLION区间、有限币种单位及字段存在性，不记录失败数据行。冻结快照诊断仍保留PARSE_ROWS、页号、时间范围、CONTRACT_UNITS和位置。只有当原有单位检查失败才生成该摘要；合法计算结果保持一致。

摘要能定位检查阶段，不能仅凭粗粒度范围推定供应商字段定义、舍入或历史合约规格的根因；后续如需计算修复必须另行核验契约，不放宽容差。

## 实际验证

- 新增专项最终27项；最终受影响复测139通过（包含上述专项、guarded_research、流可靠性、认证白名单）。真实MockTransport与原生Session拦截，不发送网络请求。
- 扩大相关回归548通过、2项既有F3.1失败、6项Windows平台跳过，另8个subtests通过。随后追加最后3项专项并复测最终受影响范围；不将重叠测试相加。
- 两项既有失败原样保留：test_init_is_opt_in_and_no_production_activation_of_uncommitted_code的Reviewed DID NOT RAISE；test_no_core_or_legacy_protocol_bytes_modified对b01436cc旧源码比较失败。该测试文件和被比较的F1/F2/F3核心文件未改。不是项目全量零失败。
- 脱敏测试注入SECRET_PROMPT、响应正文、认证头、请求ID、未知provider type、无效参数和OI原始数值，审计中未出现这些值。允许名单provider code与Retry-After保留；异常文本不保留。
- 合法FixtureModel结果、SDK用量、HTTP计数与原逻辑一致；未知用量、部分响应、SDK解析失败及审计容量失败仍拒绝后续调用并暂停。实际Responses转换器前后返回值及输入对象保持一致。
- Windows真实Market/observer/新事件共写90条记录，统一append_audit及侧锁。
- 本地WSL非root UID65534：实际新诊断模块与原生append_audit，4进程共写240条，JSON完整，审计及侧锁0600；0644拒绝；4进程在满4194304字节时全部CAPACITY，无扩容或覆盖。
- WSL原19项锁测试通过；6项混合写入OS测试通过，另FrameworkObserverTests因该标准库Linux解释器缺框架依赖而类级跳过。框架集成由Windows锁定环境验证，不能冒充Vultr验收。

本地证据目录名research_diagnostics_local_20261103，包含regression.txt、final-affected.txt、linux-locks.txt、linux-mixed.txt、fingerprints.json及TEST_FIXTURE_examples.jsonl。不将运行日志纳入Git。

## 指纹与Linux后续验收

实现指纹算法和四个输入文件清单不变；其中仅guarded_research.py内容改变。

- 基线Git blob/LF指纹：2a8fa2760267b68c66ca6797c7d6a0b2afa00b8c242406c6c2f64dc4437f5cf6。
- 当前Windows实际字节指纹：4c5bb55316405ced2bf12c13a61bf4e0c8c4088b455edca376a228d703d72007。
- 当前源码LF候选计算值：8ce20b51c02cba3679a43bee3c9e7d3b9d523d7b1cf14f96fdbc46e04c53a383，仅为离线预览，不宣称已经实测Linux。

新辅助模块不在原四文件算法输入内，不能把预算守卫指纹当作完整研究代码指纹。后续必须绑定新固定Commit并核验所有新增/修改文件Git blob和实际字节，实测Linux守卫值，检查SDK转换器接口、审计实际导入路径与86项依赖。不得让新实现复用、迁移或解除旧失败账本。

后续隔离Linux验收仅使用独立fixture及新审计文件，完整运行新增专项、原审计锁/共写、预算/认证/Market/OI及F1相关回归，保留既有失败；不调用模型或业务HTTP、不创建正式预算或claim、不启动研究。需要后续独立授权，不自动执行。

window-001历史总账、T0、截止、PAUSED、claim及报告未修改。三个安全字段均False。未连接Vultr、未创建研究窗口、未启用timer/F2/F3或交易。完成后停止。
