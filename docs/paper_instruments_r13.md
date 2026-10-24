# R1.3 通用 USDT 永续合约纸面评价

基线：`4280003d92a634dd472c6e19938272648b422fb8`。仅研究和隔离假设评价；
`allow_new_risk=false`、`simulation_admitted=false`、`execution_enabled=false`。
观察资格和纸面评价资格均不构成账户交易授权。

## 兼容性审查与协议

原 `forward_paper.py` 的 `admission()` 限制 BTC，`Market.funding()` 拒绝其他标的，
`fill()` 的合约校验绑定 BTC；D.3.1 `verify_funding_evidence()` 使用 BTC 标识。
限制来自原 BTC 费用审计的验证范围，不能仅删除白名单。

`forward-paper-v1`、原 JSON/Schema、C.1、D 代码均不改写。BTC 仍使用原 F3 财务函数、
协议指纹及原账本目录。默认 R12 CLI 仍为 BTC-only；运行新的显式 R13 CLI 才加载扩展配置。

ETH/SOL 使用 `forward-paper-instruments-v1`，这是对父协议的**标的准入扩展**。
`ExtensionProtocol` 保存完整父协议参数、父指纹及单标的批准配置。配置与 Schema 分别是
`paper_instruments_r13.json`、`paper_instruments_r13.schema.json`。
策略、15 秒窗口、60 秒重新验证、12 根最大持仓、1000 USDT 参考名义额、10000 USDT 初始独立
权益、止损优先及第一根成交柱的保守处理均沿用父协议，不因波段研究定位而改成 1—3 天持仓。

## 复用方式与维护边界

`paper_instruments.engine()` 在私有依赖字典中绑定原 F3 的 **同一 Python code object**；
不修改源文件、不替换模块全局变量、不执行外部代码、不将 ETH/SOL 改名 BTC。
原生 `scan/create_state/fill/advance_bar/settle/report` 及账本追加算法被复用。
扩展只处理准入、实际合约校验、实际资金证据标的、来源过滤与扩展 Schema。
原 `fill` 中 BTC 字面参数由一个局部校验依赖接收，随后严格校验真实 `info.instId`；
用于数量和收益计算的合约字段始终保持真实标的。

这是适配现有文件指纹约束的技术边界，不能假设任意未来 F3 改动都兼容：
升级原引擎后必须重跑全部测试并审查依赖。代码、及时调度辅助代码、资金证据校验和单标的配置
进入扩展账本指纹。不匹配时拒绝加载，需要显式的新账本和版本审查，不自动迁移旧账本。

## 准入及合约规格

`enabled` 与观察池启用开关独立。每个允许标的必须有经过公开接口验证及确定性计算验收的
Certificate：实际 SWAP/linear/live/USDT、instFamily、ctValCcy、面值、乘数、最小张数、张数步长、tick。
来源为 OKX `GET /api/v5/public/instruments`，保存获取时间与公开验证证据指纹。
Certificate 是本地受审查配置证据，不是交易所签发或加密签名的凭证。

新准入保留原生完整的研究成功、plan_valid、PROSPECTIVE、质量、观察协议、F2 条件事件、
原始条件一致性和时间检查，仅用显式单标的批准取代 BTC-only 限制。
未批准标的拒绝；报告成功或观察池启用不能自动批准。未来 DOGE/XRP 无需新引擎，但须先用公开
数据和确定性计算验证，再人工审查新增配置。不能复制 BTC 面值。

重新验证复用原 `revalidate_live/compare_revalidation`，捕获原有合约查询而不重复请求。
当前规格必须与批准规格和冻结研究规格匹配；记录获取时间、来源、指纹。缺失、漂移、异常或
过期时不能成交。证据 TTL 不延长。当前证书不证明历史合约规格不曾变化。

## 单位、费用与成交

- 基础币/张 = `ctVal * (ctMult 或官方空值对应的 1)`；要求 ctValCcy 为对应基础币。
- 张数 = 向下取整到 lotSz 的 `1000 / 不利滑点后价格 / 基础币每张`；不足 minSz 拒绝。
- 基础币数量 = 张数 × 基础币/张；USDT 名义额 = 基础币数量 × 价格。
- 价格按原 tick 规则向不利方向取整。冻结止损、止盈不重新优化。
- 每个标的显式配置 Taker 5bps、滑点 2bps；它们是本地假设，不是账户真实费率。
  Maker 未模拟。当前版本成本数值保持父协议，不提供事后修改基准成本的参数。
- 资金费用使用 `funding-rate-history` 的 `realizedRate` 和实际 `fundingTime`，保留分页原始证据。
  不以预测值补实际值、不固定八小时；沿用原审计规则：整点结算、原始页严格倒序、完整分页链、
  覆盖交易两端、支持最大相邻结算间隔八小时。超出已验证范围时暂停结算。
- 结算币数量 × 对应小时成交开盘代理 × 实际费率，按多空方向记现金流。
  仍是 **TRADE_OPEN_PROXY_NOT_EXACT_SETTLEMENT_MARK**，不是精确历史标记价格。
- 资金上界证据尚未发布时保持 EXIT_PENDING，不伪造费用；补采单独标注。

F2 ENTRY_ELIGIBLE 不等于成交。准入和重新验证后先持久化未来整点安排，再在整点后的 15 秒内
取得当时有效开盘观测；入场前重新验证须在整点前完成。迟到、重启、补采不能倒签。
退出只使用已确认收盘连续 K 线，缺口暂停，不能用后续数据挑选有利价格。
无杠杆、保证金和强平验证；没有证明真实订单可成交或策略盈利。

## 路径、隔离、恢复与调度

沿用 R12 `AutomationConfig`、只读 F1 索引、F2 事件、暂停标志、`paper.phase` 互斥锁。
研究及观察命令保持原样，R13 仅替换显式 paper/timely 调用。

BTC：原 `state_root/f3/<source>`。
其他标的：`state_root/f3/instruments-r13/<source>/<instId>`。
每个标的/源独立权益和统计，不把各自的 10000 USDT 当作共享资金组合；不汇总成虚构组合收益。
重复注册、F2 条件、开仓、结算均由原身份和追加链防重。原 F1/F2 档案只读。
配置关闭标的后保留其档案与退出/结算；禁止新准入和成交。删除已有账本的配置会报错，应保留
disabled 配置进行审计。更改批准规格不能静默接管已有账本。

单次运行由子进程硬超时保护，paper/timely 与 R12 共享互斥，不与模型任务争抢同一任务锁；
另有零 LLM 调用、实际 HTTP 派发上限及每标的逻辑请求上限。结果与 meter 写到
`state_root/r13_runs`。发生错误只记类型，不记认证头、请求凭据。
及时观察仍受操作系统、网络和磁盘延迟影响，不承诺 15 秒 SLA。超额候选等待后续扫描，错过即 MISSED。
R12 原始监控不聚合新 ETH/SOL 账本，应使用下面 R13 `status`。

## Windows PowerShell

```powershell
cd D:\_01trading_agents
# 原生批量研究及 F1 登记；消耗模型预算，仅按需运行
.\.venv\Scripts\python.exe -m tradingagents.automation research
# 原生 F2 单次观察，无模型
.\.venv\Scripts\python.exe -m tradingagents.automation observe
# 显式启用经批准标的的纸面安排/退出/结算；不会下单
.\.venv\Scripts\python.exe -m tradingagents.paper_instruments paper
# 临近未来整点的有界及时观察；不安装后台服务
.\.venv\Scripts\python.exe -m tradingagents.paper_instruments timely
# 只读各标的状态及独立统计
.\.venv\Scripts\python.exe -m tradingagents.paper_instruments status
# 有界公开数据核验，无模型、无成交
.\.venv\Scripts\python.exe -m tradingagents.paper_instruments probe
# 只导出新目录，不安装；research/observe 仍走 R12，paper/timely 走 R13
.\.venv\Scripts\python.exe -m tradingagents.paper_instruments export --output "$env:USERPROFILE\.tradingagents\logs\r13_schedule_review"
```

可用 `--config`、`--instruments` 指定经审查配置。导出任务锁定两份配置指纹，改变配置须重新审查导出。
不自动安装 Windows 任务。Ubuntu 需项目兼容的 Python、已有 requirements、UTC/NTP、持久可写磁盘、
公开 OKX HTTPS 与模型所需的单独网络认证、单实例锁和非特权系统用户；无需交易所 Key。
导出的 systemd 服务使用 `%h/TradingAgents/.venv/bin/python`，配置文件须放入示例路径并审查修改；
本轮未部署或启用 timer。Windows .venv 不能直接复制到 Linux。

## 验收口径

真实公开查询与隔离夹具结果分别保存，不把夹具或已查看历史回测计入正式前向绩效。
若正式档案只有 WAIT/失败，应输出零候选、零成交、胜率/净收益未定义。
真实 ETH/SOL 正式候选、准时成交和资金结算链路仍需未来独立样本验证。
没有新增模型研究、没有证明多标的收益优势，不进入 R1.4。

官方参考：[OKX API V5](https://www.okx.com/docs-v5/en/)，Public Data / Get instruments、
Get funding rate history，以及 Market Data / Get history candles。接口当前样本不是无限历史覆盖证明。
