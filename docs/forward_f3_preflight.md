# F3 开发前兼容性审查：成交时钟阻塞

审查日期：2026-10-24（Asia/Shanghai）。结论：**F3 未通过，尚未实现**。
这是开发前检查和兼容设计，不是 F3 工程验收或已生效的模拟协议。

基线 `872a8c8ffdc40a01e3ad712fd3ead32cd7e7c0cd`；开发分支
`codex/forward-validation-f3`。基线工作区仅有未跟踪 AGENTS.md。
本轮未修改任何源码、冻结规则、F1 登记或 F2 事件，没有提交、推送或安装服务。
execution_enabled、原 allow_new_risk、simulation_admitted 均未改变。

## 1. 已完成的复用检查

| 组件 | 可复用能力 | F3 适配边界 |
|---|---|---|
| forward_validation.Registry.load/registrations | 验证登记及复制报告的指纹，读取正式样本 | 保持原档案只读 |
| forward_tracking.EventStore.load/triggers | 验证观察链、重放 C.1、导出连续及时观察条件 | ENTRY_ELIGIBLE 不是成交 |
| observation_eligibility.assess | 独立观察资格，不提升风险许可 | 不是纸面评价准入 |
| plan_lifecycle.evaluate_signal | 冻结突破、回踩、失效和到期 | next_tradable_ms 不可改写 |
| plan_lifecycle.entry_evidence_ready | 信号后新快照、漂移及 TTL 重新验证 | F2 没有提供已完成的入场再验证结果 |
| okx_backtest.simulate / SimulationConfig | 张数、保守滑点、双边 Taker 费、止损优先、资金成本与权益 | 离线批量回放，不能直接冒充实时可执行的前向成交 |
| okx_derivatives.multiplier/funding_cashflow | 合约换算、方向和资金现金流 | 实际结算事件和价格依据仍须单独验证 |
| memory.okx_simulation.SimulationFeedback | 结果可知时间、隔离和追溯设计 | 当前固定 OFFLINE_SIMULATION，不能直接当作真实前向反馈 |

simulation_admitted 在 FrozenCandidate、TrackingEvent、ConditionTrigger 中均为
Literal[False]。独立引入 paper_evaluation_eligible 不必修改它，因此权限字段
本身不是当前不可兼容之处。

## 2. P0：确认事件晚于冻结的成交开盘

代码证据：

- plan_lifecycle.evaluate_signal：回踩确认的已收盘柱 close_ms 同时作为
  retest_confirmed_ms 和 next_tradable_ms。
- forward_tracking.ConditionTrigger.clock：强制 next_tradable_ms ==
  signal_confirmed_ms，且 signal_confirmed_ms <= observed_available_ms <=
  decision_available_ms。
- forward_tracking.process_candidate：先 Collector.get，之后将 recorded 更新为
  max(recorded, fetched, 当前时间)，最后才封存条件事件。
- okx_backtest.simulate：看到 ENTRY_ELIGIBLE 后，使用当前下一柱的 open 加不利
  滑点成交，没有“晚获取事件后再推迟一柱”的既有规则。
- docs/forward_f2.md 的 F3 只读契约明确禁止倒签到已经过去的开盘价。

独立内存夹具已实际复算（未登记为正式样本）：

| 时间字段 | UTC epoch ms |
|---|---:|
| C.1 确认时刻 / 下一柱开盘 | 1792681200000 |
| 条件行情实际可用 | 1792681201000 |
| F2 决策实际可用 | 1792681201100 |

C.1 返回 ENTRY_ELIGIBLE，F2 ConditionTrigger 校验通过，但套用原 D 开盘价格会
倒签 1100 ms。例子只证明该延迟条件合法且不能直接成交，不代表所有事件都恰好
延迟 1100 ms，也不凭此声称所有生产事件的延迟已被实测。

硬性要求是 fill_ms >= decision_available_ms；原 D 的成交规则是
fill_ms = next_tradable_ms = signal_confirmed_ms。
当 decision_available_ms > signal_confirmed_ms 时二者不能同时满足。
REST 采集已收盘行情通常会产生正延迟；不能把延迟归零、采用宽限时间倒签，或
忽略实际可用时间来绕过此冲突。

## 3. P0：入场再验证与历史模拟豁免尚未有独立前向契约

F2 ConditionTrigger 强制 requires_revalidation=True。C.1 的
entry_evidence_ready 要求信号确认后形成的新验证、完整当前行情、原快照漂移
检查和原有 TTL。F2 仅提供条件观察，未声称已完成这些检查。

C.1 HistoricalAssumptions 允许明确成本假设的历史 K 线模拟，但其适用输入是
经过验证的 historical bundle。正式前向档案不能被改标为 historical 以取得
历史模拟豁免。直接复用 D.simulate 不会自动满足前向入场再验证要求。

## 4. 兼容设计（草案，未激活）

建议协议标识预留 forward-paper-v1，须先确定成交时间语义再正式冻结。
独立保存 paper_evaluation_eligible、paper_protocol_hash、准入原因、F1/F2
原始指纹、入场再验证来源/时刻/TTL、实际数据可用时间、纸面样本分组。
allow_new_risk 和 simulation_admitted 原值始终保留；execution_enabled=false。

准入仍必须要求：正式 PROSPECTIVE、研究成功、原 plan_valid、原观察资格、
已验证且连续及时的 F2 条件、冻结几何及证据、无未来泄漏、未到期，以及独立
纸面评价协议明确规定的入场再验证结果。WAIT、失败、历史导入、夹具不得准入。

当前可以安全采用的严格处理是：若事件晚于原开盘，输出
MISSED_FROZEN_ENTRY_BOUNDARY，不成交、不结算，不改为下一小时自动入场。
该处理可保留观察统计，但不能据此声称实现了有效的生产成交链路。

如希望产生具有时间可行性的前向纸面成交，需要另行明确批准一种独立协议：

1. 允许在确认和再验证完成后第一个尚未发生的整点开盘成交；这会改变相对于
   现有 D 的入场等待时长和价格，必须记录为独立前向执行假设，不能声称与冻结
   D 成交规则完全相同。
2. 使用确认和再验证后实际采集的公开成交/报价作为纸面参考；这改变了纯 K 线
   开盘成交模型，还需采集与验证新的时间证据，不能直接复用事后 K 线 open。

本轮没有采用任何一种新规则，没有建立账本、模拟成交、成本结算、绩效系统或
自动 Memory/Reflection 写入。不存在可运行的 F3 命令或正式 F3 Schema。
未来应在独立账本中采用原子追加事件、跨进程锁、候选/成交/结算唯一键，且按
结果实际可知时间输出只读反馈，不能直接写入旧 OFFLINE_SIMULATION 来源空间。

## 5. 实际验证与样本

命令（无交易接口、无 LLM）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_forward_validation.py tests/test_forward_tracking.py tests/test_observation_eligibility.py tests/test_plan_lifecycle.py tests/test_okx_backtest.py -q --tb=short
```

结果：144 passed，96.22 秒。验证的是现有 F1/F2/F2.1/C.1/D 兼容行为，
不是 F3 撮合验收。本轮未修改产品源码，未重复执行全部项目测试；既有全量回归
中的已披露问题不能据此标记已解决。

正式 F1 只读核验：2 份登记，1 份 COMPLETED/WAIT，1 份 FAILED（其原 action
也为 WAIT，统计时应优先归类研究失败）；均 plan_valid=false、风险许可=false。
Registry.load 已核验封存登记与复制报告指纹。没有正式有效候选。

OKX 官方公开 GET 验证：2 次逻辑请求（服务器时间 + 历史 K 线），2 根已收盘
BTC-USDT-SWAP 1h 柱质量通过。没有将探测 K 线绑定研究或制造成交。
独立运行证据：
`C:\Users\jerry_su\.tradingagents\logs\forward_f3_preflight_20261024.json`。
证据文件不属于提交范围，不包含登录凭据或请求头。

真实前向成交数=0，真实前向结算数=0；胜率、Profit Factor、最大回撤等 F3
指标尚未计算，不能写成盈利为零的已验收策略。没有配对前向绩效，也没有训练
或 Reflection 的有效结果标签。

## 6. 验收结论和恢复条件

**F3 未通过：开发前兼容性检查完成，成交时钟及再验证契约阻塞尚未消除。**
遵照需求中“协议存在无法安全解决的冲突时先输出兼容设计和阻塞说明”的要求，
停止成交/结算功能开发。需要先明确是否接受独立的前向入场时序假设；在此之前
不能把新账本或夹具通过描述为 F1—F3 全链路已经投入使用。

后续资金结算仍须沿用实际已结算费率与事件覆盖验证，记录 mark 或交易价代理、
数量规格及费用假设。不能按固定八小时硬补资金事件，也不验证杠杆或强平安全。
