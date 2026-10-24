# R1.2 自动任务协调与 F1—F3 档案衔接

基线：`99ca5509b1dba2f71098fdad6a548940726668b6`，分支
`codex/auto-research-r12`。新增配置 `automation_r12.json` 及同名 Schema。
原 R1.1、F1/F2/F2.1/F3、C.1、A—D、V2.1—V2.4 源码和协议不变。
不新增 Agent，不调用账户或订单接口。execution_enabled、simulation_admitted、
allow_new_risk 始终 false。R12 不计算新的交易方向或收益规则。

## 四条独立运行通道

| 通道 | 默认触发（UTC） | 调用的原能力 | 互斥范围 |
|---|---|---|---|
| research | 每四小时，整点后 20 秒 | R1.1 run_batch，原 okx-research/F1 | research.phase + R1.1 原控制器/工作锁 |
| observe | 每分钟第 2 秒 | 原 forward_tracking.scan | observe.phase + 原 F2 扫描锁 |
| paper | 每五分钟第 20 秒 | 原 forward_paper.scan | paper.phase + 原 F3 账本锁 |
| timely | 每小时 58:40 启动限时预热 | 原 F3 重新验证、结构校验、fill、Ledger | paper.phase + 原 F3 账本锁 |

研究通道独占的锁不会阻止 observe/timely。研究调用原 R1.1 观察池和每项/整批
预算，默认 BTC/ETH/SOL 串行，不重建 Graph。已有市场/新闻/情绪、多空辩论、
交易员、风险团队及 Portfolio Manager 均保持原逻辑。

研究周期可配置为 1/2/3/4/6/8/12/24 小时，但必须与 R1.1 watchlist 的
min_interval_seconds 一致；不匹配时拒绝模型任务。observe 可配置 1/2/3/5 分钟，
paper 可配置 1/5/10/15/30 分钟。降低观察频率可能导致 F2 的 BACKFILL 降级，
不会延长原 90 秒交付分类阈值。及时通道仍依照原每小时开盘和 15 秒规则。

明确额度耗尽或账户/权限受限时，R12 持久暂停 research 通道，并标记需要人工
恢复；不会每个新 slot 继续撞同一已确认限制。observe/paper/timely 不随研究
服务故障暂停。解决服务问题后需显式 resume --phase research，仍不重跑旧 slot。

R12 对每个研究 slot 只派发一次。配置/进程变化、失败或未知中断不会自动重跑
昂贵模型。R1.1 的任务身份、最小间隔、HTTP/模型预算、额度阻塞与登记 journal
恢复继续生效。周期 slot 是调度身份，不冒充实际市场 cutoff；cutoff 取原报告。
1—3 天是研究定位，本轮没有改变 C.1 确认窗口或 F3 的 12 根最大持仓规则。

## 档案路径与只读注册索引

默认路径：

- batch_root：`~/.tradingagents/logs/auto_research/r11`。
- state_root：`~/.tradingagents/logs/automation/r12`，与批量根及所有 F1 根隔离。
- r11-current 的 registry=null：自动指向 batch_root 下
  `production/research/forward_validation/f1`，供未来新研究使用。
- r11-accepted：当前已有真实 R1.1 验收档案
  `~/.tradingagents/logs/r11_live_20261024/production/research/forward_validation/f1`。
  其他机器没有此可选目录时显示 NOT_YET_CREATED，不制造记录。
- 每个来源独立使用 state_root/f2/<name> 和 state_root/f3/<name>，没有合并账本或
  将旧 F2 观察链迁移到新版本。新源可在 sources 中显式配置，最多四个。

索引使用原 Registry.load/events 校验封存和审计，但只读查看不调用其会创建锁/
协议文件的构造器。不移动、不复制伪注册目录、不修改原报告或 F1 登记。
bindings 冻结来源名称和原 registry/F2/F3 连接关系；同名来源换路径会阻断，
不能静默继承旧观察链。index.json 保存 ID、research_id、原 content_hash 和归属。

重复路径规范化为别名，只调度一次。不同目录含相同 ID 时，相同内容保留原归属，
隔离重复来源；内容不同隔离冲突来源。原来源顺序变化不夺取已冻结的 ID 归属。
因为原扫描按整目录运行，有任意重复/损坏记录的来源会整源暂停，避免重复处理；
其余有效记录也会暂缓，必须在配置/数据层修复，不能偷偷拼一个过滤后的注册库。

丢失已索引记录不会在下次索引时被忘记；损坏指纹、缺复制档案、无效协议、错误
路径或超过默认每源 1000 条的索引上限均暂停相关源。上限可配置至 2000，仍需
容量/时效验证。历史导入、TEST_REPLAY、晚登记保留但不计为真实前向样本；旧版
F2 链由原代码拒绝，不自动迁移。statistics_complete=false 表示统计只覆盖
当前已验证来源，不能把隔离源的缺失敞口当成零敞口。

## F3 15 秒窗口：最小兼容适配

原常规 F3 scan 在获取开盘价前还需遍历登记、校验多个候选、处理其他持仓及
网络数据，且每次 CLI 有冷启动成本。不能把整点 cron 当作及时成交保证。

新增 paper_timing 只协调**已经由原 F3 准入、提前封存的 SCHEDULED**，不新增
准入策略、价格、止盈止损、数量、资金结算或会计实现：

1. 在目标整点前 80 秒启动 Python，预加载模块、校验索引、读取原账本。
2. 目标前 40 秒调用原 Market.refresh/重新验证；同一边界的选中候选全部先刷新。
   使用原 60 秒重新验证 TTL、原始漂移检查和研究预案有效期，不延长任何 TTL。
3. 到目标整点后 1 秒，逐个重新加载原登记、F2 条件、F3 状态并核验身份，获取
   最新已收盘完整前缀，通过原 check_structure，然后调用原 Market.opening 和 fill。
4. 假设成交时间仍为报价实际接收时间；15 秒内确认、confirm=0 当前柱仅引用 open、
   预先持久化安排、价格/数量及风险标志检查全部由原 F3 再次执行。
5. 已经 OPEN 的状态不再开仓；网络错误、无确认价、过期、结构失效、迟到或缺柱
   保留暂停/取消/MISSED，不能事后用历史开盘补成交。暂停在报价途中发生也不入场。
6. 常规 paper 在整点前 120 秒至后 20 秒让位；timely 与其共享通道锁，但 F2 和
   研究仍独立。单次最多处理三个到期候选，可显式配置最多五个；超容量会留待后续
   按原规则记录错过，不为提高成交数扩大窗口。需要更多容量应先独立压测。

适配层复用原 Ledger.append，因此 policy/code 指纹仍为原 F3，状态和会计字段
均通过原 Schema；仅在新事件 payload 增加 scheduler_evidence，保存期望时刻、
实际开始、延迟、重新验证完成、报价接收和假设成交时刻。没有修改 forward-paper-v1。
持仓退出、费用、资金费率、权益与绩效继续交给原 F3 常规扫描处理。

R12 调度日志另存 configured_grid_ms/grid_lag_ms、进程实际启动及 WORKER_READY。
这是配置网格和启动开销的测量；手动执行不能被解释为操作系统晚派发的证据。
市场及时性以具体预案 target、报价和实际观测时间为准。

## 手动使用与暂停

```powershell
cd D:\_01trading_agents
$python = 'D:\_01trading_agents\.venv\Scripts\python.exe'
$cfg = 'D:\_01trading_agents\docs\automation_r12.json'

# 无目录创建、无网络/模型的预览和只读状态。
& $python -m tradingagents.automation dry-run --config $cfg
& $python -m tradingagents.automation status --config $cfg

# 单次衔接 F1 → F2 → F3；默认不新调用模型。
& $python -m tradingagents.automation cycle --config $cfg

# 显式启动一次批量研究；仍服从 R1.1 和 R12 同 slot 幂等。
& $python -m tradingagents.automation research --config $cfg

# 受控手动全链路：研究在独立进程中，等待期间继续协调观察/纸面任务。
& $python -m tradingagents.automation cycle --config $cfg --include-research

# 可分别由外部调度器调用；timely 只在有已安排且临近开盘的候选时限时等待。
& $python -m tradingagents.automation observe --config $cfg
& $python -m tradingagents.automation paper --config $cfg
& $python -m tradingagents.automation timely --config $cfg

& $python -m tradingagents.automation pause --config $cfg --phase research
& $python -m tradingagents.automation resume --config $cfg --phase research
# 不指定 --phase 时操作全局 all。全局恢复不清除已有单通道暂停标记。
& $python -m tradingagents.automation report --config $cfg
```

暂停是应用级“禁止新的调度”，不是强杀已经启动的 R1.1 批次。已启动批次可以
按原预算完成并封存结果；如人工强制终止，未知工作不自动重跑。及时入场在
报价返回后再次检查暂停。暂停不会平仓或改动已有纸面持仓，也不会修改 OS 任务。

## OS 调度：本轮只导出和校验

```powershell
& $python -m tradingagents.automation export --config $cfg `
  --export-dir "$env:USERPROFILE\.tradingagents\logs\r12-schedule-review"
```

生成四份 UTF-16 Task Scheduler XML、四份 systemd .service/.timer 及 manifest。
Windows XML 使用 IgnoreNew、不补启动错过的触发、明确 UTC 起点、项目工作目录、
当前虚拟环境 Python 和已冻结 config hash。没有注册、启用或连接账户。
导出后修改 automation 配置必须重新审核/导出；否则任务拒绝不匹配的 hash。

未来人工导入时应逐项检查用户、路径、网络、认证、系统时钟、代理和运行权限。
模板默认 InteractiveToken，只在该用户登录时运行；无登录运行需要部署者在系统
本地另行配置合适账户，不能向聊天提供密码。任务不依赖 Codex 会话存在，但虚拟
环境、代理/SSH 隧道和模型认证需要独立可用。普通研究任务不能占据 paper 锁。

Ubuntu 文件是用户级模板，假定 %h/TradingAgents 及其 Linux 虚拟环境，不能复制
Windows .venv。部署前用 Linux 本地配置重新导出/校验。timer 使用 UTC、
AccuracySec=1s、RandomizedDelaySec=0、Persistent=false；service 使用有界超时、
KillMode=control-group、Restart=no。本轮未安装 systemd 单元或在 Linux 实测。

参考：[Microsoft Task Scheduler 实例策略](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-multipleinstancespolicy-settingstype-element)、
[systemd timer 官方说明](https://www.freedesktop.org/software/systemd/man/259/systemd.timer.html)。
配置精度不等于实时保证；操作系统负载、睡眠恢复、冷启动、磁盘及网络都可能
造成错过，必须接受 MISSED，不能调宽 F3 窗口来美化结果。

## 故障、审计与监控

每个 job 有独立启动锁、dispatch/config、追加 hash 审计、子进程完成记录和结果。
研究 ID 按 slot 固定；其他扫描有新 job ID，但 native F2/F3 去重处理原事件。
子进程自己保存 worker_result/WORKER_FINISHED，协调器退出也能保留完成证据。
缺乏完成证据显示 INTERRUPTED_OR_RUNNING_UNKNOWN，不假设还活着，也不重跑模型。

研究严格使用 R1.1 模型/HTTP/墙钟预算；R12 外层再限制自有进程树。observe/paper
默认每工作阶段 50 秒，timely 120 秒，外层允许有限启动开销后强制终止自有进程。
Windows 只 taskkill 本次 Popen 的 PID 树；Linux 只终止自己建立的进程组。
每源默认至多 12 次逻辑 GET、原每次最多三次 HTTP 尝试；及时阶段另有 60 次
实际 HTTP 派发的共享计数守卫、0 次模型额度。预算耗尽不能转为有效成交。

不保存 stderr、认证头、Token、Cookie、请求/响应正文。受控 stdout 临时文件用于
解析组件结果，2 MiB 上限，完成后删除；登记意图 ID 不等于认证令牌。原报告仍在
原组件目录，调度器不覆盖。退出 0 可能只是完成协调、BUSY 或零候选，不表示
产生交易；必须查看各源状态、统计完整性和原生报告。内部 _worker/_probe 不应
直接调用来绕过外层时限监督。跨主机锁、网络文件系统与无限期日志容量未验证，
长期运行前需另行制定保留、备份和告警方案，本轮没有自动清理历史档案。

status 不写任何文件；report 显式保存 JSON/HTML。计数包括研究成功/失败/WAIT、
F1 登记核验及待处理、F2 触发/失效/到期/行政排除/缺覆盖、F3 安排/持仓/待资金
结算/已结算/错过/无法确定、历史预算、超时和恢复。只计真实 PROSPECTIVE，
原 data_quality 是研究时点字段，不代表现在行情仍新鲜；可选证据的详细过期/
覆盖原因以原研究和 F2/F3 报告为准。F2 到期只表示生命周期，不自动证明期间
没有触发。F3 等待审查不等于已经纸面准入，更不是新增风险许可。

## 验收与后续边界

结果与指纹见 `automation_r12_acceptance.json`。真实环境未出现有效安排时，
按零候选/零成交输出，不使用夹具填充绩效。已有正式 R1.1 WAIT 记录被只读发现，
成功运行 F2/F3 排除/零样本链路；R12 不重跑该昂贵模型，也不声称新研究或盈利。

最终全量回归：2087 passed、5 failed、4 skipped、13 deselected、100 subtests passed，
237.09 秒；R12 的 39 个案例全部通过。五项失败仍是已披露的 Windows 私密文件
权限两项、升级命令两项、yfinance home 缓存隔离一项，本轮没有修无关功能。
F1—F3/R1.1 早期集成批次 205 项通过，后续新增边界案例已包含在最终全量结果。

真实协调执行（未包含 research）成功发现既有 R1.1 正式 WAIT，F2 扫描和 F3
零样本评估完成；timely 返回 NO_PENDING_OPEN_IN_WINDOW。没有新增模型调用，
没有生产纸面成交，没有用夹具填充统计。三标的官方合约均为 live/USDT；另有
两根已确认 BTC 小时柱及当前未收盘柱端点验证。公开探测共 6 次逻辑 GET，暖进程
取已收盘柱 + 当前 open 引用的单次耗时 588.31 ms。它不是计划整点的实际成交
测试，也不是 OS 触发和未来网络时延的 SLA 保证。

四份最终 Windows XML 通过 Task Scheduler COM NewTask/XmlText 的纯内存定义
校验，未调用 RegisterTask/安装任务。原先 UTF-8 声明在 COM BSTR 中的编码
切换问题已改为原生 UTF-16。Windows 自有临时 Python 睡眠进程超时验证约
0.55 秒内完成终止并记录 TIMEOUT，没有网络/交易操作；Ubuntu 尚未部署验证。

验收建议：有条件通过工程验收。真实有效候选的及时成交与结算、长期调度
抖动和多候选峰值容量仍未实证；研究通道的完整模型能力沿用已验收 R1.1，本轮
通过隔离测试验证委派和并发，不重复耗费模型验证它。源档案及冻结实验保持原样。
仅生成调度模板，不自动安装、commit、push 或进入 R1.3。

R1.3 若要允许 ETH/SOL 纸面模拟，需独立核验合约面值、tick/lot/min、实际结算
时间、资金费用价格口径，以及将当前 BTC 专用历史费用验证和 F3 的 BTC 限制
安全泛化。须有逐币种确定性撮合/费用回归、真实公开验证、版本化兼容与旧账本
保留方案。R12 只让 ETH/SOL 进入原研究和符合原条件的 F2，绝不解除其 F3 限制。
