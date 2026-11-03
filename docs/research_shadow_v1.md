# 零模型预筛Shadow v1：离线影子实现

基线4cb1870a44c6858d7d786c674e8fd07c4206d477，继续codex/f1-revalidation-admission-oi-diagnostics；没有新分支、Commit、Push或部署。

## 最小范围

新增三文件：tradingagents/research_shadow.py、tests/test_research_shadow.py、本文。所有旧生产源码、配置、Policy、预算指纹算法/输入文件、TTL、F1/F2/F3和11角色流程完全不改。

模块没有CLI/__main__、网络客户端实例或研究入口连接；不导入guarded_research/auto_research，不创建研究总账或审批。evaluate_shadow是纯函数，record_shadow是唯一可选写入函数，仅允许临时目录内market-shadow-FIXTURE-* / market-shadow-REPLAY-*独立审计目录。

## 规则和四类枚举

规则版本research-preshadow-v1，规则指纹23e81649fb2417288e0572326b429f9464cbe4669a861afa01c38c738ee7c5a7。规则是只读映射，运行时参数漂移返回UNKNOWN，不允许把300秒改大后沿用原指纹。

|输出|v1定义|
|---|---|
|RESEARCH_SUGGESTED|声明身份、原生快照指纹、核心K线质量、点时可用性、确定性指标复算和绑定衍生品检查全部通过。只建议人工评审是否研究，不是完整研究资格或信号。|
|DEFER_SUGGESTED|枚举保留，但v1没有被验证的安全市场性延后条件，从不产生该输出，审计接口拒绝手工伪造的DEFER。不得为了显示节省率启用。|
|DATA_BLOCKED|证据明确STALE/RESEARCH_INVALID、原生K线校验拒绝、覆盖/顺序错误或已需要更新已收盘K线等，阻止的是影子建议，不会拦截真实研究。|
|UNKNOWN|未知/缺失/不完整质量、身份或指纹不符、未来数据/接收时间、指标摘要漂移、格式错误或规则漂移。不是“无机会”。|

日线MIXED、周期冲突、横盘、没有突破回踩均不构成DEFER。重复完全相同输入得到完全相同结果，不把重复输入当“新机会”或延后依据。

## 证据及不使用未来信息

输入只接受原生multitimeframe_snapshot形态：BTC-USDT-SWAP、1h/4h/UTC日线、原始标准化OHLCV、原生确定性摘要及绑定衍生品。根字段白名单拒绝研究报告；任意层级的research_result、final_trade_decision、wait_case、模型提案、入场/止损/止盈字段均拒绝。原始文本和异常正文不写审计。

调用者必须显式提供as_of_ms、expected_snapshot_id、expected_cutoff_ms和FIXTURE/REPLAY模式；身份来自可信冻结清单，不能把快照自称的ID当作来源真实性签名。无PRODUCTION模式。

- cutoff约束市场观测：每条K线必须已经收盘，UTC时间和Date一致，严格连续，无未来K线。OI等观测ts/as_of必须不晚于冻结cutoff。
- as_of约束客户端知识：server/所有fetched_at必须不晚于影子时点。不能把后来收到的数据倒签到cutoff使用。
- 复用okx.validate_instrument、fingerprint、validate_candles、mtf.analyze_timeframe/confluence及deriv.evidence_view(now=as_of)，不修改全局时钟、不取实时数据、不把模型摘要代替计算。
- 原始250—400根多周期记录和确定性指标重新校验，摘要只取哈希，不输出价格、入场、方向或候选。原始快照不被修改。
- Shadow证据年龄上限300秒，实时衍生品自身expiry同样必须有效；不延长任何原生TTL。即使未到300秒，只要跨收盘边界已需要新闭合柱，仍DATA_BLOCKED，沿用原生max_lag=0语义。
- 历史OI等按原来的采样时段检查，不错误要求每个历史数据点都在300秒内；但它们的客户端接收必须在as_of之前且新鲜。
- 未来资金结算/expiry是已经公开的计划时刻，不被伪称为已实现观测；不据此预测收益或产生交易判断。

该范围不包含独立新闻/宏观/订单流等完整研究证据，也不复刻原生F1准入；即使RESEARCH_SUGGESTED，正式研究仍须重新采集并经过全部原生闸门。

## 输出与独立审计

每条记录含version、rule_fingerprint、mode、as_of、cutoff、snapshot_id、全输入规范化证据指纹、静态reason_codes、六阶段PASS/FAIL/NOT_CHECKED、各周期覆盖与收到时间、确定性特征哈希。没有LONG/SHORT、交易价格、F1候选或模型正文。

shadow_only=True、research_authorized=False、model_calls=0、http_calls=0，三个最终安全字段恒False。

record_shadow只在系统临时目录内显式命名的私有测试目录保存shadow.jsonl，拒绝符号链接/真实路径重叠及普通业务目录。复用现有append_audit，不新增锁机制：0600、永久侧锁、4MiB、身份绑定、失败明确传播、不删除/截断/重放。未接入TA_SINGLE_AUDIT_FILE或任何旧日志。

事件ID为完整结构化输出的规范化SHA256。重复观察可追加同ID，以保留调用事实；统计必须按event_id去重，不能按行数增加样本。哈希不等同签名或完整研究证据认证。

## 历史回放（不是前瞻样本）

仅从六份原始multitimeframe_snapshot.json读取，不以WAIT或最终模型结论作输入。as_of设为该快照所有已记录客户端接收时间的最大值：这是反事实离线可用性检查，不证明当年实际存在预筛进程，也不证明完整文件恰在这一毫秒已经持久化。

|源|输出|直接理由|
|---|---|---|
|trial-001/R1|RESEARCH_SUGGESTED|点时证据完整，无安全延后条件|
|trial-001/R2|RESEARCH_SUGGESTED|点时证据完整，无安全延后条件|
|trial-001/R3|UNKNOWN|衍生品证据不完整|
|trial-002/R1|UNKNOWN|衍生品证据不完整|
|trial-002/R2|RESEARCH_SUGGESTED|点时证据完整，无安全延后条件|
|trial-002/R3|RESEARCH_SUGGESTED|点时证据完整，无安全延后条件|

4次建议、2次未知、0次DEFER、0次DATA_BLOCKED。UNKNOWN不算延后或节省；不得从历史WAIT推出v1已避免无效研究。模型调用/业务HTTP均0，实际研究Token节省未实证。

独立本机日志目录research_shadow_v1_20261103保留historical-replay.json、原文件SHA256、事件ID、审计临时目录及测试日志；不提交原始研究数据。复验前后原文件字节不变。

## 前瞻对照设计（仅方案，未执行）

先封存规则与审计Schema，默认只影子记录、不阻止或触发真实研究。将SUGGESTED/UNKNOWN分别登记；v1没有DEFER组，无法估计延后漏检率。

若未来独立评审v2延后规则，必须在看结果前固定对照抽样方法/种子/时点，把拟DEFER组也纳入有概率被完整11角色研究的人工授权对照。对照同样遵守独立审批、原共享预算、24小时间隔与72小时窗口，不自动补跑。UNKNOWN不能默认为负样本；数据不足导致研究无法运行须另记，不计作WAIT或无机会。

比较原始模型OPEN/WAIT、F1准入、F2触发、F3净成本结算及逐调用Token，并按行情状态报告缺失/筛选偏差。禁止只验证SUGGESTED组、用事后价格收益挑选回放时点或改写历史WAIT。6次WAIT且0自然OPEN不能估计召回率或证明不会漏检。

停止条件：身份/时间漂移、未来信息、未知质量当成无机会、自动触发付费调用、审计失败或需要增加方向性硬门槛，停止接入评审，不绕过、不补跑。本轮没有新增研究或自动任务。


## 本地测试结果与验收边界

- 最新Shadow专项46通过、1项Windows/POSIX符号链接平台跳过。覆盖未来柱/OI、客户端收到时间、跨周期身份、Date漂移、指标复算漂移、研究结论混入、300秒边界、新闭合柱边界、规则漂移、审计字段伪造及重复事件ID。
- 最终直接相关回归165通过、6项POSIX平台跳过，另8个参数化子检查通过。包含最新Shadow、原多周期/衍生品、审计锁/观察器、F1重验证明及F2.1资格。
- 扩大相关回归486通过、2项既有F3.1失败、7项平台跳过，另8个参数化子检查通过。覆盖Market/Graph/CLI/预算/F1/F2.1及F3.1；该轮在最后新增Shadow边界用例前执行，之后已复测上述最新专项和直接范围。未宣称全量无失败。
- 既有两项失败仍为Reviewed DID NOT RAISE与旧b01436cc源码字节比较；未修改、跳过或隐藏。新Shadow的POSIX符号链接用例没有在本轮Windows执行，不冒称已完成Linux集成验收。
- 首轮专项的11项失败来自测试配置未启用衍生品，后续仅修正新增测试fixture配置并通过；原始日志保留，不把未执行检查伪装成功。
- 六份历史快照均核对原封存清单SHA256；审计在新market-shadow-REPLAY-*临时目录，共12条：6个主回放和6个同快照十分钟后过期控制。后者全部DATA_BLOCKED，不是新增市场样本。0模型、0业务HTTP，原输入文件字节不变。

状态READY_FOR_SHADOW_REVIEW（仅本地离线评审），不是生产自动筛选、研究或F1准入授权。未部署Vultr、未创建新研究窗口/总账/授权/调度；停止等待验收。
