# F2.1：前向观察资格与新增风险许可隔离

分支 codex/forward-validation-f2。Git基线仍为F1提交3bec3dfd0feebeafffd7f52a209c2f85d5c26a29；F2和本轮更改均未提交。
本轮只分离观察资格，不开发F3、不安装调度任务、不新增Agent，不修改任何风险许可、冻结策略或旧验收报告。

## 根因审查（修改前完成）

1. `agents/perpetual_schemas.py::PerpetualResearchResult` 中allow_new_risk和execution_enabled均为Literal[False]，不是LLM输出的方向判断。
2. `dataflows/perpetual_plan.py::finalize_result` 在结构、方向共振、费用、数量/价格精度、时效等校验通过后，可以生成plan_status=VALID_CANDIDATE、plan_valid=True，但仍无条件设置allow_new_risk=False。
3. `dataflows/okx_research.py::final_gate` 在交易预案模式再次把最终allow_new_risk设为False。行情bundle中的同名字段原先表示数据闸门是否可用，不能与最终预案的安全许可混为一谈。
4. `forward_validation.py::Registry.register` 原样复制最终结果的False；F1 simulation_pool_eligible的计算实际独立于该风险字段，另看研究完成、预案、质量、原始时窗、登记及时性与证据过期。
5. 原F2 `exclusion_reasons` 同时要求登记及original_result的allow_new_risk都为True，导致标准永续预案永远被挡在观察入口。

原生代码具备生成有效候选的路径，tests/test_perpetual_plan.py中的test_valid_independent_candidates及test_full_mock_agents_deliver_checked_candidate分别验证多空候选与原生模拟多Agent全流程。
这证明实际生产代码逻辑可产生有效候选，不等于当前真实模型或市场已经产生过有效候选。本轮未为取得指定方向重新运行昂贵研究，也未调参。

## 字段分层

| 字段 | 含义 | 本轮处理 |
|---|---|---|
| research_completed | 原workflow=COMPLETED且llm_status=AVAILABLE | 新观察元数据中确定性派生 |
| plan_valid | 原预案Schema与程序校验结论 | 原值与定义不变；必须True |
| allow_new_risk | 原最终研究的新增风险许可 | 保持False，不作为观察否决项，也不改成True |
| F1 simulation_pool_eligible | 原登记时质量/时窗预筛选，名字不等于已经模拟入场 | 保留原值，并保守保留其质量/时间限制 |
| research_observation_qualified | 冻结研究是否满足新观察协议 | 与当前是否仍在观察窗口分开 |
| observation_eligible | 本次可否进入仅观察流程 | 通过静态校验且假设未到期才True |
| simulation_admitted | 是否已获独立模拟交易准入 | 本轮固定False，不开发准入/结算 |
| execution_enabled | 真实执行能力 | 所有新结构固定False |

`observation_eligible=True`不是交易信号、模拟成交或风险授权。仅允许读取登记后合法已收盘K线并复算C.1的技术条件。

## 版本化观察协议

协议forward-observation-v1；机器定义docs/forward_observation_v1.json，数据结构docs/forward_f21.schema.json。
F2实现版本forward-f2.1-v1；条件证据格式forward-observation-condition-v1。

`observation_eligibility.assess()`必须在调用方使用Registry.load校验原始封存之后运行，并要求：

- 来源是PROSPECTIVE的F1 v1；模型和workflow完成；动作明确为OPEN_LONG/SHORT_CANDIDATE。
- 原plan_valid=True，完整复用PerpetualResearchResult.model_validate，不改变其定义，不允许仅凭BUY/SELL方向准入。
- 研究ID、合约、父/子快照、UTC cutoff、价格来源及原登记一致。原价位可引用经过验证的4h子快照，不能错误要求全部价位引用父快照。
- 核心及1h/4h/1d原数据质量有效、指纹正确、时间连续且没有未来柱。源快照或预案缺失时拒绝。
- 保留F1 pool/status的原质量与时间门槛；登记不得晚于首根允许确认柱，确认窗口、到期和原C.1规则一致。
- 研究/登记时已经STALE_DATA或RESEARCH_INVALID的证据不豁免；F1 pool=False不自动重判为可观察。
- 原风险字段允许保持False；登记与original_result两处字段必须一致。不会把字段不一致解释为默认值。
- 没有可靠cutoff、模型失败、WAIT、plan_valid=False、历史导入或TEST_REPLAY始终拒绝。

研究时有效、随后变旧的历史证据不冒充当前证据。本轮不再次渲染旧快照为“当前新鲜”，不延长TTL，不获取现在的数据替换原研究依据。
只要原研究/登记合格、C.1假设仍在期内，可以等待未来柱；真正入场仍必须重新检查新行情、流动性、成本及原有风险门槛。
当前到期时observation_eligible=False。新过期样本不补建历史跟踪；已经在本协议下观察的链只允许写入原C.1 EXPIRED终态，不重新激活。

## 调用链

F2 scan → Registry.load/events → assess(冻结研究) → EligibilityAudit.record(独立元数据) → candidate_from_record(重新复核判断) → 原process_candidate → 原evaluate_signal。
传入一个伪造的observation_eligible=True对象不能覆盖原不合格预案；candidate_from_record会重新计算并拒绝事实不一致。
旧的公开函数名exclusion_reasons保留，转为返回新观察协议的否决原因；CLI用法保持不变。

## 独立审计与兼容

新审计位于F2 output/observation_eligibility：

- <policy_hash>.policy.json：冻结观察协议，不写F1协议目录。
- <registration_id>.events.json：资格评估事件，关联原登记hash、研究/快照ID、policy版本/hash，分别记录plan_valid、原allow_new_risk、observation_eligible和simulation_admitted。
- event_id按原登记/策略/决定事实派生，重复同一决定幂等；窗口或决定变化追加事件，保留旧事件和记录时间。原始登记不变。

F1 v1以只读方式兼容：新增资格判断不是回填研究结论，也不是把旧研究重新登记为当前研究；历史导入及测试回放不因此升级。
原F2 v1跟踪链或代码/策略不匹配的已有链保留只读、明确暂停，返回LEGACY_TRACKING_CHAIN_PAUSED，不自动迁移或重算其旧事件。
本轮实际生产F2没有旧跟踪事件，因而不需要迁移；旧链暂停规则有隔离夹具验证。任何今后迁移必须另行保留旧链并显式审核。
旧F2说明、协议及验收报告仍描述当时事实，未被本轮覆写；当前规则以本说明和新版本为准。

## 不授权F3

ConditionTrigger输出为新版本forward-observation-condition-v1，并强制：

- observation_only=True。
- simulation_admitted=False。
- requires_separate_simulation_admission=True。
- allow_new_risk=False、execution_enabled=False、is_simulated_fill=False。
- requires_revalidation=True。

该结果是观察证据，不是允许承担风险的交易样本。后续F3必须有独立的模拟准入设计；本轮没有新增这套机制。
C.1 next_tradable_ms和实际decision_available_ms仍分开保存，不能倒签到已经过去的开盘价。
F2原有缺柱暂停、晚采/BACKFILL、未来柱拒绝、幂等、锁及原子恢复规则保持；补采或FIXTURE不能导出生产条件证据。

## 测试和真实复核

最终90项专项/F1/F2兼容测试通过：包括有效候选且风险False的观察准入、模拟标准F1登记至突破/回踩/重启/条件读取/到期，以及WAIT/失败/无效/过期/历史/夹具隔离、旧链暂停和指纹校验。
正例使用pytest临时目录中的模拟标准F1档案和确定性行情，没有写入正式研究目录；明确TEST_REPLAY来源仍然被拒绝。
完整原生模拟多空研究能力由原有tests/test_perpetual_plan.py正例随全量回归验证，没有修改冻结策略。
全量回归1962 passed、5 failed、4 skipped、13 deselected、100 subtests passed（191.67秒）；随后补充两项缺截止时间/伪造资格防御用例，最终90项检查覆盖收尾变更。
五项仍为已披露的Windows权限/升级命令/yfinance缓存隔离问题，不声称全量全绿。

实际扫描只读取正式F1档案并访问一次OKX公开时钟，不调用LLM。现有档案仍是一个正常WAIT和一个RESEARCH_FAILED，两者没有因风险字段不再否决而变合格。
新增观察审计明确分别保留False风险许可及False观察资格；正式合格候选0、真实条件触发0、模拟成交0。
因此：**入口阻塞在代码及隔离模拟链路上已消除；真实有效候选的生产端到端观察仍未实证验证。**
不为验收制造真实信号、导入旧回测交易或重跑模型直到得到指定结果。

## 使用与文件

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.forward_tracking scan --registry 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f1' --output 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f2' --max-seconds 120 --max-requests 4
.\.venv\Scripts\python.exe -m pytest tests/test_observation_eligibility.py tests/test_forward_tracking.py tests/test_forward_validation.py -q
```

修改：tradingagents/forward_tracking.py、tests/test_forward_tracking.py（只更新被本轮新语义替代的风险False必拒绝断言；原生命周期/恢复/前视等测试继续保留）。
新增：tradingagents/observation_eligibility.py、tests/test_observation_eligibility.py、docs/forward_observation_v1.json、docs/forward_f21.schema.json、本说明及docs/forward_f21_acceptance.json。
未修改F1源代码、原协议和封存记录、C.1、A—D策略/回测、原验收报告和AGENTS.md；没有commit/push、调度安装或F3开发。

建议有条件通过本轮专项工程验收。条件是保留没有真实有效候选、原F1质量/时间限制仍生效、旧F2链不可静默迁移、全量五项既有失败及本地哈希非外部签名的限制。
观察入口具备接收未来合法有效候选的代码条件；不等于已经证明前向收益、交易能力或持续数据服务可靠性。
