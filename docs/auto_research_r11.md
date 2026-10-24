# R1.1 多标的观察池与批量研究

基线分支 codex/forward-validation-f3；基线提交
43e0248aaa8a08028375086c83c34591480d30a6。
开发分支 codex/auto-research-r11。只做显式观察池研究和 F1 登记。
execution_enabled 始终 false，不调用账户、订单、F2 跟踪或 F3 纸面成交。

## 配置与原生流程复用

配置文件 `docs/auto_research_r11.json`，机器 Schema
`docs/auto_research_r11.schema.json`。JSON 为单一配置来源，不读取交易所个人自选。
instruments 默认 BTC-USDT-SWAP、ETH-USDT-SWAP、SOL-USDT-SWAP。
编辑列表即可新增/移除，enabled=false 禁用。空白去除并转大写，完全相同项去重，
同一合约 enabled 冲突拒绝。必须明确是 `*-USDT-SWAP`，不把现货或币本位标识
转换成永续合约。只处理配置中启用且在 max_symbols 范围内的标的；默认最多三个。

每项先用原 OKXPublicClient.instrument/validate_instrument 验证交易所、SWAP、
USDT 结算、linear、live、面值、tick/lot/min。研究开始时，原适配器再次检查
实际数据。预检可用不代表模型研究或交易预案有效。

控制器依次启动隔离 Python 子进程。子进程实际调用的是原生入口：

```
python -m cli.main okx-research --instrument <configured instrument> --interval 4h
  --multi-timeframe --trade-plan --taker-fee-bps 5 --forward-register
```

实现通过 runpy 在该子进程中运行原 CLI，未复制 Graph、分析师、预案、报告或
登记逻辑。默认 events=true，因此原 CLI 启用 Market/News/Sentiment 分析，
再进入已有 Bull/Bear、Research Manager、Trader、风险团队、Portfolio Manager。
enhancements 可开关 orderflow、market_checks、price_depth、events，复用其原
CLI 参数及质量降级。默认 WS 采样秒数为 0，开启相关开关不等于获得完整动态样本。
如配置采样，单项最多 10 秒，仍受任务总时限约束。没有长期订阅后台进程。

1d/4h/1h 的计算、统一单项 cutoff、预热、C.1 确认、预案及最终闸门均不变。
同批的不同标的是串行真实研究，不伪装为同一个毫秒 cutoff 的同步横截面对照。

“目标持仓 1—3 天”是用户的研究方向，**本轮未把现有冻结的 6 根确认窗口或
12 根纸面最大持仓规则改成 1—3 天**，也不声称该持仓期限已经被回测验证。

## 调度身份、幂等和恢复

调度 slot = 当前 UTC 毫秒按 min_interval_seconds 分桶。任务 ID 由版本、合约、
slot 确定，不含配置 hash：同一时段修改配置会报告冲突，不因此重跑模型。
slot 是防重复调度的标签，**不是行情 cutoff**。实际 cutoff、snapshot_id、
研究/登记时间均从真实 F1 记录读取。默认四小时一次，跨桶边界仍检查之前任务
创建时间，避免几秒内连续研究同一合约。

默认数据根：`~/.tradingagents/logs/auto_research/r11/production`：

- tasks/<task_id>/task.json：不可静默改写的配置、代码/Git/分支、预算授予和标的。
- tasks/<task_id>/events.json：状态追加 hash 链。
- tasks/<task_id>/meter.json：派发前持久化的调用/HTTP 计数和实际可得 Token 使用。
- tasks/<task_id>/receipt.json：工作进程阶段、报告位置、F1 意图标识和登记结果。
- validations：本 slot 的公开合约验证及使用量，重复调用复用，不重做相同预检。
- research/reports：原 CLI 生成的独立 HTML/JSON 等原生研究报告。
- research/forward_validation/f1：原 F1 Registry 正式登记；不是旧 F1 目录。
- runs：批量 JSON/HTML 汇总。没有自动提交本地运行数据。

schema/配置不接受密钥或交易权限字段。task/receipt/计数均有摘要校验，事件链可
追溯。原 F1 研究档案由原 Registry 再次验证。F1 intent_token 是本地登记意图 ID，
不是登录令牌；不记录 Authorization、Cookie、API Key 或网络请求内容。

全控制器锁防两个批次冲突，工作进程单独持锁。控制器中断但工作进程仍在运行时，
后续批次返回 ORPHAN_WORKER_BUSY，不启动第二个完整模型研究。单次任务超时会终止
自己启动的 Python 工作进程；没有默认后台进程或自动重试整项研究。

对 DISPATCHED/TIMEOUT/未知中断，只检查已有 receipt 和 F1 journal：如果研究已完成
但登记中断，调用原 Registry.register 继续登记，不重跑模型；F1 原时间规则会将
过晚恢复标为 LATE_REGISTRATION，不追溯冒充正式即时样本。无 journal、无可信完成
结果时记录 INTERRUPTED_UNKNOWN，保留状态并禁止该 slot 自动再调模型。
明确停留在 QUEUED、没有计数文件、从未派发的任务可以续派；不得把已派发但响应
未知的任务重置为 QUEUED。预检在发出 HTTP 前也预留整段预算，控制器中断不会将
预检消耗清零。同一 slot 已确认的额度/权限限制会在重启后继续阻断新模型调用。

正常 WAIT、候选、研究失败及质量不足保留原字段。REGISTERED 只表示已归档，
不等于研究成功、预案有效或允许新风险。模型构造/进程超时若早于原 CLI 报告生成，
可能只能保存失败任务/意图而没有 F1 研究记录；不得伪造一份完整研究来补齐登记数。

隔离夹具使用 FIXTURE/TEST_REPLAY；测试目录不进入生产统计。跨 namespace 的任务
复制会被拒绝。R1.1 幂等适用于共享同一批量根的调用；无法阻止用户另外手动启动
原 CLI、另一个根目录或另一台机器研究同一行情。实际市场样本仍应按标的/cutoff
识别，不能把不同配置或重复报告自动当作独立市场样本。

## 预算与错误隔离

默认上限：每项 900 秒、批次 2700 秒；每项 24 次逻辑模型调用、批次 72 次；
每项 240 次 HTTP 派发、批次 720 次。公开预检也计入批次 HTTP 预算。
配置更小上限时按剩余额度授予各项，不为后续资产重置全批预算。

新 budget 模块只在工作进程安装临时守卫，不修改原 SDK/Graph 源码或全局 MCP。
同步 LangChain 缓存生成/stream 入口计为模型调用（缓存尝试也计，保守口径）；
Requests、httpx/httpx2 的派发含 SDK 重试消耗 HTTP 预算。异步模型/HTTP 路径在
本版不支持，明确拒绝，不能绕过预算。HTTP 计数不是 TCP 包数或 WebSocket 消息数。
计数器在派发前落盘；未知中断按整个授予预算预留，不能误记零消耗。

单项子进程有外部墙钟超时，预检也由限时子进程执行；控制器用剩余批次时间决定
是否继续派发。总耗时统计包含等待；控制器本身的磁盘/进程清理开销可能略超预算，
不能当作操作系统级实时调度保证。默认只允许一个完整研究工作进程。

具体标的网络/模型/数据失败一般继续下一项；明确额度或账户/权限限制则跳过其余
模型任务并记录共享服务阻塞，不重复尝试昂贵调用。不增加原 SDK 的重试范围。
预算次数不是美元收费上限，实际 Token 仅统计服务完整返回的 usage，缺失时
usage_available=false；不将未返回用量解释为免费。不能从累计 Token 推断实际账单。

## 运行方法

```powershell
cd D:\_01trading_agents
$python = 'D:\_01trading_agents\.venv\Scripts\python.exe'
$cfg = 'D:\_01trading_agents\docs\auto_research_r11.json'
$out = "$env:USERPROFILE\.tradingagents\logs\auto_research\r11"

# 公开合约 + 三周期覆盖验证：不调用模型，不登记 F1。
& $python -m tradingagents.auto_research validate --config $cfg --output $out --coverage

# 显式开始串行批量研究 + 原生 F1 正式登记。
& $python -m tradingagents.auto_research run --config $cfg --output $out

# 读取状态、用量及已归档报告，校验 F1；不调用模型。
& $python -m tradingagents.auto_research status --config $cfg --output $out
```

停止运行后再次执行 run 不代表重新研究：已派发同 slot 任务不会自动重跑。
要得到新研究，应等待允许频率和新 slot；不要删除旧 task/meter 来绕过预算。
run 的 COMPLETED 只表示本批控制完成，必须查看各项 research_status、原登记
workflow/quality/origin 以及 notes。退出码 2 为配置/账本等不可用；正常汇总可能
包含失败标的、预算阻塞或 BUSY，不能只依据退出码判定市场研究成功。

本轮不安装调度器。建议使用较短的本地路径，避免原 F1 的 Windows MAX_PATH
限制；没有更改原 F1 原子文件规则或系统注册表。账本最多 5000 个任务，需显式
归档；本地 hash/锁不提供跨主机分布式事务或恶意整体重写防护。

## R1.2 / R1.3 接口及边界

可复用：Watchlist.enabled、Store.tasks/events、任务唯一 ID、Meter 快照及 summary
JSON。未来调度层只负责按时调用单次 run，复用相同根和锁；调度策略应另行版本化。
后续可从 `production/research/forward_validation/f1` 读取真实登记；是否进入 F2
仍由原观察协议决定，不以批量研究完成状态替代。F3 保持仅 BTC，本轮绝不调用它，
ETH/SOL 即使有有效候选也不能绕过 F3 限制。

原 V2.4 的项目专属来源不是无限全标的新闻库：BTC/ETH 有既有官方来源；SOL
项目专属历史事件覆盖不足时保留原降级，不新增或虚构 SOL 新闻源。

实际公开覆盖、真实模型结果、测试及版本指纹见 `auto_research_r11_acceptance.json`。
公开数据验证成功与完整多 Agent 成功独立报告；不宣称批量化提高交易准确率。

## 本轮实际验收

- 三标的官方公开验证成功：BTC/ETH/SOL 各 1h、4h、1d 返回 400 根有效已收盘柱；
  日线使用原 1Dutc；整个公开验证 54 次 HTTP 派发，0 次模型调用，没有生成 F1 样本。
- 真实完整研究只执行 BTC 一项，预算 24 次模型调用、200 次工作进程 HTTP、600 秒；
  实际 14 次、54 次、全批约 523.49 秒，另有一次批量合约预检 HTTP。
  服务返回的累计 Token 用量 494554；这是已报告用量，不是实际账单或美元估算。
- 模型复用当前配置 chatgpt / gpt-6.1-sol。真实结果 COMPLETED / AVAILABLE /
  data_quality=VALID，动作为 WAIT，plan_valid=false、allow_new_risk=false、执行=false。
  原生 F1 正式登记 origin=PROSPECTIVE，登记 ID
  `80453e38893be05f1a530b03f5c118d2bfe472106f2b89ee212ed48f3d9ae8a4`。
  实际 HTML/JSON、复制档案和登记指纹均重新读取验证；没有要求市场产生候选。
- 真实运行启动后补入三个控制器恢复边界修正：QUEUED 续派、预检预算预留、额度
  阻塞跨重启保持。没有改动该次工作进程已加载的研究/模型/登记逻辑；如实保存
  真实构建与最终代码的不同指纹，不冒充最终构建重新跑过完整模型。最终复查
  同 slot 因版本指纹差异拒绝重派，调用仍 14、登记仍 1，没有第二次昂贵尝试。
- 最终专项 33 passed（7.61 秒）；早期 F1/F2/F2.1/F3 兼容批次 171 passed（55.16 秒）。
  最终全量 2048 passed、5 failed、4 skipped、13 deselected、100 subtests passed
  （114.29 秒），覆盖上述补充修正。五项仍是既有 Windows 权限两项、升级命令
  两项、yfinance 缓存隔离一项，没有借本轮改动这些旧模块。
- ETH/SOL 只完成真实公开数据验证，未运行真实完整模型研究。三标的串行、异常
  隔离、预算和恢复由隔离夹具验证；不能宣称已实测全天候批量运行稳定性。

验收建议：**有条件通过工程验收**。没有安装调度任务，没有调用 F2/F3、私有
账户或订单接口。当前仍未验证 1—3 天新持仓规则、跨机器并发或长期无人值守。
原冻结规则、实验档案、股票研究功能保持原样；不自动提交或推送。完成 R1.1 后
停止，不进入 R1.2。
