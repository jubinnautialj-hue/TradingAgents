# F3：前向纸面评价、结算和审计

## 范围与独立协议

分支 `codex/forward-validation-f3`，基线
`872a8c8ffdc40a01e3ad712fd3ead32cd7e7c0cd`。
本协议为 `forward-paper-v1`，机器配置见 `forward_paper_v1.json`，结构见
`forward_f3.schema.json`。先前 `forward_f3_preflight.*` 是保留的开发前阻塞记录，
不应被当作当前实现状态；本说明对应用户后续明确授权的独立前向时序。

所有 `simulation_admitted`、`allow_new_risk`、`execution_enabled` 仍固定 false。
`paper_evaluation_eligible` 仅代表进入隔离的假设性评价，不是风险许可或订单。
没有 Agent、新策略、实盘接口、自动调参、后台服务或自动 Memory 学习。
首版实际资金费用审计复用 D 的 BTC 校验器，**只准入 BTC-USDT-SWAP**，其他合约
明确拒绝，不能先开仓再承认无法核算资金费用。

## 复用及改动边界

- F1 Registry.load/events/tracking_status：读取封存、校验登记/报告与行政状态。
- F2 EventStore.load/triggers：验证原观察链及代码、连续性、时间、状态重放。
  不调用会写观察资格审计的 read_condition_events，不追加或迁移任何 F2 事件。
- F2.1 assess：保留研究资格、来源、质量、原预案校验与风险许可隔离。
- C.1 evaluate_signal：原突破/回踩、收盘失效及到期逻辑，源码不变。
- perpetual_plan.revalidate_live/compare_revalidation：复用六项公开市场核验、
  合约规格对照、原始 ATR/价格/OI/资金预测/盘口漂移阈值；不修改阈值。
- OKXPublicClient/Collector/ScanLimits：复用公开白名单、SSL、重试、分页、
  已收盘 OHLCV 检查及预算。只新增 `/api/v5/market/candles` 公共 GET 白名单路径。
- round_tick、multiplier、funding_cashflow、verify_funding_evidence：复用价格精度、
  合约单位、方向资金费用和历史分页证据校验。没有改动原 D 引擎。
- 原生 Memory 的结果可知时间思想被保留，但 D.5 固定 OFFLINE_SIMULATION 来源，
  本轮不直接写入；提供独立只读 feedback 接口。

## 准入和成交时钟

只有 PROSPECTIVE、研究完成/模型可用、plan_valid、原研究证据及观察资格有效、
封存指纹正确、冻结条件完全一致、F2 连续及时 ENTRY_ELIGIBLE、尚未过期且当前
结构/重新验证通过的记录，才允许纸面安排。WAIT、失败、历史导入、晚登记、
TEST_REPLAY、补采条件事件和无效预案不能成交。有效观察也不会自动安排。

状态：SCHEDULED → OPEN → EXIT_PENDING → SETTLED；此外有 CANCELLED、MISSED、
UNRESOLVED。它们是 F3 账本状态，不改写 C.1/F2 状态。

1. 条件确认时间、F2 数据可用时间、F2 决策可用时间全部保留。
2. 完成独立准入和公开重新验证后，选择严格晚于所有完成时刻的第一个 UTC 整点。
   即 `(completed_ms // 3600000 + 1) * 3600000`，刚好在整点完成也选择下一整点。
3. 先原子封存安排，再记录已经完成持久化的 ARMED 证据。持久化时间必须早于计划
   开盘。中断在 ARM 前不会被推定为已经及时安排，重启不能补出过去的安排。
4. 原计划开盘不自动滚动。开盘前 60 秒内可重新采集原公开证据，保持原阈值；
   实际核验完成必须早于计划开盘，开盘观测时仍在 60 秒 TTL 内，且未过原预案期限。
   不延长原研究 TTL，也不拿新的快照替换原研究证据。
5. 开盘时先重放从原确认首柱到最新收盘柱的完整 C.1 前缀。缺柱或失效不能成交。
6. 只有预先安排后的 15 秒交付窗口内取得当前 1h K 线开盘引用，才接受价格观察。
   请求开始不得早于计划开盘；K 线 ts 必须匹配、confirm=0、已有实际成交量。
   只消费 o、ts 及成交存在性，未收盘 HLC 不进入指标或退出判断。
7. **假设成交时间记录为价格实际接收时间，不倒签为整点**。整点仅是预先冻结的
   开盘参考边界；成交价是及时观察到的开盘价加冻结不利滑点，是有明确交付时差的
   K 线纸面假设，不保证收到价格时仍能真实成交。
8. 超过 15 秒、已确认历史柱、无成交、缺价、未及时核验、到期、结构破坏均不生成
   成交。错过开盘标记 MISSED；无法确定的价格证据保留暂停，之后错过即失效，不能
   事后补采开盘来补成交。该交付窗口不是把过去的决策时间改成更早。

### 与历史 D 的差异

D 在历史回放中以确认收盘边界的下一柱开盘加不利滑点成交。F3 按用户授权先等待
实际确认及核验完成，再提前安排未来整点；其实际接收/假设成交时间另行记录。
不能声称两种成交时序相同，也不能混合计算收益或追溯修改 D 的历史结果。

由于 F3 接收可能晚于整点数秒，入场首根小时柱的高低点不一定都发生在接收之后。
若首柱只在高/低点触及止盈而收盘未在目标外侧，记录 UNRESOLVED，不拿可能发生在
入场前的有利波动获利；首柱止损采用明确的保守假设，可能包含入场前极值。
双触发仍 STOP_FIRST。该精度限制显式保留，未改动 D 或 C.1 的规则。

## 持仓、成本与资金费用

- 各研究配置组独立初始权益 10000 USDT，固定名义上限 1000 USDT，同组只允许一个
  活跃安排/持仓/待结算或无法确定的敞口。冲突候选记录拒绝，不悄悄删除。
- 研究方向、入场条件、止损、第一止盈、合约面值、tick/lot/min 不被模型修改。
  张数按 D 的 Decimal 向下取整公式，双边 Taker 各 5 bps，不利滑点各 2 bps。
  Maker 未使用，不表示账户真实费率或排队成交能力。
- 止损开盘跳空取更不利价格，止盈不取得超出目标的改善；同柱先止损。
  最大持有期限沿用 12 根，从计划入场柱计，按后续开盘退出。后续开盘价在已收盘
  柱中确认，记明实际获知时间。没有扫描窗口结束就强平的行为。
- 原研究假设到期阻止新入场，不会自动平掉已存在的纸面持仓；已有持仓按冻结退出
  规则继续处理。模型后续失败不抹去已登记的候选或持仓。
- 净损益 = 含滑点价格的方向毛损益 − 双边手续费 + 实际资金现金流。
  单列滑点影响，不二次扣除。另输出不含费用/滑点的毛损益口径。
- 仅使用 realizedRate 和实际 fundingTime。复用 D 的分页原始证据、严格游标、
  结算事件前后括取及可支持间隔检查；不硬补每八小时事件。当前校验器不支持亚小时
  结算，也不能凭 REST 自证交易所档案绝对无遗漏，限制必须保留。
- 历史实际费率未覆盖完整时停在 EXIT_PENDING，不把缺费用当零费用。
  资金计价采用已披露的结算小时交易开盘价代理，不冒充精确历史标记价格。
  新开仓不参与更早的整点结算，计划最大持仓开盘退出前结算；普通柱内退出不计入
  下一整点持仓。沿用 D 的保守小时级结算口径。
- 价格退出时间、实际已知时间、资金档案获取时间与最终结果可知时间独立记录。
  后续补采可恢复退出/费用核算，但明确 backfill，不允许补造原始实时入场。

## 账本与幂等

F3 必须使用与 F1/F2 不重叠的新目录。production 与 fixture 分开。
`production/ledger.json` 是权威链；事件包含协议及代码指纹、唯一 ID、UTC 毫秒、
registration/research 关联、前事件 hash 和完整状态证据。F1/F2 封存内容只读。

全账本使用原 files.locked 的跨进程锁。每次使用 F1 atomic 原子提交，成功后才
更新内存事件数组；中断和重复请求不会产生重复费用或成交。处理水位在状态的
cursor_ms 内，不能分别先更新游标再更新成交。扫描调度游标独立，只影响轮转，
不是会计真值。活跃持仓优先且轮转，其他记录也轮转，避免前一批 WAIT 永久阻塞。

代码或协议变化拒绝静默续接旧账本。早期开发探测目录保留，不自动迁移。文件上限
32 MiB、20000 事件，超限要求显式归档；不是无限容量数据库。局部文件 hash 链
不能防止有权限者整体重写/截断，也不保证网络共享盘/跨主机一致性。

Schema 覆盖 PaperProtocol、PaperJournal、PaperState、PaperReport。载入时还执行
事件/状态归属、链顺序、指纹及生命周期检查，原输入每次重新通过 F1/F2 校验。
机器协议由实际模型导出，专项测试检查 Schema 与实现一致及冻结成本一致。

## 报告、对照和反馈

每次成功扫描在 runs 下保存独立 JSON/Markdown/HTML。JSON 为权威结果，保留
逐笔状态、行情证据、原预案/F2 条件、数量、费用、资金原始分页证据、成本假设、
模型/研究配置、Git 与源码指纹。错误仅存脱敏类别，不记录请求头或凭据。

研究层分别统计完成 WAIT、失败、有效候选、观察资格、条件触发及 F2 失效/到期。
失败记录即使原 action=WAIT 也不被当作正常观望。提供实际分母与记录截断标志。
交易层分别统计已成交、未结算、已结算、胜负、Profit Factor、费用及资金现金流。
零成交时净收益、胜率、收益比为 null，不将尚无收益数据写成已证明的零收益策略。

提供已结算权益曲线与其回撤，以及费用全部已知后重建的已结算交易小时收盘权益/
回撤。后者显式标明 reconstruction_known_at_ms，不能作为更早研究输入。
未结算/无法确定敞口存在时 complete_net_equity_available=false，不将不完整的
已结算曲线冒充完整组合风险。MAE/MFE 是小时柱极值上界，包含首柱时序不确定性。

compare_pair 先检查 PROSPECTIVE 来源、同 pair_id/标的/cutoff、同计划评价窗口、
模型及成本协议；不同配置组各自处理持仓冲突，不直接删除拒绝交易计算伪组合。
当前只输出可比性和配置差异，不宣称增强版表现更好。没有真实配对时输出空列表。

feedback(states, as_of_ms) 只输出已经 SETTLED 且结果已知的同命名空间记录。
它是给后续 Memory/Reflection 的只读接口，不自动写旧股票/D.5 Memory、不调用
模型、不学习、不修改策略。未结算和失败不是成功规避损失。

## Windows 手动运行

```powershell
cd D:\_01trading_agents
$python = 'D:\_01trading_agents\.venv\Scripts\python.exe'
$f1 = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f1'
$f2 = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f2'
$f3 = 'C:\Users\jerry_su\.tradingagents\logs\forward_validation\f3_v1'
# F2 先按原协议观察；它不保证当前有候选或有条件触发。
& $python -m tradingagents.forward_tracking scan --registry $f1 --output $f2 --max-seconds 50 --max-requests 8
# F3 单次运行，不调用 LLM，不需要 API Key。
& $python -m tradingagents.forward_paper scan --registry $f1 --f2 $f2 --output $f3 --max-seconds 180 --max-requests 40
# 离线重新校验账本和输出评价（不重新获取行情）。
& $python -m tradingagents.forward_paper report --registry $f1 --f2 $f2 --output $f3
```

scan 由子进程监督运行时限；默认最多 40 次逻辑 GET，每次原客户端最多三次 HTTP
尝试。每扫描最多 200 登记、20 处理对象、每行情/资金范围三页；超限或缺柱暂停。
扫描无需 Codex 在线，不会默认启动持续服务。report 不联网。
默认 200 份登记上限意味着扫描结果可能只是选定文件集合；范围外的新登记不会被
该次统计覆盖。活跃持仓优先加载，集合内做轮转。大量登记需显式分片或通过 Python
ScanLimits 提升 max_records（上限 2000）；本版 CLI 不承诺无限历史登记全量统计。
返回码：0 扫描/报告完成（包括 BUSY 或零样本），3 外部数据/协议不可用，4 硬超时。
单次摘要 COMPLETED 不表示有成交；必须同时查看逐项 PAUSED、MISSED 及覆盖指标。

## 调度建议（未安装）

Windows 任务计划程序可复用上述单次命令，工作目录设置为项目目录，禁止并发新实例。
必须既有候选发现/持仓扫描，也有计划整点前的重新验证和整点后的及时观察。
例如部署者可以安排每小时 59:30 和次小时 00:01 的两次任务，再辅以持仓扫描；
这不是采集时效保证：冷启动、网络、F2/F3 并发和任务抖动都可能错过 15 秒窗口，
此时必须接受 MISSED，不能临时放宽协议。权限、代理和日志轮转需部署环境自验。

Ubuntu 预留 pathlib/fcntl 与外部 cron/systemd timer 调用相同单次 CLI；本轮没有
Linux 实测，没有部署/安装调度器。不能直接复制 Windows 虚拟环境过去。

## 验收范围

测试及公开数据结果见 `forward_f3_acceptance.json`。确定性夹具证明工程行为，
不能填入生产绩效。正式档案目前只有一份成功 WAIT 和一份失败，无有效候选；
因此真实前向成交与结算尚未得到端到端验证，不宣称盈利能力。
保留原全量测试已披露的 Windows 失败，不为使本轮全绿而改动无关功能。

官方免费数据依据：[OKX API V5 官方文档](https://www.okx.com/docs-v5/en/)，
使用公开 time/instruments/candles/history-candles/资金历史以及原公开重新验证接口。
confirm=0 的当前柱只用于及时开盘参考；正式收盘计算仅使用 confirm=1。

## 本轮实际结果

- 最终 F3 专项：51 passed，23.86 秒。
- F1/F2/F2.1/C.1/D 与当时 F3 的兼容批次：192 passed，126.55 秒；随后增加三项
  测试，由最终专项及全量回归覆盖，不将早期批次误称为最终 195 项的单独运行。
- 最终全量：2015 passed、5 failed、4 skipped、13 deselected，100 subtests passed，
  133.28 秒。五项为既有 Windows 问题：ChatGPT 私密文件权限两项、升级命令两项、
  yfinance 用户目录缓存隔离一项。本轮没有修改这些无关模块。
- 最终真实 F3 单次扫描及离线 report 均成功；正式档案 2，完成 WAIT 1、失败 1，
  候选 0、成交 0、结算 0。没有 LLM 调用，净收益/胜率/Profit Factor 为 null。
- 独立公开接口验证：5 次逻辑 GET，当前 K 线接口返回 confirm=0；2 根已收盘 1h
  柱通过校验；10 次实际资金结算记录可解析，本次返回间隔均 8 小时。没有据此
  假设其他时期也固定每八小时，更没有把探测价格用作生产成交。
- 9 个受保护模块/协议与基线比较一致，146 个原始冻结实验文件与原验收 hash 一致。
  旧源码唯一改动为 OKX 公共 GET 白名单新增 candles；其他新增文件均为 F3。

最终建议：**有条件通过工程验收**。真实前向成交/结算、精确资金标记价格、Linux
部署及长期调度时效仍未实证；仅 BTC、15 秒交付窗口和小时级价路径限制保持明确。
不能据此宣称已经证明盈利能力或具备实盘准备程度。验收后停止，不安装调度器，
不自动提交、推送或扩展交易功能。
