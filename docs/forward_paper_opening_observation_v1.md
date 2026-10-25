# F3.1 有界开盘观测扩展操作与验收说明

实现分支：codex/f31-bounded-opening-observation。
实际设计基线：b01436cc096af4bb5f64bea9e9c39b02dfa35feb（已获授权仅将设计推送origin2）。
诊断基线：8f359cfacb8b1726640f08473d39e69eac3f81ac。
本轮实现未提交、未推送、未部署Vultr，未安排整点试验。

## 协议、复用与旧账本隔离

扩展名forward-paper-opening-observation-v1，默认关闭。父forward-paper-v1、R13标的准入及旧实现字节保持不变。

| 组件 | 复用/差异 |
|---|---|
| Market.opening | 仍调用原函数，唯一目标1H柱、confirm0、成交量存在性、价格、15秒均不放宽 |
| fill | 调用原BTC或R13标的fill；增加观测策略身份与HTTP证据检查 |
| create_state | 先调用原父引擎，额外加入观测协议/审批指纹及独立trade_id/group |
| advance_bar / settle / feedback | 复用父引擎原函数，止盈止损/资金结算/结果可知时间不重写 |
| paper_timing.step | 逐候选调用原函数，保留SCHEDULED/PAUSED/MISSED/CANCELLED/OPEN语义 |
| R13 | 保留真实合约证书、ctVal/ctMult/tick/lot/minSz及资金费用验证；ETH/SOL不回退BTC |
| F1/F2、研究通道 | 只读，不改原记录、规则、模型流程或调度CLI |
| 新传输 | 独立公开GET客户端；复用原instrument/candles辅助函数及公开路径白名单，无全局猴子补丁 |

新账本在显式指定的新root下，按源注册目录哈希前缀和标的隔离。完整注册目录哈希仍绑定协议，短目录名冲突会因完整协议不匹配拒绝，不会混账。旧F3目录不迁移、不修改，不能将旧账本载入新引擎。
父paper_protocol_hash保留用于父引擎验证；新opening_observation_policy_hash、opening_approval_hash与新journal policy_hash分别表明扩展身份，不能假冒旧协议。事件仍保持三项安全字段False。

## 文件

- tradingagents/forward_paper_opening_observation.py：协议、显式审批、引擎组合、账本、paper/timely/status协调及独立CLI。
- tradingagents/opening_observation_transport.py：持久化HTTP预算预留与实际发送审计、单次发送、响应摘要及deadline。
- docs/forward_paper_opening_observation_v1.json：默认关闭配置。
- docs/forward_paper_opening_observation_v1.schema.json：配置Schema。
- docs/forward_paper_opening_observation_journal_v1.schema.json：协议/事件/独立账本Schema。
- tests/test_opening_observation_f31.py：确定性、隔离链路及兼容测试。
- 本文档和最终本地验收记录。

## 显式准入与默认关闭

init必须显式--enable。生产模式还要求实现代码已存在于所记录的Git提交且与文件一致；因此本轮未提交的开发代码不能直接启用生产root。这是有意的发布前保护，不会自动提交。
审批封存配置、原R1.2路径/预算、R13 profiles、代码指纹、Git提交、激活时间和命名空间。只允许BTC/ETH/SOL已启用profile；其他标的不能自动进入。
只有激活后新登记、父F3原本认可的前向候选可新调度。旧注册不会静默导入；已存在旧SCHEDULED/OPEN也不会迁移到新账本。代码或审批变化后拒绝混用原扩展账本。
F1/F2记录、条件事件及指纹仍由原_verified_inputs校验。WAIT、失败、无效、过期、非前向记录不会因新扩展取得资格。

## 新旧协议兼容矩阵

| 场景 | 旧forward-paper-v1/R13 | 新opening-observation-v1 |
|---|---|---|
| 默认命令与已存档样本 | 完全保留原行为 | 默认关闭，不读取为新账本 |
| BTC/ETH/SOL | 原标的协议和证书不变 | 复用原证书，另加扩展审批及激活后登记限制 |
| 首查目标柱缺失 | 保留旧单次行为 | 仅在同一15秒窗口的固定后续轮次补查 |
| 成交归属时间 | 父fill的实际observed_ms | 同样实际observed_ms，绝不回填边界T |
| 价格、费用、退出、资金费率 | 原计算 | 调用原计算，另附观测证据 |
| 旧账本/旧协议哈希 | 不变 | 拒绝加载，不自动迁移 |
| 旧四通道调度 | 不变 | paper/timely须显式选择独立CLI与root |
| 真实风险/模拟准入/执行许可 | 保持原语义 | 三项固定False，只有独立纸面评价资格 |

## 时间语义

T=边界前已持久化的未来计划整点，D=T+15000毫秒。
默认轮次最早为T+1、T+5、T+9秒；串行标的和前置校验会造成实际偏移，所有真实时间单独记录。
只有TARGET_BAR_NOT_VISIBLE这一结构明确的旧柱缺失结果允许后续轮次。目标柱存在但无效、HTTP/解析/准入/结构/重新验证失败均保留PAUSED证据，不当作成功；本版本不对这些错误做自动补查。

原fill已实现：
`T <= request_started_ms <= observed_ms <= fill_check_now <= D`。

新版保留它，并增加HTTP发送/接收/处理完成与候选attempt_id的一致性检查。第5秒取到有效柱时，entry_ms仍为实际observed_ms，不是T、首查时间或轮次计划时间。HTTP响应接收和可解析完成可能相差若干毫秒，均分别记录；原observed_ms是在get返回后完成有效读取的时间。

entry_reference仍是目标小时open，再使用父5bps手续费、2bps不利滑点和原精度/数量规则；它是纸面价格代理，不保证观测时刻实际可成交。不能把小时边界ts理解为逐笔交易时间。
重新验证必须在T前完成、fill时仍未到原有效期，不延长60秒TTL，不重新修改方向、预案或止盈止损。
首柱止盈止损不明的UNRESOLVED/保守处理、12根最大持仓及资金结算沿用父规则。资金费用仍要求实际entry_ms < fundingTime，不能把边界前未持仓的资金费率记入。

## 预算、截止与故障隔离

- 每候选最多3个持久化attempt，单边界最多3个候选、最多9个报价HTTP。
- 整窗口最多60个HTTP预算预留，包含时间、历史闭柱、合约及重新验证，而不只报价。实际发送不超过此上限；9个报价额度是子上限，不增加总额度。崩溃或发送前截止的预留仍消耗预算，不能把count直接解释为成功发送数。
- 公开GET单次发送，不隐藏重试；全局最小250ms发送间隔；单请求connect/read总socket预算最多1秒并受窗口剩余时间约束。
- 响应体最多1MiB；保存白名单query、HTTP状态、发送/响应时间、有限摘要及指纹，不保存认证头、Cookie或异常任意文本。
- HTTP401/403/429、明确限流API码锁存该窗口预算暂停，不连续重试限流接口。
- 在HTTP发送前持久化预算和intent，分别保存dispatch_reserved_ms、http_dispatched_ms及actual_http_attempts；后者表示客户端Session.send边界，不代表内核发包时间。若预留后已到截止时间，actual_http_attempts=0且不伪造发送时间；崩溃视为已消耗，不允许重用同一个发送序号或覆盖旧收据。
- 及时进程使用Linux SIGALRM截止保护；Windows本地不声称已验证Linux硬截止，deadline/晚响应拒绝由确定性夹具验证。未来部署另需一次性受控实测。
- 逐候选包装还会将意外组件异常记录为PAUSED及异常类型，不记录任意异常正文；损坏账本不会被自动修复或覆盖。
- 同一扩展root有跨进程phase互斥；每个候选在账本短事务内记录attempt，等待下轮时不持有其他标的账本锁。
- 重启读取OPEN状态便不再fill；发送前intent存在但无完整结果时保守阻止该候选继续补查，报告INTERRUPTED_UNCONFIRMED_ATTEMPT，不伪装完整响应。
- 已用尽轮次但仍未到D：保持SCHEDULED并带PAUSED/尝试耗尽记录；不提前把它称为MISSED。D后按原规则推进MISSED。预算/网络不足以完成维护检查时保持PAUSED，后续paper维护仍可按原规则处理，不能伪造成交。

## 四条通道的兼容方式

research和observe继续原R1.2/R1.4命令。旧paper/timely仍管理旧账本。
新版paper/timely通过独立CLI、独立root显式选择，不修改旧paper_instruments.py、paper_timing.py或R14 manifest来绕过代码哈希。
未来若同时保留两组，必须分别分配操作档案和虚拟资金，不能把同一市场候选的A/B结果当两个独立样本，也不能合并权益宣称收益提高。本轮未实现跨协议收益比较，未安装任何调度。

## Windows使用（代码验收后由用户授权发布/启用）

当前安全的只读状态命令，不创建或启用账本：
```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.forward_paper_opening_observation status --root D:\ta-f31
```

未来实现已审阅提交后，才可显式初始化（本轮未执行）：
```powershell
.\.venv\Scripts\python.exe -m tradingagents.forward_paper_opening_observation init --enable --root D:\ta-f31 --automation docs/automation_r12.json --instruments docs/paper_instruments_r13.json
.\.venv\Scripts\python.exe -m tradingagents.forward_paper_opening_observation paper --root D:\ta-f31
.\.venv\Scripts\python.exe -m tradingagents.forward_paper_opening_observation timely --root D:\ta-f31
```

建议Windows数据root使用短路径，避免旧F1/F2和原报告文件名加临时UUID导致Windows路径上限问题。以上不改变旧研究默认行为，也不授予实盘权限。

## 测试样本边界

纯财务内核测试使用FIXTURE命名空间。完整F1/F2防前视与来源校验测试需要模拟PRODUCTION形状，但所有输入都由测试生成、Git标记为ISOLATED_DETERMINISTIC_FIXTURE，这里只是测试审批git_commit字段的隔离标记，不是真实Git提交；源码审批校验仅在该夹具内替换，正式CLI拒绝这种审批。全部仅存在pytest的独立临时目录，不会读写真实用户档案，也不是正式前向绩效。
默认conftest阻止外部网络，HTTP响应为固定模拟数据；没有模型调用。本轮不以这些夹具表示Ubuntu实测已通过。

专项与兼容组合命令：
```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_opening_observation_f31.py tests/test_quote_probe_diagnostics.py tests/test_ubuntu_r14.py tests/test_forward_paper.py tests/test_paper_instruments.py tests/test_automation.py
```
实际结果：246 passed，235.45秒。包括本轮新增的逐候选意外异常隔离与CLI脱敏检查。

全量默认离线回归使用`python -m pytest -q`，继承项目默认`not integration`范围，不对缺失的可选依赖、POSIX权限或外部服务测试伪称通过。最终结果与基线对照记录见本机独立验收目录，不将原始测试日志加入Git。

## Vultr后续部署及整点验收方案（未执行）

1. 用户验收并授权固化F31后，以实际提交生成新的只读release和部署包；旧R14 release、账本和manifest保留原样。
2. 非root运行，新私有root，与现有F1/F2来源只读关联；先status/dry review确认默认未启用，再显式init新审批。
3. 不迁移旧样本。由激活后新研究形成合格候选；没有真实候选就报告零样本，不能用旧诊断或夹具制造正式交易。
4. 先独立一次性公开接口和deadline探针，再另行授权有真实候选的纸面准入试验。三标的每次发送、接收、fill决策、持久化和费用均审计。
5. 测1/5/9秒可见、持续缺失、网络异常、重启与资源竞争；任何晚响应拒绝。一次通过不代表长期可靠。
6. 本轮不连接Vultr，不安装新版、不安排整点、不运行完整研究，不进入D3.3/D4。

## 当前限制

- 新版正式前向准入、真实HTTP/操作系统调度、Linux实际截止及三标的真实成交结算尚待未来验收。
- D3.2.2是旧独立诊断的真实证据，不能作为本模块的实机通过记录。
- 原历史合约参数、开盘/资金标记价代理、首柱路径不明及小样本限制继续保留。
- 状态与报告复用父引擎；跨新旧观测协议的配对收益比较不在本轮实现。
- 未提交实现会主动阻止生产初始化；只允许本地隔离夹具测试。

execution_enabled=False / allow_new_risk=False / simulation_admitted=False。

## 最终本地验收（2026-10-25）

建议：**有条件通过本地工程验收**，未通过或声称完成新版Vultr生产时效验收。

- F31新增48项全部通过；专项及兼容组合246项全部通过（235.45秒）。
- 最终全量默认离线回归：2243 passed、5 failed、4 skipped、13 deselected、100 subtests passed（422.34秒）。未声称全绿。
- 5项失败在不含F31实现的b01436设计基线独立副本上逐项复现：ChatGPT认证2项、CLI升级2项、yfinance缓存隔离1项。没有据此修改本轮范围外源码。
- Windows跳过POSIX权限3项与未安装可选langchain_aws1项；13项外部integration未运行。未因此安装依赖或调用真实认证/模型服务。
- 原跟踪文件无diff；旧F3、R13、F1/F2及C.1核心源码与设计基线一致。旧paper策略指纹仍为e29998a373c1295791116b026a04f18c4067f5c14a1352cfad2a4b93066dbd64。
- 只读CLI实测返回DISABLED，未初始化生产root；实现仍未提交、未推送，AGENTS.md未改动或纳入Git。
- 本地验收JSON、最终JUnit与测试日志位于<USER_HOME>/.tradingagents/logs/f31_local_acceptance_20261025/，不进入Git。
- 新模块无真实HTTP、Vultr部署、整点试验或正式成交；没有以夹具或旧D322证据补充真实前向样本。

发布前置：用户验收实现并授权固化后，才能通过生产源码版本校验；随后另行授权Ubuntu部署与一次性整点试验。60个HTTP预算及串行预检可能让后续轮次没有足够余量，必须按实际deadline失败/暂停，不能保证三标的每次都完整执行三轮。
