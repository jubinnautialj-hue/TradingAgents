# Market流事件兼容核对 v1

基线：`d59d82a9b04d5657e5a8d0c8c78c2abc06233f45`。仅本地实现和离线验收；不授权部署、模型调用或新Probe。

## 支持回复的适用边界

依据用户转述的OpenAI支持答复，完整`output_item.done`和`function_call_arguments.done`等流事件可以累积并核对。没有取得回复原文、工单及适用服务版本，不能宣称已独立核实其全部条件，也不能据此宣布所有显式空终态都合法、前序事件总能覆盖终态，或Probe历史故障已归责于服务端/SDK。

公开函数调用指南支持按身份关联流事件形成调用信息，但不代替应用准入：
https://developers.openai.com/api/docs/guides/function-calling

本实现是用户明确授权的、条件严格受限的应用层兼容路径；保留原始SDK响应，不把合成投影冒充服务端终态。

## 最小实现

唯一生产改动为`tradingagents/agents/analysts/market_safe_adapter.py`。不修改SDK、模型客户端、提示词、Market Schema、普通研究入口、Policy、四个预算实现指纹输入文件或其他10个角色。

- 非空`response.output`仍走原有终态工具核验，不使用事件投影覆盖它。终态只有reasoning/message也不能触发空终态兼容。
- output缺失、None或非list显式拒绝，不把它们转换成空数组。
- 没有任何工具事件的正常空终态仍可以返回零工具调用，不制造工具请求。
- 只有**显式空list且有工具事件**，才尝试版本`market-stream-reconciliation-v1`的完成事件核对。

该路径必须同时满足：

1. 唯一`response.created`、唯一`response.completed`、响应ID一致，完整迭代到EOF且无流异常。所有观察到的事件sequence_number都是非负int且严格递增；允许间隔，不补序号。
2. 没有未登记OTHER事件；显式response_id如存在必须与本次响应一致。原有failed/incomplete/error/重复完成及完成后事件拒绝仍有效。
3. added、output_item.done、arguments.done、阶段记录与slot身份集合完全匹配；无孤立delta。索引唯一且从0连续，没有遗漏slot或无法解释的非工具item。
4. 每个item只出现一次added，之后零或多个delta，然后一次arguments.done、一次output_item.done；不接受空终态路径的delta先于added或在done后追加。done状态必须为completed。
5. 只从done复制其实际item_id、call_id、工具名、最终参数和状态。随后复用同一原生准入循环：added/done身份一致、调用ID唯一、索引一致、最终JSON与arguments.done一致、initial+delta与最终JSON一致、原Market Schema通过。不会补参、默认日期、猜JSON或使用LangChain半成品参数。
6. SDK原始/归一化Token完整一致；原生Session计量与UNKNOWN暂停保持不变。所有工具通过且审计写入成功后，才把调用集合交回原生工作流；本模块本身不执行工具。

完整终态原有的精确身份关联、包括先收到delta再收到added的既有处理保持不变；新增顺序约束仅用于缺少终态工具快照的严格替代证明。完整快照没有被改成更宽松的事件路径。

兼容路径保守限定为工具item；空终态含无法解释的reasoning/message slot、索引缺口或未知事件时拒绝，不增加新的多类型聚合器。

## 审计

原始终态结构及其差集不改写，仍可看到`EMPTY_ARRAY`、terminal=0。新增`reconciliation`仅保存版本、来源`COMPLETED_EVENTS`、工具数量和CANDIDATE/VERIFIED状态。全部准入检查通过后才记录`EVENTS_RECONCILED`；失败不能伪装成完成的资格证明。

沿用0600、永久侧锁、4MiB容量及失败传播。不记录参数、提示词、正文或未清理异常。原始HTTP/Token账本、claim和业务档案均不改写。

## 历史证据边界

Probe-001/002脱敏摘要只能证明事件种类、数量及关联哈希，不能恢复参数、序号或完整wire。测试使用独立合成参数与事件，仅复现“1个item、21条delta、完整done、显式空终态”的形状。

历史摘要还含1条OTHER事件，其原始类型未知。包含这一未知事件的合成对照**必须拒绝**。不能称历史Probe已忠实重放、不能保证同一真实流会通过，也不得猜测OTHER是无害事件。序号和done状态在历史摘要中也未完整记录。

## 离线验证范围

`tests/test_market_stream_reconciliation.py`覆盖单项、6/7项并行交错/分片、显式空/非空、股票数据工具、Schema、重复/冲突/缺失/中断、序号、跨响应身份、未知事件、未迭代完、UNKNOWN暂停、审计失败与容量拒绝，以及同一冻结快照下确定性指标输出字节一致。

原`test_original_matching_and_admission_body_are_unchanged`是诊断阶段的AST不变测试，本轮有意新增输出证明选择器，因此改为`test_shared_admission_validators_are_unchanged`：仅归一化新的输出选择器及诊断状态，其余原生身份/参数/Schema/集合验证AST仍必须与旧版本一致。没有删除既有安全断言。

所有测试只使用Fixture、MockTransport和冻结行情，禁止真实socket连接。F3.1的Reviewed和历史协议字节比较既有失败必须继续记录；不宣称全量零失败。

## 剩余限制

此版本仅证明所列约束下的离线事件核对与安全拒绝；Linux POSIX、实际服务环境及在线兼容均须独立验收/授权。不得在当前历史失败账本上补跑，也不引导模型生成OPEN。

## 本地验收（2026-11-04）

- 新增专项58项通过，包含在联合结果中，不重复相加。
- 最终联合：421通过、7项既有Windows/POSIX平台跳过、8个子测试通过，退出码0，167.34秒。使用`-W error::pytest.PytestUnraisableExceptionWarning`，无告警，未过滤告警。
- F3.1另测46通过、2项既有失败：Reviewed未抛ValueError、b01436cc历史协议字节比较不同；退出码1，122.37秒。未修改/跳过这两项，不宣称全量零失败。
- 原始准入验证AST（除有意新增的输出证明选择器及审计字段）保持一致；原流异常及审计失败路径AST保持一致。正常非空输出保留原协议；空终态核对后的调用与非空对照一致。
- 首轮指标字节测试缺少OKX运行配置，已只修正Fixture以使用同一冻结快照和原生配置。初版容量Fixture先触发SDK首流审计而非准入审计，并暴露既有生成器清理二次诊断告警；未改生产异常处理。现将此新Fixture准确定位到完整流后的准入追加，最终以告警视作错误重测通过；没有过滤告警或弱化容量拒绝。
- 没有SDK/锁文件/Policy/预算/TTL/F1/F2/F3源码变动；没有真实模型、业务HTTP、部署、账本或授权操作。三个安全字段仍False。

结论：`MARKET_STREAM_RECONCILIATION_READY_FOR_LINUX_ACCEPTANCE`，仅指本地受限实现可申请Linux离线验收；不等于历史Probe忠实重放成功或已具备在线放行资格。
