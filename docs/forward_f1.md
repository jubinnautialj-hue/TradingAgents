# TradingAgents F1 前向验证协议与研究登记

本轮基线：`c16266672928c44b7769d8ac5f8b71599cfa1fbd`；开发分支 `codex/forward-validation-f1`。
协议版本 `forward-f1-v1`，机器配置为 `forward_f1_protocol.json`，结构定义为 `forward_f1.schema.json`。
F1 只登记与封存，不抓取未来行情、不生成成交、不结算、不新增 Agent，execution_enabled 始终为 false。

## 样本与统计单位

保留 PerpetualResearchResult 的原始 proposed_action、model_proposed_action、plan_status、plan_valid、signal_state、allow_new_risk 和原文判断，不把 SELL 映射为开空。
OPEN_LONG_CANDIDATE、OPEN_SHORT_CANDIDATE、WAIT、REDUCE_LONG、REDUCE_SHORT、EXIT_CANDIDATE 各自登记。
`sample_category()` 优先区分 RESEARCH_FAILED、DATA_QUALITY_INSUFFICIENT、RESEARCH_NOT_COMPLETED、PLAN_INVALID，再区分研究动作。
因此模型失败报告即便原 Schema 的 proposed_action=WAIT，也不计为一次完成的 WAIT 判断，更不计为成功避险。
数据有效、研究流程完成、预案有效、模拟池资格、实际触发及模拟成交是不同概念。F1 不生成后两类标签。

`origin` 分为 PROSPECTIVE、HISTORICAL_IMPORT、LATE_REGISTRATION、TEST_REPLAY。
PROSPECTIVE 表示有本轮研究开始前创建的意向，并及时封存；它也可以是失败研究，不能因此进入候选池。
历史导入、测试回放和晚登记不得纳入正式前向绩效。数据检查模式 `--data-only` 不能搭配正式登记开关。

## 时间规则

内部使用 UTC epoch 毫秒；界面/复核可用显式 UTC/+08:00 转换。不得用本地无时区字符串推算 cutoff。

- started_ms：Registry.begin() 在调用研究前读取本地时钟并写入意向，调用者不能传入过去的开始时间。
- cutoff_ms：原 OKX 核验快照的统一研究截止时间；不得为了登记或 TTL 延长而移动。
- completed_ms：原生报告再次执行 final_gate 并完成写出后，由 CLI 读取完成时间。
- registered_ms：实际封存时间。比完成时间晚超过 60000ms，标记 LATE_REGISTRATION；不得回填实时标签。
- data_available_ms：已取得证据的最晚采集时间，是本项目可证明的本地可用时间上界，并非交易所历史最早发布时间；必须不晚于完成时间。
- first_bar_open_ms：不早于 cutoff、完成、登记和数据可用时间的下一个 UTC 整点。
- first_check_ms：first_bar_open_ms + 1h，表示第一根允许观察的确认柱真正收盘后才可检查，而非开盘时预知其收盘。
- expires_ms：直接采用原 signal_timing.hypothesis_expires_ms；原 expires_at_utc、research_expires_at_utc 和各证据 TTL 仍完整保存在 original_result/证据中，各有语义，不合并。

有 cutoff 的实时意向要求 cutoff 不早于 started_ms。机器协议还保留300秒入口最大年龄上限；上述不得早于开始时间的检查更严格，不会使用该上限放宽时间条件。
行情完全失败、根本没有可靠 cutoff 时，允许把 cutoff/data_available/first_check/pair_id 保存为 null，明确无市场样本或可检查窗口，不编造截止时间。这类档案只统计故障。

## 生命周期及资格

继续使用 C.1 的 PlanTiming / evaluate_signal：PENDING_BREAKOUT → PENDING_RETEST → ENTRY_ELIGIBLE；WAIT、EXPIRED、INVALIDATED 语义不变。
突破只能由已收盘柱确认，回踩必须在其后柱确认；成交只能按已有下一根开盘规则模拟，并且仍需新的入场前核验。六根确认柱和原策略价位均不改。
F1 仅保存原 signal_state、signal_timing、价位、失效条件和时间边界，绝不在登记时提前推进未来状态。
如果研究完成/登记已经错过原 C.1 第一根允许确认的柱，F1 不补用登记前价格，不平移确认窗口：该档案不进入模拟池。

simulation_pool_eligible 是 F1 的必要资格筛选，不是入场许可。必须是实时及时登记、研究 COMPLETED、核心质量 VALID、原 plan_valid=true、假设尚未过期、未错过首柱且无过期/无效证据。
所有候选仍 requires_revalidation=true；allow_new_risk 保留原研究值，永不通过登记自动设为 true。阶段 C 的结果通常为 false。
可选证据 PARTIAL/缺失单独保留，不推断方向；本版对任何已明确 STALE_DATA / RESEARCH_INVALID 的证据采取保守归档，不纳入模拟池。
保留报告原始 effective_view，同时通过原有 evidence_view 在登记时再计算 registration_view；不请求新行情、不替换研究依据、不扩大 TTL。

## 成本与将来标签

沿用 D.3 冻结基准：模拟初始权益10000 USDT、名义1000 USDT、Taker 5bps、单次成交不利滑点2bps、确认6根1h、最长持有12根1h。
Maker 费率未定义，不假称实际账户费率。F3 必须沿用 D 的下一开盘成交、同柱止盈止损冲突先止损、止损缺口按更差开盘、止盈不获得缺口额外收益的规则。
滑点已进入成交价时不得重复扣减。资金费率仅用真实已结算事件和实际结算时刻；费用计价是否用了历史标记价或成交价代理必须显式披露。
资金事件缺失、参数无法验证、行情缺口或先后顺序无法判断，不可静默填补；保守假设仍须被记录，否则结算无效。
本轮不计算杠杆、保证金、强平，也不声称验证了实盘成交可行性。

F2/F3 预留 UNOBSERVED、TRIGGERED、INVALIDATED、EXPIRED_UNTRIGGERED、SIMULATED_FILL、TAKE_PROFIT、STOP_LOSS、NET_PNL、MAE、MFE 标签。
触发率分母是已登记且可观察的有效候选，胜率分母是已完成结算的模拟成交；覆盖率另计观察成功与失败。WAIT、未成交、数据失败、模型失败不可混入交易胜率。
方向判断没有单独冻结预测目标、期限和真值定义前，不允许输出“预测准确率”。本轮没有这些未来标签的实测结果。

## 封存结构与恢复

默认档案目录：`C:\Users\jerry_su\.tradingagents\logs\forward_validation\f1`，实际跟随配置 results_dir，不写入源码仓库。

- `<token>.intent.json`：研究开始前的配置白名单、模型设置、Git 分支/Commit、源码 SHA256、协议全文。
- `<token>.journal.json`：最终登记前的完成时间、研究结果、必要状态和原始报告内容，供异常恢复。
- `<registration_id>.json`：不可通过 API 覆盖的结构化登记。包括原判断、完整 original_result、质量证据、路径、协议/结果/源码/报告指纹、配对 ID、Memory 关联槽位。
- `<registration_id>.artifacts.json`：自包含复制的 HTML/Markdown、JSON 快照与结果，原报告搬移/删除不影响封存内容。
- `<registration_id>.events.json`：独立事件链，保留每个 event_id、自身 event_hash、previous_hash 和记录时间。
- `protocol-forward-f1-v1.json`：档案库第一次使用时冻结协议。同一版本内容变化会报错，不能静默重写已有样本规则。

复用原 files.locked 的进程锁；临时文件写完 flush/fsync 后原子替换。最终登记文件是提交标记，未完成的 journal/archive 不会被列表当作有效样本。
research_id 派生稳定 registration_id；已存在时核验原研究结果和快照，返回相同登记，不重复建样本。结论或快照冲突报错。
中断后重用 journal 的完成时间和封存内容，晚恢复仍会变为晚登记，不能刷新时间伪装。即使原报告已删除，也能验证 journal 内快照后恢复。
`load()` 核验登记及复制档案，`verify_original()` 单独显示原路径是否还存在/匹配；原路径缺失不会偷偷改档。
JSON 指纹使用规范化对象；报告文本指纹按 UTF-8 文本及统一换行读取计算，不声称是操作系统文件原始换行字节的取证签名。
本地哈希/锁无法对抗有权限同时重写全部文件、伪造意向或回拨系统时钟的恶意行为，不提供远程可信时间戳/WORM/签名服务。事件整体被恶意截断也需要外部备份才能发现。

## F2/F3 接口边界

- Registry.registrations()：含失败、WAIT 和导入的全部登记，供独立分母统计。
- Registry.pending()：尚为 QUEUED 的有效候选，不采集任何行情。
- Registry.load(id)、frozen_conditions(id)：冻结条件、原预案、统一 cutoff、首根柱/最早检查时间、截止时间、快照及成本协议。
- FutureBarObservation：后续已确认1h柱的所有权、开收盘、可用及记录时间、指纹契约；validate_against() 返回 registration_id+bar_open_ms 的幂等键，拒绝登记前行情。
- Registry.annotate()/events()：审计记录；原登记不变。
- Registry.update_tracking_status()：仅行政 QUEUED/DEFERRED/CANCELLED 更新；原本不合格的登记不能通过事件启用。
- F3 可读取 frozen_conditions 的成本和原结果及独立事件。F1 **没有**生成“已确认模拟成交”的接口或事件；F2 确定性状态复核、行情观察持久化和 F3 成交/结算事件 Schema 留待后续实现，不伪造已具备的成交能力。

Memory/Reflection 复用 research_id/snapshot_id 关联语义并预留 memory_links；当前没有对应前向结算，因此 links 为空，不读取历史结果、不调用 Reflection、不宣称自动学习已上线。

## 新旧配对

pair_id 由交易所、标的、相同 cutoff 和周期集合计算。两报告只有这些条件相同时才具备配对入口，仍须检查各自 snapshot_id、source_hashes、模型与 config，不能声称输入相同。
同一 pair_id 代表同一市场样本下的配置对照，不可当作多个独立市场样本；不同完成/登记时间的可跟踪区间仍须在 F2 公平截断。
相同原研究重复登记幂等；事后重跑即使 cutoff 相同也会标记晚登记/导入，不作为实时对照。F1 不自动运行双份 LLM。

## Windows PowerShell

```powershell
cd D:\_01trading_agents
# 原生命令默认不登记。增加开关后登记新研究，WAIT/模型失败也保留。
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --forward-register
# 按需添加已验收的 --orderflow --market-checks --price-depth --events，不会默认启用。
$registry = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f1'
.\.venv\Scripts\python.exe -m tradingagents.forward_validation --registry $registry list
# 用 list 的真实 registration_id 替换；核验封存与原报告。
.\.venv\Scripts\python.exe -m tradingagents.forward_validation --registry $registry verify '<registration_id>'
# 历史研究仅作为历史导入，不能升级为正式前向样本。
.\.venv\Scripts\python.exe -m tradingagents.forward_validation --registry $registry import-report '<已有完整多周期报告目录>'
# 只在已有 journal 的登记中断后使用，token 来自对应 intent/journal 文件名。
.\.venv\Scripts\python.exe -m tradingagents.forward_validation --registry $registry resume '<token>' '<原报告目录>'
.\.venv\Scripts\python.exe -m pytest tests/test_forward_validation.py -q
```

## 本轮真实工程验证

一次最小模型调用返回完整 OK，耗时7.0955秒；用量 input21/output5/total26 Token。
随后只进行一次真实 BTC-USDT-SWAP 4h、多周期、衍生品及预案原生研究，正式登记链路成功保存失败样本：

- research_id：105fa338bb24414db708e21710f05dfe。
- registration_id：648e415e0cbb99ba30a21a25cc106268febbabb3a0f69240a665d791e5ea0605。
- 开始 UTC 2026-10-24 03:14:52.642；cutoff 03:15:02.591；完成03:15:49.873；登记03:15:49.905（北京时间分别11:14:52.642、11:15:02.591、11:15:49.873、11:15:49.905）。
- 核心行情VALID；workflow FAILED；LLM_UNAVAILABLE；原动作WAIT，plan_status=LLM_UNAVAILABLE，plan_valid=false，模拟池资格false，execution_enabled=false。
- category=RESEARCH_FAILED，不是正常WAIT预测；origin=PROSPECTIVE仅代表这次实时研究尝试被及时登记。
- 错误安全摘要：Market Analyst / MODEL_SERVICE_ERROR / APIError / partial_response=true，无HTTP或底层异常细节，不能断言网络、配额或认证根因。
- 原报告：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_c32ad0140605\complete_report.html。

该样本是在F1开发中形成，后续时间接口又补充分开首柱开盘/收盘检查字段；保留原档案及代码指纹，不改造成最终版成功样本。旧档案缺少 first_bar_open_ms 的情况不能进入有效跟踪队列。错误来源另加独立审计事件，不修改原记录。
完整成功研究的正式登记当前只通过模拟原生工作流验证；真实模型服务仍有稳定性限制，不能声称完整成功链路实测通过，也没有反复重试挑选成功输出。

测试结果、版本指纹和最终限制见 forward_f1_acceptance.json；日志均在用户 logs 中，未纳入 Git。F1 有条件工程验收不意味着策略盈利或 F2/F3 已投产。


## 最终测试与验收建议

专项测试32项通过；最终 F1+原 OKX CLI/预案相关回归93项通过（34.67秒），包括登记错误保留报告和恢复标识。
全量非联网回归：1905 passed、5 failed、4 skipped、13 deselected、100 subtests passed（162.87秒）。全量之后又补充单周期快照封存用例和登记错误提示，已由上述93项最终检查覆盖；不把尚未执行的全量数量写成全绿。
五项为 V2.4 已披露的同名 Windows 失败：两项 ChatGPT 私密文件 POSIX 权限断言、两项升级命令以及 yfinance home 缓存隔离。本轮不修改无关认证、升级或缓存功能。
冻结策略指纹仍为 b61aa92741f23a9db0120211654ab56317490b22878622adeb77fe538eca79cf；原D.4.1目录146文件的最后写入时间均早于F1研究开始，未发生本轮写入，当前逐文件指纹也随验收清单保存（不是伪称拥有本轮开始前的新独立哈希基线）。

建议：**有条件通过 F1 有限范围工程验收**。协议、结构、默认关闭开关、封存、幂等、恢复、读取、审计事件及模拟原生流程已验证；真实失败研究的登记与原报告校验成功。
条件是保留真实完整模型流程未成功、五项既有回归失败、本地时间/哈希信任边界和后续F2/F3尚未实施的限制。先稳定模型服务，再以不重跑旧样本的方式积累新的前向研究。
F2 开发前应读取 frozen_conditions、first_bar_open_ms、first_check_ms、expires_ms 和 pending 队列，复用C.1推进状态；逐柱幂等键由 FutureBarObservation.validate_against 提供。F3 仅消费将来由F2确定性确认的模拟成交，沿用冻结成本，不得从本轮原动作WAIT或任何行政审计事件推导成交。

新增/修改：cli/main.py（唯一原文件修改）；tradingagents/forward_validation.py；tests/test_forward_validation.py；docs/forward_f1_protocol.json；docs/forward_f1.schema.json；本说明；docs/forward_f1_acceptance.json。未新增 Agent、依赖或数据库；AGENTS.md 原样保留、不暂存。本轮不 commit/push，不进入 F2。
