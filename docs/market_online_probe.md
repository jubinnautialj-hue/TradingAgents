# Market Online Probe v1

本工具仅验证一个Market模型响应的在线协议兼容性，不是研究系统、交易候选或绩效样本。本轮实现仅离线测试，禁止据此直接部署或发起付费请求。

## 用途隔离与入口

Policy新增`run_purpose`，默认`research-v1`；Probe必须显式为`market-protocol-probe-v1`。普通`run`、`_worker`及watchlist拒绝Probe；`probe-claim`、`probe`、`_probe_worker`拒绝普通用途。既有PRODUCTION禁止invoker测试注入的规则保留；FIXTURE不传显式离线invoker也拒绝，CLI不暴露invoker参数。

复用原生init、Ledger、Session、Meter、controller/transaction锁、原始SDK用量与归一化对账、UNKNOWN暂停及监督/终止机制。不调用TradingAgentsGraph、ToolNode、后续模型回合、报告生成器或Registry。没有新Agent、SDK或依赖。

未来经独立安装及付费授权后，服务协议为：唯一原生init → 审批文件由管理员批准 → ExecStartPre原生`probe-claim` → ExecStart原生`probe`。目前没有安装任何这些服务或正式配置。

## 一次性行政绑定

独立approval版本`market-online-probe-v1`，绑定用途、authorization_id、唯一账本绝对路径、Commit、模块SHA256、原生实现指纹、原始Policy SHA256、规范化Policy指纹、完整实际运行配置指纹，以及60秒claim窗口。新增审批必须默认approved=false，no_retry=true、attempts_authorized=1。

PRODUCTION检查审批目录、Policy和审批文件为可信root属主且不可由组/其他用户写入；代码必须已提交且与实际源码一致。运行身份和cgroup仍由原生resource_check验证，不能靠环境变量伪造。

claim沿用既有行政门禁的O_EXCL/O_NOFOLLOW、0600、文件fsync和目录fsync协议，并在原生非阻塞controller锁下核验原生总账尚无调用/任务/暂停、T0及截止有效。标记为ledger/probe/claim.json，永不删除或重建。claim只记录已消费授权，不等于研究开始。

`probe`在controller锁下由原生start_research生成唯一run_id，网络请求前持久化binding.json及UNFINALIZED receipt。子进程独立核验approval/claim/binding/run_id，并以O_EXCL创建worker.entered.json；重复入口、restart或并发均不能再次调用模型。claim、绑定或worker标记部分写入/异常时保留现场并拒绝，不自动修复或重放。

## 一次Market回合

完整运行配置在请求前核对审批中的config_hash，结果只写本次私有目录；不读取旧账本、报告、F1、记忆日志或授权。模型必须为现有chatgpt/gpt-6.1-sol，仍经过原生Session守卫。

复用原生OKX预检和多周期/衍生品采集、原Market节点、原提示词、已验收安全适配器及严格Schema。启用多周期、衍生品、原有宏观来源采集；宏观403是否允许缺失取决于显式批准的optional_source_policy，建议未来配置沿用official-macro-403-v1，不扩大白名单。

不启用交易预案生成或F1。质量通过只表示原生预检允许本次协议验证，不是交易许可；对外三个安全字段永远False。Probe PASS不证明衍生品证据完整、研究结论正确或F1资格有效，也不放宽任何数据TTL。

一次响应允许多个完整工具调用，但只验证终态、身份、索引、参数一致性、Market Schema及用量，不执行这些工具。模型可能不调用工具，此时INCONCLUSIVE，不得自动追加调用。不得强制工具选择或改写提示词以制造覆盖。

## 固定Probe预算

- 单次、单日、累计Token软阈值100000，调用前余量100000。
- 单次、单日、累计逻辑调用均1；HTTP均240；task_seconds=900。
- Policy仍保留72小时、max_researches=3、24小时间隔原生约束，但一次claim及累计1次调用意味着不授权后续轮次。
- 仍需1536MiB、1核、Swap≤256MiB、TasksMax≤64、Nice≥10、Restart=no及原生进程组终止限制；本轮不安装systemd单元。

原生准入实测：零已消耗Token加100000余量恰好等于100000阈值，首次调用通过；已消耗正数后下一次调用在HTTP前拒绝。用量超过100000的完整在途响应仍可能发生，守卫按真实用量入账并暂停；这不是货币费用硬上限。实际费用及剩余额度UNKNOWN。

## 独立收尾

receipt.json只包含版本/用途、run_id、claim引用、配置指纹、状态、工具数量、公开快照指纹/cutoff、已报告Token及实际用量状态、HTTP/逻辑次数和三个False字段，不存模型正文、提示词或工具参数。

| receipt状态 | 含义 |
|---|---|
| PASS | 此次完整响应至少有一个工具调用，经原适配器和计量对账通过 |
| INCONCLUSIVE_NO_TOOL_CALL | 响应及用量完整，但没有工具事件覆盖；不能称为兼容验证通过 |
| FAILED | 数据/协议/配置/审计/预算失败；已知用量保留 |
| USAGE_UNKNOWN | 流/SDK/对账异常或未决调用；实际用量必须UNKNOWN |
| UNFINALIZED | claim后的运行尚未完成，或崩溃未收尾；禁止据此再次执行 |

Ledger的runs是原生操作计量容器，成功行仍使用原生COMPLETED/REVIEW_ONLY，不代表11角色研究成功。只有该用途的独立receipt可解释为Probe结果；禁止混入正式研究完成率或交易绩效。

异常使用原生finish_research(success=False)封存并暂停。强杀可能保留RUNNING/PENDING及UNFINALIZED；必须另行批准原生recover-interrupted，追加失败封存并同步Probe receipt，不补填Token、不解锁或重试。已完成记账但最终receipt未落盘时，追加PROBE_FINALIZATION_FAILURE暂停并标为失败，不能把缺失验收证据解释为PASS。

审计继续复用append_audit、0600、永久侧锁、身份保护及4MiB容量。在任何请求前先写入脱敏Probe准入事件，审计权限/容量异常先行拒绝；执行期间审计失败仍传播。

## 版本与验收边界

新增用途改变完整Policy配置指纹；safety_budget.py及guarded_research.py变化改变原生实现指纹，算法和输入文件清单不变。模块还由审批中的独立SHA256和已提交源码检查保护，不能把守卫指纹当成全部业务源码指纹。必须新建独立配置及预算，不迁移任何旧账本。

普通研究默认用途、阈值、TTL、模型、11角色和F1/F2/F3语义不变。docs/d4b_safety_budget.json仅同步显式默认用途，不改变任何原预算值。Market安全适配器和第三方依赖未修改。

离线验证覆盖：原生100000余量边界、用途双向拒绝、审批/配置/身份漂移、并发唯一claim、重复worker拒绝、单次模型HTTP、真实SDK MockTransport与原Market节点、数据拒绝、三类协议安全边界、UNKNOWN暂停、软阈值在途超额、零调用伪成功拒绝、崩溃封存、审计容量/权限及脱敏。POSIX权限及真实systemd资源环境必须在后续Linux离线验收补测。在线认证、费用和真实事件兼容性仍未验证。

## 本地验收记录（2026-11-04）

- 最新Probe专项48通过、1项POSIX平台跳过。
- 联合回归476通过、7项平台跳过、8个子测试通过；包括当时的Probe用例，不能与专项简单相加。
- 最终入口及Probe复核82通过、1项平台跳过，覆盖最终CLI输出和恢复后状态读取相关源码版本。
- F3.1为46通过、2项既有失败：Reviewed断言及b01436cc协议字节比较；未修改或跳过。
- 首轮默认Policy示例缺少新增用途字段的一致性失败已通过仅同步示例字段消除；普通研究所有原有字段及预算值与基线相等。
- 测试曾修正Fixture的Windows原始文件SHA256计算及无工具响应不应产生工具准入记录的断言；没有放宽生产门禁。
- 结论：MARKET_ONLINE_PROBE_READY_FOR_LINUX_ACCEPTANCE。不是在线兼容验证成功，也不是部署或付费授权。
