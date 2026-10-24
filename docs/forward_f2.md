# TradingAgents F2：未来已收盘行情跟踪

## 范围与当前权限阻塞

基线 `3bec3dfd0feebeafffd7f52a209c2f85d5c26a29`，分支 `codex/forward-validation-f2`。
F2 只观察条件，不新增Agent、不调用LLM、账户或交易接口，不成交、不结算、不计算收益，execution_enabled始终false。
所有F1源码、协议/Schema、研究档案与C.1/D冻结规则保持原样。

**本轮要求的严格权限筛选与现有F1存在兼容性阻塞：PerpetualResearchResult.allow_new_risk在现有Schema中固定为False，而本轮明确要求allow_new_risk=False不得进入模拟入场候选池。**
因此即使F1的plan_valid和simulation_pool_eligible都为true，F2也仍拒绝该记录。不得把F1的“研究预案有效”当成新风险许可。
本轮实现安全入口、独立跟踪引擎和接口，并以隔离FIXTURE验证状态流转；**未贯通现有正式F1候选的生产触发链路**。
若后续希望“只读条件跟踪”与“允许新增风险”分离，必须另行审核版本化协议/权限语义，不在F2擅自修改F1或改写旧记录。

## 原有能力复用

- F1 Registry.load/events/tracking_status：读取原封存、协议、档案指纹及行政状态。
- F1 FutureBarObservation：UTC已收盘柱、归属及实际可用时间结构。
- C.1 PlanTiming/evaluate_signal：原突破/回踩、确认窗口、止损结构失效、下一根开盘和到期规则。
- OKXPublicClient.get：只读公开GET白名单，SSL验证、禁止跳转、原限流/重试、超时和无账户认证设计。
- okx.validate_candles：九字段、confirm、UTC整点、OHLC/成交量、缺柱及新鲜度检查。
- files.locked：Windows msvcrt / Linux fcntl跨进程锁。
- F1 atomic/digest：原子文件替换和规范化SHA256，不创建数据库。

## 接口与运行

```powershell
cd D:\_01trading_agents
$python = 'D:\_01trading_agents\.venv\Scripts\python.exe'
$registry = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f1'
$output = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f2'
# 单次扫描：无需Codex在线，不调用模型；--output为统一的F2生产档案目录。
& $python -m tradingagents.forward_tracking scan --registry $registry --output $output --max-seconds 180 --max-requests 8 --max-records 200 --max-candidates 20 --max-pages 3
# 可选：公开接口连通性验证，不绑定研究、不产生信号样本。
& $python -m tradingagents.forward_tracking scan --registry $registry --output $output --probe-market BTC-USDT-SWAP --max-seconds 180 --max-requests 6
# 专项测试，不访问交易所。
& $python -m pytest tests/test_forward_tracking.py -q
```

CLI主入口以子进程运行内部worker，超时强制停止；worker不启动其他子进程或后台服务。不要直接调用内部_worker绕开监督。
退出码0=扫描完成（也可能没有候选或另一实例忙碌），2=存在档案/跟踪拒绝错误，3=公共数据或扫描不可用，4=硬运行时限耗尽。
Python `scan()` 是合作式预算接口；嵌入别的程序时调用方还需提供进程运行时监督，不能把socket读超时误称为整个扫描的硬截止时间。

默认最多200个F1记录、20个合格候选、8次逻辑GET、每范围最多3页、CLI180秒。每次原GET内部最多3次HTTP尝试，所以8次逻辑GET不等于最多8次底层HTTP尝试，最坏上限为24次。
每份F1登记和复制档案文件各限制32MiB。未处理记录/候选在摘要中列明数量，并用独立调度游标轮转，避免始终只扫前一批。

## 筛选与时间规则

必须同时是PROSPECTIVE、workflow COMPLETED、模型AVAILABLE、OPEN_LONG/SHORT_CANDIDATE、plan_valid、数据VALID、F1池合格、行政QUEUED，且登记和原original_result两处allow_new_risk均为true。
WAIT、研究失败、无效预案、历史导入/TEST_REPLAY及权限false分别记录排除原因，不产生条件事件。新发生的其他研究/模型错误不会改写已登记的旧研究。
原F1报告搬移不影响封存档案；复制档案、原登记或事件链损坏则拒绝推进。F2不读取后续研究、Memory/Reflection或实时衍生品来改写原判断。

只跟踪C.1规定的1h确认柱，不重新计算1h/4h/1d趋势或移动原研究cutoff。
起点必须等于原C.1 ceil(cutoff/1h)并不早于正式登记，首根检查时间必须为其收盘；错过首柱的候选不能补用登记前行情。
正常状态为PENDING_BREAKOUT→PENDING_RETEST→ENTRY_ELIGIBLE；收盘破坏原止损结构为INVALIDATED，到期为EXPIRED，均复用原函数。
数据尚未到首根收盘或没有新柱时，记录WAIT操作事件，但不把生命周期的PENDING_BREAKOUT改成交易动作HOLD。
数据错误时operational_status=PAUSED，与生命周期状态分开；不制造有效方向。

每根柱只用截至该柱收盘的前缀调用C.1；记录signal_as_of_ms（条件确认时刻）以及实际fetched/available/recorded时间。首次收集后知晓的条件不得伪装成当时已观测。
ENTRY_ELIGIBLE是技术条件确认，仍requires_revalidation，不表示全部OI/盘口/成本/入场前风控已复核，更不是模拟成交。

## 获取、缺口与补采

[OKX官方API文档](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks-history)提供公开history-candles；沿用1H及九字段响应，只使用confirm=1。
以公开服务器时间固定本轮行情cutoff，校验本地时钟偏差不超过30秒。实际采集发生在服务器时间查询之后；本轮不会使用查询cutoff以后的新柱。
从最后已提交BAR_OBSERVATION之后增量取范围，按after向过去分页。只接收完整连续的所需范围；相同重复行去重，不同内容同时间戳拒绝；乱序按UTC排序，未收盘/范围外行剔除。
缺首柱、中间缺柱、最新柱延迟、页数不足或API故障均暂停整批推进，不以插值填充，也不提交该不完整批次的前缀。
已提交连续观察就是持久缓存；相同instrument/start/end在同一扫描内共享一次获取。中断在最终提交之前的获取可能会重做，但不会重复记事件。

实际采集晚于柱收盘90秒（默认）标记BACKFILL；90秒仅是交付延迟分类，不更改任何研究TTL或C.1策略条件。首次观察链中任一柱为BACKFILL，就不输出可供F3消费的生产条件事件。
实际采集和处理晚于到期，不恢复已过期预案；只记录日历到期及未知观察覆盖。
到期是确定的时间边界，不等于“期间从未触发”。观察不足时coverage=UNKNOWN_UNOBSERVED，不给出过期未触发的事实标签。

成交量分别保留volume_contracts（张）、volume_base（币数量）、volume_quote（USDT），不跨单位合计，也不据此计算下单数量。
不采集当前OI、盘口、新闻或宏观事件来填补这些未来K线。

## 持久化、审计和并发

F2 output目录与F1分开，PRODUCTION与FIXTURE也物理分开：

- production/<registration_id>.events.json：唯一处理水位，只有已经提交的连续BAR_OBSERVATION计算在内。
- fixture/：仅测试材料，不能经生产读取接口输出F3条件。
- runs/<摘要指纹>.json：扫描时间、公开行情cutoff、排除分类、故障、预算、代码指纹和可选连通性样本。
- scan_cursor.json：只用于公平轮转调度，不是生命周期真值或独立记账水位。

同一output整体扫描非阻塞锁，候选另有独立跨进程锁。定时任务应统一使用同一output；不同output不是同一个跟踪账本，跨主机/网络文件系统一致性未验证。
每次提交保留全部旧事件对象，通过临时文件fsync和原子替换整条链；不会分别先提交cursor、后提交证据。
事件唯一ID由registration_id、事件类型及柱/终态标识确定，观察和转换有不同ID。相同柱重复扫描不产生重复观察/转换；候选过期或失效后不再激活。
事件有版本、research/registration标识、方向、协议/快照/结果/原登记指纹、UTC记录时间、行情cutoff、来源、证据指纹、previous_hash和event_hash。
恢复时校验归属、链顺序、原始候选指纹、观察策略和F2代码指纹，重放C.1核对转换。候选、代码或观察分类策略变化不能静默续接旧链，需要明确迁移/新版本。
历史调度游标丢失不会重复推进状态；原子提交之前崩溃则整批重新获取，并如实标记补采延迟。
本地哈希链不是外部签名/WORM：不能防恶意整体重写/截断，也不保证断电硬件级持久性。

## F3只读契约

`read_condition_events(registry_root, output, registration_id)` 再次检查F1权限、封存指纹及行政状态，然后返回ConditionTrigger。
结构定义见forward_f2.schema.json；包括原research/registration、协议和研究快照、方向、冻结原条件、确认柱、signal_confirmed_ms、observed_available_ms、decision_available_ms、原C.1 next_tradable_ms、到期、状态变化、连续性和幂等ID。

只有生产链、连续有效且没有补采前缀、处理仍在期内的条件才可导出；夹具始终不可导出。
字段is_simulated_fill=false、allow_new_risk=false、execution_enabled=false、requires_revalidation=true。
**next_tradable_ms是C.1的理论下一开盘边界，decision_available_ms可能更晚。F3不得据此倒签到已经过去的开盘价；本轮不解决成交时延、费用或结算。**
已有条件事件只说明当时确认过技术条件，F3还必须检查之后是否失效/到期和原协议的再验证要求，不能直接当作下单或成交指令。

## Windows定时任务建议（本轮未创建）

1. 在任务计划程序新建任务，使用有权限读取F1档案和写F2目录的Windows用户。
2. 操作程序为D:\_01trading_agents\.venv\Scripts\python.exe，起始目录D:\_01trading_agents。
3. 参数使用上述单次扫描，不加--probe-market；推荐每1分钟触发，--max-seconds 50，避免每5分钟才采到上一整点而被误认为及时观测。
4. 设置“不启动新实例”。可勾选错过后尽快运行；中断后补采会明确标记BACKFILL。
5. 若要求退出登录也运行，在任务计划程序本地配置对应Windows登录权限，不向Codex提供密码。Python/网络必须独立于Codex存在；若代理依赖登录用户进程，需要自行确认其可用性，本轮不改全局代理。

没有默认后台服务、轮询线程或Codex自动化。停止/禁用该系统任务即可停止周期扫描，单次CLI仍可手动运行。

## Ubuntu预留（未部署）

依赖现有项目环境、pathlib及原fcntl锁，无Windows专属执行依赖；本轮只在Windows测试，Linux部署仍需验收。
Linux使用自己的Python虚拟环境，不能复制Windows .venv。将登记和output目录放在有备份且权限受控的本机目录，部署只读市场访问，不需要交易所API密钥。
可在管理员确认后配置cron，例如：

```cron
* * * * * cd /opt/TradingAgents && /opt/TradingAgents/.venv/bin/python -m tradingagents.forward_tracking scan --registry /srv/tradingagents/f1 --output /srv/tradingagents/f2 --max-seconds 50 --max-requests 8 >> /srv/tradingagents/f2-cron.log 2>&1
```

日志轮转、目录备份及调度安装是部署事项，本轮未实际创建cron/systemd任务。

## 验证边界

专项夹具覆盖多空确认、仅突破、失效、到期、缺口暂停/补齐、晚采、未来柱、乱序/重复、分页预算、重启/中断、Windows双进程、事件/原F1篡改和权限隔离。
真实扫描仅证明免费公开时钟/已收盘K线和正式档案排除链路成功，**没有真实合格候选、没有真实条件触发、没有模拟成交或收益**。
全量回归及最终结果见forward_f2_acceptance.json；本轮不修改旧验收报告。


## 本轮实际结果与建议

F2最终专项40项通过（11.57秒）。全量非联网回归1946 passed、5 failed、4 skipped、13 deselected、100 subtests passed（85.72秒）。
五项仍是之前已披露的Windows问题：两项ChatGPT私密文件权限、两项升级命令及yfinance home缓存隔离；本轮不修无关功能，不声称全量全绿。
最终真实单次扫描使用当前代码：2026-10-24 UTC06:58:15.791（北京时间14:58:15.791），两次逻辑GET，公开时钟与两根已确认BTC 1h柱通过校验。
正式档案中一条RESEARCH_FAILED、一条WAIT均被排除；合格候选0，生产跟踪事件0，条件触发0，模拟成交0，模型调用0。
探测K线是单独连通性材料，没有绑定登记，不能算研究登记之后的真实触发样本；真实WAIT的首个潜在确认柱尚未收盘，也没有回填其之前行情去制造触发。
所有F1源文件、原登记/报告指纹、协议以及146个冻结实验文件仍与原验收清单匹配，冻结策略指纹不变。

建议：**有条件通过有限范围工程验收**。单次扫描、确定性状态机复用、独立审计、预算/恢复/并发/补采隔离及公共数据接入已验证。
**生产有效候选端到端链路仍受F1 allow_new_risk=False契约阻塞，不应宣称真实前向条件跟踪已经全面投入使用。**
下一步需先由用户决定是否另行审查“研究观察资格”与“新增风险许可”的版本化分离方案；不能直接把False改True或回写旧登记。这个决定不在本轮F2自动执行。
F3尚未开发；本轮没有真实ENTRY_ELIGIBLE事件、成交结算、盈亏或统计优势证据。

新增文件仅为tradingagents/forward_tracking.py、tests/test_forward_tracking.py、docs/forward_f2.md、docs/forward_f2.schema.json、docs/forward_f2_policy.json、docs/forward_f2_acceptance.json。
既有源码无修改，AGENTS.md原样保留且未暂存；没有安装付费服务、创建计划任务、commit/push或自动进入F3。
