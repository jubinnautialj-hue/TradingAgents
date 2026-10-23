# D.4 小样本多 Agent 筛选对照

仅研究实验，`execution_enabled=False`、`allow_new_risk=False`。不新增 Agent，不改动原策略、成交账本、历史报告、Memory 或未来盲测。

## 固定协议

A 为 D.3 原始 279 个候选和 14 笔交易；B 在原候选上筛选，不改变方向、价格、仓位算法或交易条件。样本按全部候选的 cutoff 排序，固定抽取索引 `0`、`floor(N/2)`、`N-1`，不使用盈亏、是否成交或模型输出选择样本。样本可能没有实际成交，这种情况只能验证研究流程，不能评估收益改善。

绩效使用原账本已准入的成交机会，不因某个 REJECT 再引入原本被阻断的其他候选；这是固定机会集的条件性筛选对照，不是独立重分配资金的完整新策略回测。对未研究的候选不推断 B 的决定，完整 B 组指标为 `NOT_EVALUATED`。必须区分全部候选、已研究样本以及有明确 ACCEPT/REJECT 的配对样本。ABSTAIN、数据失败、格式失败、模型失败不计为成功避险。

## 复用与权限

`GraphSetup.setup_graph(..., historical_filter=True)` 复用原有九个角色、Bull/Bear 轮转和三方风险轮转。只运行 Market Analyst，研究辩论一轮、风险辩论一轮；Research Manager、Trader、Portfolio Manager 通过现有 `choose_research()` 分派到受限 Schema。

本模式不接入任何 ToolNode 或 Memory 节点；Market Analyst 直接读取离线数据包，其他角色保留原有报告/辩论传递。`AuditedModel.bind_tools()` 明确拒绝工具。最终 Schema 只允许候选 ID、ACCEPT/REJECT/ABSTAIN、证据 ID 和理由，额外价格、方向、数量字段均失败；模型无权覆盖质量状态或执行权限。

历史预检复用 `okx_cost_audit.audit()`，校验完整数据、费用档案和原始账本；每个样本重新调用 `regenerate_decisions()`、`historical_bundle()` 验证原候选。输入仅包含 cutoff 前确认收盘的三周期价格、基于最多 400 根 K 线计算的指标和结构，以及最近 12 根 K 线。模型没有获得整段历史、后续触发、资金结算、交易 PnL、当前 OI/盘口或 Memory。

特别注意：D.2 会在原决策 `diagnostic` 中补入生命周期和交易结果。本模式只比较重新生成的当时字段，绝不复制后验附加字段；模型输入通过明确字段白名单重新构造。

历史输入截断无法消除 LLM 训练中的事后知识。当前合约参数、收盘数据零传播延迟及历史标记小时开盘价等假设继承 D.3.1，所有已查看历史仍是开发集。本实验不构成严格无污染的历史预测验证。

## 成本和审计

每个样本最多九次请求，共最多 27 次；SDK 和实验级重试均为零，解析失败不修复或重新采样。每次请求记录生成的研究提示词、原始回复、解析内容、token 用量（若供应商提供）、模型标识、耗时和安全错误类型，不保存认证信息。

请求开始前保存 `.started.json`，每次完成或异常后保存调用日志；中断的样本不会自动再次发起请求。已完成结果复用，避免用重新运行挑选更佳答案。每个请求沿用模型客户端现有超时，样本在下一次调用前检查 1200 秒预算；它不是强制终止已在响应中的流。

订阅模型不接受 `max_tokens`，因此该值不作为有效硬上限；次数、无重试和输入规模控制实验预算，提示词要求简洁输出。无法查询实际账单时金额为 UNKNOWN，不将 token 用量冒充实际费用，也不声称免费。

## Windows 使用

先准备并固化协议，不调用模型：

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.okx_agent_comparison --run "C:\Users\jerry_su\.tradingagents\logs\okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee"
```

本轮批准的小样本真实研究使用相同命令追加 `--real-llm`。同一代码、配置和输入版本会复用同一协议目录及已完成结果；修改模型、提示词、代码或版本会生成另一个协议，不能用不同协议反复运行后只报告最好的一组。

输出目录 `~/.tradingagents/logs/okx_agent_comparison/<protocol_hash>/` 保存协议、三个纯历史输入、全部调用和案例结果、`comparison.json`。其中 A_all 保留原 14 笔；A_studied 为抽样候选中原来成交的交易；A_decisive_only 与 B_accepted_only 是排除弃权/故障后的明确决定配对比较。B 接受但原本没有成交的候选不虚构成交，拒绝未成交候选不产生“避险收益”。

## 后续判断

先核对角色完整性、时间隔离、输出约束与实际成交样本数量，再判断是否扩大覆盖。没有成交的样本不能判断 Agent 是否过滤亏损或错失盈利；即使有少数交易，也不能声称统计优势。后续可预注册扩大候选覆盖的协议，但不得按本轮有利结果选择案例。

Memory/Reflection 后续只接收独立命名空间的模拟结果，按 `resolved_at_ms` 的实际可知时间过滤，关联协议、候选快照和模型版本；必须区分分析错误、数据故障、执行假设，不自动优化策略或污染股票 Memory。本轮不实现该集成。

## 本轮真实结果及版本说明

本轮唯一真实协议目录为 `7319379d00a8d9112b75bb27e9bced43360aae87752299fb2b5dfcc4518dddc9`。实际使用 ChatGPT 订阅认证，quick/deep 均为既有配置 `gpt-6.1-sol`，没有更换模型或提示词挑选结果。服务未返回不可变权重版本或系统指纹，不能把模型别名视为永久固定的服务版本。

全部 279 个候选中抽取 3 个，276 个未研究；九个角色各执行一次，共 27 次请求，无重试、模型失败或格式失败。ACCEPT=2、REJECT=1、ABSTAIN=0：

| UTC cutoff | 方向 | 最终筛选 | 主要理由 | 原账本成交 |
|---|---|---|---|---|
| 2025-10-06 00:00 | LONG | ACCEPT | 日线及 4h 方向支持条件性观察，同时提示动能冲突和偏低盈亏比 | 无 |
| 2026-02-19 12:00 | SHORT | ACCEPT | 日线/4h 偏空支持原触发条件，同时保留短周期背离与支撑风险 | 无 |
| 2026-10-06 12:00 | LONG | REJECT | 触发位置贴近日线阻力，结构及动能冲突，扣成本后的空间不充分 | 无 |

三个样本都不属于原 14 笔交易，因此已研究样本的 A/B 均无成交、手续费与损益为零，胜率不适用。完整 B 组仍是未评估。不能把 A_all 的 -21.9648 USDT 与 B 小样本的零交易相比，也不能证明过滤亏损或错失盈利。没有追加挑选“有利”案例。

供应商报告输入 208792 tokens、输出 11157 tokens；27 次调用累计约 439.26 秒。实际账单金额不可查询，记为未知，不声称零费用。所有历史参数、资金估值与发布时间限制继续保留，成本敏感性未作策略优化。

完成后复跑检查发现，原生周期快照 ID 含采集时钟，导致准备同一历史实验时目录不稳定。已在 **v1.1** 将历史周期 ID 改为候选、周期和已收盘数据的稳定内容哈希；增加真实数据重复重建及单元测试。已完成的 **v1** 27 次模型请求、原协议和完整源码快照原样保留，未重新调用模型。原 ID 是不透明哈希，数值输入及 cutoff 没有未来数据；本次修正处理的是复跑身份稳定性。

读取本轮已完成结果，明确禁止新增模型调用：

```powershell
.\.venv\Scripts\python.exe -m tradingagents.okx_agent_comparison --run "C:\Users\jerry_su\.tradingagents\logs\okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee" --resume-experiment "C:\Users\jerry_su\.tradingagents\logs\okx_agent_comparison\7319379d00a8d9112b75bb27e9bced43360aae87752299fb2b5dfcc4518dddc9"
```

`scripts/report_okx_agent_comparison.py --experiment <目录>` 可离线导出配对结果、CSV 和三份原生 Markdown/HTML 九角色报告；不会调用模型。原生 HTML 中 REVIEW 是兼容旧报告的评级占位，实际筛选由 Portfolio Manager 的 ACCEPT/REJECT/ABSTAIN Schema 单独记录。

最终相关测试共覆盖 **420 项**：完整选定回归执行 419 项通过，随后新增离线复用测试并执行专项 25 项全部通过（其中 24 项与前次重合；既有相关回归 395 项）。未声称执行全仓库所有测试。真实 14 笔原准入机会重放的交易、指标、权益曲线均保持一致。

本轮工程流程验收通过，收益改善验证因有效成交样本为零而不成立。若继续，应另行预注册更有成交覆盖的条件性配对样本，并保留未成交层，不以本轮结论挑选案例；本轮不追加模型成本、不执行 D.5。

## 本轮文件清单

新增：

- `tradingagents/okx_agent_comparison.py`：协议、历史输入白名单、原生图运行、调用审计、固定机会集账本和离线复用。
- `tradingagents/agents/candidate_filter.py`：既有角色的筛选提示词及严格 Schema，不增加 Agent。
- `tests/test_okx_agent_comparison.py`：25 项专项测试。
- `scripts/report_okx_agent_comparison.py`：离线复用原生 Markdown/HTML 报告导出，附配对 CSV。
- `docs/okx_d4_comparison.md`：协议、运行方式、结果与限制。

最小增量修改（均保留之前未提交内容）：

- `tradingagents/agents/state.py`：增加隔离模式与输入状态字段。
- `tradingagents/agents/perpetual_research.py`：既有 `choose_research()` 增加历史筛选分派。
- `tradingagents/agents/analysts/market_analyst.py`：既有 Market Analyst 增加离线已验证证据模式。
- `tradingagents/graph/setup.py`：既有图增加默认关闭的历史筛选开关，不接工具/Memory，原模式默认行为保持不变。

未修改三个冻结策略源文件、原始股票 Backtest/Memory、OKX 数据适配器、实盘权限或 MCP 配置；未提交或推送 Git。
