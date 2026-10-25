# D3.2.1 整点K线可见性与逐标的故障隔离

分支codex/d31-quote-probe-diagnostics；HEAD01b82f435bb3631d3325e3a440b356d32bbb1312。
本轮只更新独立诊断模块与专项测试，新增本说明；原已跟踪文件diff为空。原D3/D3.1/D3.2结果、已验收release及旧诊断副本不覆盖。

## 既有故障结论

D3.2的BTC请求为GET https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1H&limit=2。
实际HTTP200、34ms、零重试，整点后1.034秒返回的最新柱仍为UTC00:00，而目标为UTC01:00。原Market.opening在forward_paper.py:254拒绝，是目标柱匹配数量不为1；不是接收超出15秒。
原run_probe遇首个异常即退出，ETH/SOL未执行。D3旧SOL的确切根因仍未知。

## 官方接口核验

官方来源：[OKX API V5 Market Data / Candlesticks](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks)，本轮通过官方文档搜索内容核验。完整页面过大，网页打开工具无法整页加载；未用第三方文档补充事实。

- candles是当前K线接口，history-candles是另一个历史接口；现探针没有误调用历史端点。
- 1H有效，limit2有效；没有传after/before，无历史翻页。未增加未公开参数或缓存绕过参数。
- 九字段依次为ts/o/h/l/c/vol/volCcy/volCcyQuote/confirm；ts是开盘毫秒时间，confirm0未收盘、1已收盘；永续vol为张数。
- 官方列出candles限速40次/2秒/IP；市场数据由多个独立缓存服务提供，后一次请求可能返回更旧数据；未承诺整点后1秒必定发布新柱。官方也提醒首柱可能不完整，不应无节制重复轮询。

因此缓存差异和生成/分发延迟是合理候选，尚无证据区分。既有requests适配器没有本地响应缓存，每次新建Session；不能由此排除交易所或中间节点缓存。未记录缓存节点身份/发布时间，不宣称已证实缓存故障。
UTC/北京时间转换正确：09:00北京时间=01:00UTC；目标epoch向下取整3600000毫秒。1H不需要把日线UTC+8定义套进来；未发现分页、周期或时区参数错误。K线开盘时间不是逐笔成交或API发布时间。
没有添加ticker诊断：现证据已定位到缺少目标柱，ticker不能证明该柱已发布，也不能替换F3开盘价格来源。

## 实现与版本

更新tradingagents/quote_probe_diagnostics.py为public-quote-diagnostics-d321-v1。

- check模式对合约错误和报价错误逐标的隔离，BTC失败不阻断ETH/SOL，任何错误都会使总体结构检查不通过。
- 新visibility模式独立于保留的旧timed重现模式，不再通过原run_probe的fail-fast外循环；每次实际报价校验仍调用未修改的Market.opening。
- 使用原arm_probe的封存计划，加独立采样策略d321-hour-visibility-v1、协议内容指纹、独立attempt/result和追加事件；同计划同策略不能换输出路径重复观测，崩溃不重跑。
- 每次记录实际HTTP发送/接收、状态、逻辑请求完成、query白名单、最多2根柱的有限字段摘要、解析错误及代码位置；异常消息仅允许固定安全集合，其他字符串省略，避免泄露任意服务返回内容。
- 逐标的记录每轮采样、第一次实际看到目标柱的响应接收时刻和验证完成时刻、第一次原生opening接受时刻；首轮成功和后续诊断重查成功分开。
- 一旦目标柱已看到但原生校验拒绝，标为VISIBLE_BUT_INVALID，不通过后续择优样本掩盖。
- 行情可能来自不同缓存，首次看到仅表示本次离散请求中最早观测，不是全市场首次生成时间，也不能假定之后每个节点永不回退。

## 待授权的实验规则

本轮**未封存新的未来计划、未创建任何timer、未执行visibility真实整点观测**。

拟申请一次性新试验：

1. 用户确认后，选仍在未来且留足准备时间的UTC整点；提前80秒启动已验签独立进程，并验证配置、UTC/NTP与服务器时间。
2. 固定按整点后1、5、9秒进行最多3轮串行采样；每标的最多3次，已出现并通过或已出现但无效的标的不反复查询。持续缺失最多3次，不保证覆盖最后6秒。
3. 最多10次实际HTTP尝试（1次时钟+至多9次candles），按原进程级0.25秒间隔限速；visibility单次查询不启用隐藏传输重试，connect/read总socket预算不超过1秒且受剩余窗口约束。窗口外响应保存为诊断，但不得成为成功。
4. 使用独立Linux进程SIGALRM在目标+15000ms中断阻塞观测，恢复本进程信号处理；不修改F3、SSH、系统服务或正式价格来源。另有外层单次进程上限用于异常回收。Windows无此原生alarm，只用于隔离夹具，不冒充Ubuntu硬截止验收。
5. 全局时钟不可信或预热失败时停止全部观测；一般单标的错误不阻止其他标的。三标的不全有效，总体不通过。
6. 分别统计first_attempt_accepted与diagnostic_retry_success；即使三者诊断补查通过，formal_f3_admission仍False，不能声称原正式F3一次请求流程已通过，也不能形成纸面成交。
7. 原始安全字段均False，样本仅进私有probes目录，保留所有失败轮次，不能挑选有利结果。达到次数预算可提前停止，必须披露采样空隙及提前停止，不能标成连续15秒全覆盖。
8. 若遇严重网络/服务故障，有限预算不扩张；不自动安排第二次整点。不安装长期任务。

## 测试与真实非整点验证

本地专项19 passed，最终相关集合（diagnostics、ubuntu_r14、forward_paper、paper_instruments）159 passed in 57.42s。未运行本轮无关的全量A—D/股票套件，冻结原始已跟踪文件未改动。
覆盖：BTC异常时ETH/SOL继续、全部失败总体失败、旧柱后新柱出现、窗口外响应拒绝、三轮/10请求上限、同计划幂等、无法把过去整点导入为前向、429单次HTTP无隐藏重试、非整点合约错误隔离、字段摘要/脱敏及旧F3兼容。

Vultr以ta-research UID999执行一次独立Linux alarm短测试：51ms中断1秒阻塞，未设置任何系统定时任务。
随后UTC2026-10-25 01:14:45至01:14:46非整点公开检查：

| 标的 | 逻辑请求开始UTC | 完成UTC | 耗时 | HTTP/实际次数 | 结构 |
|---|---|---|---|---|---|
| BTC | 01:14:45.586 | 01:14:45.840 | 254ms | 200/1 | VALID |
| ETH | 01:14:46.091 | 01:14:46.347 | 256ms | 200/1 | VALID |
| SOL | 01:14:46.591 | 01:14:46.841 | 250ms | 200/1 | VALID |

7次公开GET（含时间和三份合约规格），零网络重试；原生opening在窗口外均按预期抛Opening not observed on time，不能登记为入场成功。时钟偏移+8.5ms、RTT41ms。
三标的真实整点首次可见时间目前均**未测得**，不把本次非整点已经存在的小时柱当作首次发布。

## 实机路径及指纹

新附加副本：/opt/tradingagents/diagnostics/d321-20261025/quote_probe_diagnostics.py。
SHA256：76285bc58be122a9f72574179cf9be3c6dd069fb8fccaec0cdb9520242da152c。
非整点原档案：/var/lib/tradingagents/r14/probes/d321-20261025/non-hour-check.json。
没有覆盖D31旧副本，D3.2的原runner和结果仍可按原指纹复核。使用现有3.13.13/86项依赖，不新增依赖、模型调用或认证变更。

本地命令：
```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_quote_probe_diagnostics.py tests/test_ubuntu_r14.py tests/test_forward_paper.py tests/test_paper_instruments.py
.\.venv\Scripts\python.exe -m tradingagents.quote_probe_diagnostics check --output "$env:USERPROFILE\.tradingagents\logs\d321_check_NEW.json"
```
visibility需显式--plan，且仅在新的整点试验获授权后按封存计划执行；本说明不代表已创建计划。

## 是否修改正式F3

当前无需、也没有足够证据修改冻结F3。它对缺少目标柱的拒绝是正确保护。先用隔离诊断量化API可见性及不同缓存响应；若反复证明正式一次查询流程不可靠，再提交独立兼容设计和协议版本评审，不自动把补查规则移入正式成交逻辑。

最终建议：诊断改进、隔离测试与非整点验证有条件通过；整点可见性实验待授权、实际延迟未确定，不能宣称D3总体通过或模型完整研究已完成。
execution_enabled=False / allow_new_risk=False / simulation_admitted=False。
