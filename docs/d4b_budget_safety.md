# D4-B.0 BTC 低频研究预算与安全守卫

## 状态与范围

这是本地开发版本，**未部署、未启动72小时研究、未安装定时任务、未调用真实模型**。
开发分支 `codex/d4b-budget-safety-guards` 从 F3.1 提交
`43e320d2c93ad5363ca71c31137babc426128b8a` 创建，其历史包含 R1.4
`01b82f435bb3631d3325e3a440b356d32bbb1312`。原有未跟踪文件保留。

仅新增 `safety_budget` 与 `guarded_research`，通过进程内作用域补丁复用
`auto_research.run_batch / worker / validate_pool / recover`、原 CLI 和 F1。
没有修改原 `auto_research.py`、`research_budget.py`、模型客户端、策略、提示词、
C.1、F1/F2/F3/F3.1 或既有代码哈希。

新守卫不拦截旧 CLI 或整个账户的其他应用。所谓全局预算，指**所有使用同一个审批后
canonical ledger 根的 D4-B 任务**；不等于账户账单总额。部署前必须保持旧研究调度关闭。
本入口不提供另一个 output 参数，原研究输出固定在该根的 `batch/` 中；重启、跨批次、
cutoff变化不能获得新的预算。新建另一个审批根是新的人工授权，不能当作自动恢复。

三个最终安全字段始终False。历史快照的 `allow_new_risk=True` 继续仅代表数据资格；
最终许可、F1校验、原始预案、TTL均不改写。不运行F2扫描、paper、timely或F3.1。

## D3.3计量结论与新计量边界

D3.3已有14次逻辑调用、56次计量HTTP、SDK汇总442522 Token，费用UNKNOWN。
旧档案没有逐调用用量，无法事后可靠拆分、去重或证明覆盖率，本升级不回写旧实验。

本机安装的 LangChain `_create_usage_metadata_responses` 会把缺失输入/输出补零，
还可能用两者相加生成缺失总量。新入口在该规范化之前读取**原始 SDK usage**：

- input_tokens、output_tokens、total_tokens；cached_input_tokens、cache_write_tokens、
  reasoning_tokens，字段未提供时为JSON null，状态含UNKNOWN，不伪造0。
- cached input是输入子集，reasoning是输出子集，不再次加到total。
- 原始输入+输出必须等于原始total；可获得的细项必须与规范化结果一致。
- 逐调用原始total须与旧Meter本次增长一致；研究结束时所有旧Meter之和须与独立逐调用之和一致。
- 总量、计量有效性和是否完整收到响应分开记录。部分响应即使用量存在也不能算完整决策。

input/output/total是必需的核心计量。缓存、cache-write、reasoning细项是可选字段：缺失时保持UNKNOWN，
但预算仍按完整原始total占额，既不扣除缓存，也不假定折扣，因此不因缺少这些细项少计总Token。
这是已用固定用量测试验证的保守处理；它不能豁免核心字段缺失、算术不一致或实际费用未知。

参考：[OpenAI官方缓存说明](https://developers.openai.com/api/docs/guides/prompt-caching)。
文档说明不能证明当前订阅账户的实际费率或额度，运行时仍以已安装SDK实际字段为准。

`CALL_STARTED`在发送前持久化唯一逻辑ID。每个底层HTTP有独立wire_id、call_id、
UTC毫秒开始/返回时间、端点类别和可获得HTTP状态；只保存request/response ID的SHA256。
**不保存URL、请求体、提示词、响应正文、Token、Cookie或Authorization**。
HTTP返回时间是传输函数返回（流式时通常是响应头可用），不冒充SSE完整结束时间；
完整结束由CALL_FINISHED另行记录。HTTP_STARTED是发送前保守占额，异常时无法证明网络已发出，
因此计量发送/预留数不等于独立抓包数或账单请求数。

同一逻辑调用只允许一个模型发送。第二个发送在网络前拒绝，避免SDK重试或重定向造成未知重复费用。
原生Graph可以并行调度分析员，新入口仅将实际模型发送及其计量串行化，保留原Graph、输入和提示词。
不同线程的call_id与原始用量独立，避免一个分析员的用量落入另一个分析员的Meter增长区间。
这可能延长完整研究耗时，是否仍满足900秒须经未来受控实测，不能用单模型探针替代。
认证刷新和公开GET分别计入HTTP，不算模型Token。原生公开市场白名单继续生效；
除批准的Responses POST和官方认证刷新POST外，其余写请求拒绝。

同一provider response ID重复返回不会再次加Token，立即暂停；没有原始用量、没有模型发送证据、
疑似本地缓存复用或原始用量不唯一时均暂停。未经证明不能把缓存结果的旧Token再当成新消耗。
本版本只支持当前同步 `invoke` 路径及其**内部Responses SSE**；直接公开stream/astream与异步调用拒绝。

失败/认证错误/限流/网络故障停止下一次请求，不在守卫中扩大重试。部分流与截断不重放。
未知值使total_usage=UNKNOWN，tokens只代表已知上报子总量，不能把未知总量显示成0。

## 版本、审批与默认预算

机器配置 `d4b_safety_budget.json`；Schema `d4b_safety_budget.schema.json`；版本
`d4b-safety-budget-v1`。enabled与accepted_post_response_exposure默认均False。
生产初始化须用户显式批准配置；实际启动还要求新守卫已经被审阅并提交，源码与HEAD一致。
本轮不会自动提交，所以当前未固化实现不能直接进入生产启动。

|范围|默认限制|
|---|---|
|标的/模型|BTC-USDT-SWAP；沿用chatgpt/gpt-6.1-sol适配器，模型变化拒绝|
|研究间隔|实际启动间隔至少86400秒，不按UTC日期变化解锁|
|周期|审批初始化起72小时，最多3次；失败也消耗一次，不补跑|
|研究/UTC日/全周期Token|600000 / 600000 / 1800000，上报后的软停止阈值|
|调用前操作余量|100000 Token；不是已证明的最大请求暴露量|
|预警|各Token阈值70%，status.warnings可读|
|逻辑调用：研究/UTC日/周期|24 / 24 / 72|
|HTTP：研究/UTC日/周期|240 / 240 / 720，包含验证和认证发送|
|研究时间|900秒；独立父进程超时监督，systemd另设900秒进程级限制|

**不是请求前Token或金额硬上限。** 当前ChatGPTPlan适配器明确丢弃max_tokens，
本轮未证明可设置请求前输出上限，也没有已验证的最大输入暴露量。一次在途请求可能超出余量
甚至整个软预算；此时记录完整已知用量并停止后续调用，不隐瞒超额。若用户必须保证不超过
Token或金额硬额度，当前实现不能满足，应保持禁用，而不是修改模型或认证方式。

跨午夜响应按开始日和完成日均保守占用日Token额度，研究/全周期只加一次。
日计数之和可能大于周期计数，这是明确的保守规则，不是重复计费。跨日不会清除未知用量或暂停。
接近72小时尾端、无法容纳完整任务上限时拒绝新任务；父进程等待也受任务及周期剩余时间限制。
本轮没有修改冻结模拟手续费、滑点或策略参数。

## 持久化、暂停和恢复

`journal.json`是原子替换、fsync的追加语义事件日志，含策略、根路径、实现指纹和SHA256链。
每次读写在共享文件锁下执行；controller锁覆盖整个研究。状态由事件重算，不能重启清零。
不同进程竞争只有一个能够领取研究任务。复制账本到其他路径、篡改数据、实现变化均拒绝。
日志有10000事件/16MiB上限，到限停止，不能自动删档重新开始。
哈希用于损坏检测和追溯，不是能抵御持有同一用户写权限的攻击者的数字签名。

人工暂停允许在当前流结束后阻止后续发送，不能承诺撤销已在途的远程模型请求。
硬停止用独立服务的systemctl stop / 原进程组终止，不触碰其他服务。恢复不会重置计数。
资源准入暂停需实机资源重查后人工resume；未知Token、失败响应、不一致和账本损坏不能直接resume。
`recover-interrupted`只将孤儿任务记为失败/未知，不重新研究、不重放模型、不解锁未知费用。
此版没有人工猜填Token的解锁接口；需要外部真实证据的计量修复应另行评审，不能新建账本规避。

F1仍负责封存与研究语义；全局日志保存registration_id、原始动作、plan_valid、workflow_status关联。
研究WAIT可以完整完成并登记；模型失败保留失败；预检失败可能尚无报告/F1，此时只记录运行失败。
预算运行状态与原始研究状态独立：守卫失败不能改写已封存的原研究结论。
研究回放夹具为FIXTURE/TEST_REPLAY，不进入正式前向统计。审批与超时测试中的PRODUCTION形状
仅写pytest私有临时目录并使用Mock进程，不代表已启用正式研究或纸面评价。

## 资源保护与部署条件

复用D3.3/D4-A实机资料：服务器2核/3392MiB，D3.3研究峰值377.9MiB，D4-A可用内存最低约2099.7MiB。
这些数据不是研究与及时任务并行的生产测试。本轮不连接或修改Vultr正式目录。

生产入口仅允许Linux、ta-research，要求当前cgroup已经限制：MemoryMax<=1536MiB、
SwapMax<=256MiB、CPU<=1核、TasksMax<=64、Nice>=10；缺少约束直接拒绝，不能在Windows绕过。
运行前及每个网络/逻辑调用前复核可用内存>=1536MiB、磁盘>=10GiB、NTP同步与进程时钟漂移。
单次在途流不能靠本地检查立即中止，必须同时使用父进程900秒超时和systemd cgroup/OOM保护。
`d4b_btc_research.service.example`只是未安装示例，不包含timer，不执行任何安装命令。

示例中的隔离代码、预算目录及认证HOME均尚未创建。未来应由用户明确批准认证可写目录，
维持相同ChatGPT认证方式、用户交互授权；不得复制Windows凭据或擅自改动R1.4已有认证目录。
同一服务器其他服务的资源竞争须在未来受控启动前重新只读核验，不能用旧资源值代替当时情况。
本轮2026-10-26 10:55:15 UTC仅作只读实机复查：负载0.10/0.10/0.09，可用内存2282MiB，
磁盘约92GiB空闲，NTP同步；现有hermes、Codex、宝塔进程保留，未发现相关长期timer。
没有部署或启动本轮守卫，这不构成Linux新版运行验收。

## Windows PowerShell本地验收与只读操作

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m pytest tests/test_safety_budget.py tests/test_guarded_research.py -q
.\.venv\Scripts\python.exe -m tradingagents.guarded_research --help
```

以下只适用于**已经单独批准初始化**的预算根，不应对不存在的正式根先创建新预算：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.guarded_research status --ledger C:\approved-local-ledger
.\.venv\Scripts\python.exe -m tradingagents.guarded_research pause --ledger C:\approved-local-ledger
.\.venv\Scripts\python.exe -m tradingagents.guarded_research resume --ledger C:\approved-local-ledger
.\.venv\Scripts\python.exe -m tradingagents.guarded_research recover-interrupted --ledger C:\approved-local-ledger
```

status严格只读、不创建目录或锁。pause/resume/recover仅写新预算账本，不调用模型或推进F1/F2/F3。
生产根的resume要求Linux资源复核，因此从Windows应经已授权SSH调用对应Linux命令，不提供绕过方式。
实际初始化/运行命令仅为未来审批后的操作接口，本轮不执行：

```text
python -m tradingagents.guarded_research init --ledger /var/lib/tradingagents/d4b-budget --policy <已批准配置> --approve
python -m tradingagents.guarded_research run --ledger /var/lib/tradingagents/d4b-budget
```

## D4-B启动验收清单

1. 本轮专项、原生F1链路、R1.4/F1—F3/F3.1兼容回归记录可核对，原冻结文件无变化。
2. 新实现提交及隔离部署另行授权；核验代码/Schema/配置/账本根指纹，保持旧研究通道关闭。
3. 用户批准单研究/日/周期软预算和在途超额风险，不把调用数当作金额硬上限。
4. 使用固定账本根，根路径权限与只读状态入口分离；仅BTC，72小时最多3次，每次实际间隔>=24小时。
5. 验证真实Linux cgroup、其他服务资源竞争、UTC/NTP、孤儿任务和明确人工暂停/停止路径。
6. 认证方式保持不变，认证目录变更需另行授权；不复制任何Token。明确配额不足即停。
7. 先单独授权低成本真实探针，再考虑完整研究；原F2/F3/F3.1仍关闭，不能因候选出现自动放行。

## 尚待授权的真实计量探针方案

本轮真实调用=0。建议另行授权一次：无工具、只要求返回OK、同一已配置订阅模型；
1逻辑调用、最多4计量HTTP、60秒、禁止重试。成功后先对照原始/规范化/独立聚合用量，
不要自动追加第二次，更不执行完整研究。可设10000 Token事后停止阈值，但该提供方仍无已验证
请求前Token/金额硬限额，因此这不是10000 Token的硬费用承诺。用户若不接受该暴露，应不授权。
未来探针应使用独立授权协议/夹具隔离的诊断记录，不能占用或伪造正式3次研究/F1样本。
