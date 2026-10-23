# D.4.1 回顾性成交条件样本对照协议

只做 `trading_research`。`execution_enabled=False`、`allow_new_risk=False`。不新增 Agent，不修改原策略、费用、执行权限、账户配置、Memory 或未来盲测。

## 冻结与样本归属

全部纳入原 D.3 的 14 笔成交对应候选，按研究 cutoff 排序；不依据盈利、亏损或模型输出选例。原始集合仍是 279 个候选，其他 265 个保留。此样本以事后发生过成交为条件，属于回顾性开发样本，不能代表全部候选或严格样本外预测。

`locate_candidates()` 检查 snapshot ID 唯一、原交易研究时间和方向、止损止盈来源，以及 PENDING_BREAKOUT → PENDING_RETEST → ENTRY_ELIGIBLE → ENTRY 的事件归属。再次以 C.1 `evaluate_signal()` 回放已收盘确认柱，验证下一可交易时刻等于原开仓时刻。原账本另通过 D.3.1 完整数据及费用重放审计，任何关联失败都在调用模型之前停止。

模型和提示词沿用 D.4：原生 Market Analyst、Bull/Bear、Research Manager、Trader、三方风险团队、Portfolio Manager。研究辩论及风险辩论各一轮，最终动作限于 ACCEPT、REJECT、ABSTAIN。全部提示词、代码、运行版本、数据哈希、模型配置、费用配置及 14 个输入哈希存入 `protocol.json`，并保存源码快照。

原模型配置为 quick/deep 均使用 ChatGPT 订阅下的 `gpt-6.1-sol`；以实际冻结文件为准。模型别名不是不可变权重版本，服务未提供的版本不虚构。

## 输入隔离和先后顺序

分为三个独立命令/进程：

1. 准备：验证历史原账本，构建 cutoff 前的输入与独立关联记录。
2. 执行：只接受实验目录，不允许传入原账本目录。仅读取固定协议和历史输入文件。`run_case()` 不接入外部工具、账户、Memory 或 Reflection。
3. 分析：只有全部案例状态写入 `sealed_decisions.json` 后，才打开原始结果进行盈亏关联和组合回放。封存哈希保护每个案例的完整输入输出记录；任何改动都阻断分析。

模型输入复用 D.4.1 之前已修正的 D.4 `case_input()`：仅 cutoff 前确认收盘的 1h、4h、UTC 1d 数据，基于最多 400 根柱计算的指标和结构，最近 12 根柱，以及原候选和显式成本假设。后续确认时间、未来开仓信息只在 `lineage.json`，不进入模型输入。后验资金结算、未来 OI/盘口、净损益和事后复盘均排除。最终实际数量尚不可知，只提供原固定名义敞口算法，模型不能决定数量。

输入截断无法消除模型训练知识中的事后信息；实际行情传播延迟及历史修订版本仍不完整，不能认证严格无污染的历史验证。

## B 组连续账本规则

本轮不再采用 D.4 固定成交机会集的删减方式。B 从同一初始权益出发，完整回放全部原始决策点、279 个候选、历史 K 线及资金结算。

- ACCEPT：保留原候选及所有条件。
- REJECT：只在原研究时刻屏蔽这个候选，不改方向、价位或费用。
- ABSTAIN、模型/格式/数据失败：原规则透传，单独标记，不计为成功避险。
- 其他 265 个未研究候选：原规则透传，不擅自推断 Agent 决定。

沿用单活动预案/单持仓、六根确认窗口、下一根开盘及不利滑点、STOP_FIRST、最长 12 根持仓、数量换算和资金结算顺序。拒绝后可能解除后续候选的持仓冲突，从而产生原先未成交的交易；这些交易必须真正进入同一账本，重新计算手续费、资金费、权益和回撤。

因此 B 是**部分 Agent 覆盖的混合策略场景**，不是完整 Agent 策略，更不是实盘收益。配对的“拒绝原亏损／错过原盈利”只描述原 14 笔交易的筛选归属，不等于组合收益变化或因果收益贡献。

基准单边 Taker 5 bps、不利滑点 2 bps，固定模拟权益 10000 USDT、名义敞口 1000 USDT，其他配置从原输入完全继承。不通过费用敏感性调整挑选最优策略；D.3.1 低/基准/高成本结论和历史参数、标记价代理、资金结算边界限制继续适用。

## 预算、缓存和中断

14 个案例各最多九次请求，总上限 126 次；最多同时运行两个独立案例，SDK 和实验层重试均为零。订阅服务可能不接受 `max_tokens`，因此不声称它是有效硬上限。按次数、固定样本及简洁输出控制预算，实际账单金额不可查询时明确为 UNKNOWN。

每个案例开始前写入标记，完成后不可变保存 `.result.json`，其中含九角色请求、回复、Token、耗时及安全错误信息。完成案例直接复用；中断中可能已经发出的调用不自动重放，记为 INTERRUPTED_NOT_RETRIED，其他尚未开始的案例可在同一冻结版本继续。限流或认证故障会阻止排队中的新案例发起请求；故障不改记为 REJECT。

封存后的同一实验重复执行不创建模型、不会再次调用。代码、配置或输入变更会阻断现有冻结实验，不能靠修改协议重跑后只选择有利结论。

## Windows 命令

准备（无模型调用）：

```powershell
cd D:\_01trading_agents
$baseline = 'C:\Users\jerry_su\.tradingagents\logs\okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee'
.\.venv\Scripts\python.exe -m tradingagents.okx_traded_comparison --run $baseline
```

将输出目录设为 `$experiment`，依次运行：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.okx_traded_comparison --experiment $experiment --execute
.\.venv\Scripts\python.exe -m tradingagents.okx_traded_comparison --experiment $experiment --run $baseline --analyze
```

执行阶段故意不接受 `--run`，防止研究进程获得原始交易结果。已经完成的同一冻结实验复用缓存；不要修改配置后建立多个实验来挑选最好的一组。

## 输出与验收

实验目录保留：原协议、源码快照、279 候选目录、14 个纯历史输入、逐案例调用及结果、独立决策封存、关联审计、逐笔配对 CSV、A/B 连续账本、逐笔交易、事件及权益曲线 CSV。所有原始研究报告和原 14 笔结果均不覆盖。

专项测试覆盖关联错误阻断、已收盘状态链、九角色真实流程的模拟模型执行、先封存后分析、封存篡改拦截、预算/冻结变更、重复执行不再调用、中断不重试、弃权和失败透传，以及拒绝解除冲突后正确出现后续交易及其费用。另运行 A/B/C/C.1/D.1/D.2/D.3/D.3.1/D.4、股票 Backtest 和 Memory 的相关回归。

本轮不自动优化策略、不向 Memory 写入收益、不进入 D.5。未来若引入 Reflection，只能使用独立模拟命名空间及结果实际可知时间过滤，不能将本次回顾性效果推广成实盘优势。

## 本轮真实结果

冻结目录为 `1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261`。14 个候选全部关联成功并完成独立结果封存；**实际完整模型研究只有 6 个，另 8 个失败**。完整案例均执行原九角色、最终 ACCEPT=6、REJECT=0、ABSTAIN=0。失败没有被解释为拒绝。

完成的六个案例全部是 SHORT，覆盖原 4 笔盈利及 2 笔亏损；八个失败案例恰好都是原亏损，其中包含全部两笔 LONG。这种按运行顺序发生的缺失不能删除后再计算“Agent 高胜率”，也不能声称做多筛选已被验证。

安全错误记录统一为 `APIError / MODEL_SERVICE_ERROR` 且 `partial_response=True`。这表示服务调用已发生部分响应，不足以认定代理故障或订阅额度耗尽；未保存或输出认证信息，也没有盲目重试、更换模型或改提示词补做有利答案。本次实际调用 75 次，零重试；已报告输入 511924、输出 27767 tokens，八次失败调用缺少用量，因此这些总数不是完整消耗证明。累计调用时间约 1136.54 秒（并发调用的时长之和，不是墙钟时间），实际账单金额未知。

全部结果封存后才由独立分析进程加载原盈亏，并重放完整 2316 个决策点及原 279 个候选。根据预注册的失败透传规则，B 没有屏蔽任何候选：

| 指标 | A 纯规则 | B 部分 Agent 覆盖混合重放 |
|---|---:|---:|
| 交易数 | 14 | 14 |
| 多／空 | 2／12 | 2／12 |
| 净损益 USDT | -21.96483003 | -21.96483003 |
| 胜率 | 28.5714% | 28.5714% |
| 最大回撤 | 1.3778% | 1.3778% |
| 手续费 USDT | 13.94311582 | 13.94311582 |
| 内含滑点 USDT | 5.59425000 | 5.59425000 |
| 资金费现金流代理 USDT | +0.29801578 | +0.29801578 |

交易、指标和权益曲线一致；B 没有新增未研究成交，也没有删除原成交。已完成的两个亏损机会均未被过滤，未出现错拒盈利；但研究只完成 6/14，不能据此评价整个 Agent 筛选能力。

## 分析修复与最终验证

第一次分析暴露了缺失 `Counter` 标准库导入的问题，已修正。原冻结协议、源码快照、提示词及全部模型结果保持不变，没有重新调用模型。`analysis_source_amendment.json` 记录修复前后哈希；分析专用检查要求其他源文件完全相同，并以 AST 验证研究、执行、交易回放和统计运算没有改变，仅允许标准库导入及来源核验包装。模型执行仍保留严格代码冻结检查，不以这个分析修复放宽模型执行条件。

最终 **447 项相关测试通过，66.06 秒**，其中本轮专项 27 项、既有相关回归 420 项。包括封存后分析的完整模拟端到端路径、费用/事件来源一致性，以及分析修复不能改变数值算法的测试；没有声称执行全仓库所有测试。

本轮源文件均为新增，未改写原策略或旧阶段源码：

- `tradingagents/okx_traded_comparison.py`：关联、协议、独立执行/封存、连续账本分析及审计。
- `tests/test_okx_traded_comparison.py`：27 项专项测试。
- `scripts/report_okx_traded_comparison.py`：离线复用原生 Markdown/HTML 导出及配对报告。
- `docs/okx_d4_1_traded_comparison.md`：协议、命令和结果。

实验目录内的 `report_reviewed.md` 是包含非随机缺失说明的复核报告，`paired_cases.csv` 保存全部 14 例，`A_trades.csv` / `B_trades.csv` / `A_equity.csv` / `B_equity.csv` 为真实程序重放输出。另保留原六份完整九角色 Markdown/HTML 报告与八个失败案例的请求记录。

离线重现本轮已封存结果的分析（不会调用模型）：

```powershell
$experiment = 'C:\Users\jerry_su\.tradingagents\logs\okx_traded_comparison\1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261'
.\.venv\Scripts\python.exe -m tradingagents.okx_traded_comparison --experiment $experiment --run $baseline --analyze
.\.venv\Scripts\python.exe scripts/report_okx_traded_comparison.py --experiment $experiment --report-name report_reviewed.md
```

若后续恢复失败案例，应先另行验证服务稳定性，预先规定恢复协议、预算及停止条件，保留本轮首次失败与六个已完成决定，不按盈亏挑选重试对象、不覆盖原结果。本轮不启动恢复实验或 D.5。
