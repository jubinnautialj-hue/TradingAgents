# Windows 11 运行手册：A—D 已验收范围

本手册描述现有命令，不增加功能。所有 OKX 输出保持 execution_enabled=False。下列行情命令是公开 GET；完整研究会调用模型并产生用量，不代表已经获准交易。不要在本轮验收后自动执行整组命令。

## 环境与路径

```powershell
cd D:\_01trading_agents
$researchLogs = Join-Path $env:USERPROFILE '.tradingagents\logs'
$baselineRun = Join-Path $researchLogs 'okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee'
$frozenExperiment = Join-Path $researchLogs 'okx_traded_comparison\1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261'
.\.venv\Scripts\python.exe -m cli.main --help
.\.venv\Scripts\python.exe -m cli.main okx-research --help
```

必须先进入项目目录，使用该 `.venv`。本轮观察到工作区外独立脚本可能导入 site-packages 内的原版 tradingagents，而不是当前 OKX 源码。不要以“环境版本同为 0.7.0”认定源代码相同。没有为本轮验收重装包或修改全局 PYTHONPATH。

19 项直接依赖满足当前 pyproject；SOCKS 路由需现有可选 socks 依赖和有效本地隧道。继续复用当前模型配置，不把 API Key、登录 Token 或服务器密码写进命令/报告。不修改 Codex MCP。

## 公开数据与研究

```powershell
# 单周期，只验证行情；不会调用模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 1h --data-only

# A/B：三周期与可选衍生品，只验证数据
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives --data-only

# B：完整多 Agent 研究，会调用当前模型
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --derivatives

# C/C.1：独立多空交易预案，会调用模型；5bps 是假设费率，不是账户费率
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5
```

`--trade-plan` 自动启用三周期和衍生品。没有账户持仓资料时是 UNKNOWN，不假设已经做多或做空。`--portfolio` 仅接收调用方本地持仓描述，不读取 OKX 私有账户；格式与数量单位必须符合现有校验，不将通用股票 quantity 自动当成合约张数。

当前 `--interval` 仅支持 1h/4h/1d，日线实际映射 1Dutc。`--date YYYY-MM-DD` 是 UTC 研究日：历史日按当日 UTC 结束处理，当前日取服务器现在；该参数不是任意小时 cutoff。历史当前盘口/OI 不得填入历史研究。单次研究默认400根/周期用于指标，不应把此限制误读为 D.3 历史下载的总上限。

报告写入 results_dir/reports/唯一目录，含 Markdown/HTML；预案模式另有 research_result.json 与随报告保存的 Schema。已有报告不代表当前仍有效。读取状态时区分：

- data_quality / allow_new_risk：数据研究许可，不是订单许可；预案结果中的 allow_new_risk 固定 false。
- plan_valid / plan_status：条件候选是否完整，不是当前已经触发。WAIT 不能被解释为数据故障，数据故障也不能用 WAIT 掩盖。
- PENDING_BREAKOUT / PENDING_RETEST / ENTRY_ELIGIBLE：确定性历史/显式调用的信号状态；没有自动盯盘驱动器。
- requires_revalidation、实时 TTL、假设有效期：分别检查；不能通过延长 TTL 消除数据过期。

现有 okx-research 退出码：配置 ValueError 为1；核心无效/被阻断一般为2；LLM_UNAVAILABLE 为3；成功为0。预案模式下 WAIT/PLAN_INCOMPLETE 不必是非零退出码，必须检查 JSON 状态，不能只检查进程退出码。

## 历史模拟、诊断和费用

```powershell
# D.3：完全离线地重建既有 386 天规则研究与连续账本；耗时较长，不调用模型
.\.venv\Scripts\python.exe -m tradingagents.okx_continuous --start 2025-10-01T00:00:00Z --end 2026-10-22T00:00:00Z --assume-current-contract-spec --input "$baselineRun\inputs.json" --output "$researchLogs\acceptance\manual_continuous" --processing-days 14

# D.3.1：独立费用审计，不修改原账本
.\.venv\Scripts\python.exe -m tradingagents.okx_cost_audit --run $baselineRun --output "$researchLogs\acceptance\manual_cost_audit"

# 其他既有接口：先查看真实必填参数
.\.venv\Scripts\python.exe -m tradingagents.okx_backtest --help
.\.venv\Scripts\python.exe -m tradingagents.okx_diagnostics --help
```

D.1 显式要求 start/end、taker-fee-bps、slippage-bps、assume-current-contract-spec；`--input` 用于对应已有输入，不要把 D.1 输入文件和 D.2 prepared source 格式混用。D.2/D.3 的 `--input` 分支与下载分支分开。processing-days 是处理分段，不是每段强制平仓或重置权益。

不要把受保护未来盲测区间 `[2026-10-24, 2027-01-24)` 代入开发命令。当前开发截止界限仍是 2026-10-22。所有已查看历史是开发数据；成本场景不是重新优化后的策略。

## 原 Agent 对照与故障恢复

默认只查看封存结果，不重跑14例：

```powershell
Get-Content "$frozenExperiment\report_reviewed.md"
.\.venv\Scripts\python.exe -m tradingagents.okx_traded_comparison --help
.\.venv\Scripts\python.exe -m tradingagents.okx_llm_recovery --help

# D.4.2 离线诊断：无模型调用，独立输出
.\.venv\Scripts\python.exe -m tradingagents.okx_llm_recovery --experiment $frozenExperiment --output "$researchLogs\acceptance\manual_llm_diagnosis"
```

不要对原目录执行 --execute 或 --analyze 来绕过冻结版本限制；源码变化后拒绝是保护行为。新恢复调用需要单案例、独立输出以及显式 --recover --restart-incomplete-role，会产生真实模型用量，并只能标为探索性，不能补成原冻结实验结果。未完成/部分响应、未知错误和 STARTED 中断默认不自动重试；额度或权限错误立即停止。

原 D.5 服务探测结果可直接读取 `okx_feedback/d5_service_probe/probe_result.json`。不应为了显示成功反复新建探测目录。一次短请求不验证长流程稳定，也不能证明8例故障已经修复。

## 独立模拟 Memory / Reflection

```powershell
# 新独立示例目录：导入原封存结果，固定 MOCK Reflection，不调用模型
.\.venv\Scripts\python.exe -m tradingagents.okx_feedback --experiment $frozenExperiment --run $baselineRun --output "$researchLogs\acceptance\manual_feedback" --mock-reflection --max-reflections 1
```

移除 --mock-reflection 则仅导入。重复导入不会重复记账；重复小批次运行可继续处理下一条未复盘完整记录，不会重复同一条反思。MOCK 目录不要改作真实 Reflection 目录；MOCK 文本不能进入正式研究上下文。

`--real-reflection` 默认关闭，会调用模型，本轮未验收其真实输出质量。失败或中断不会自动重放。新反思使用实际生成时间，不能将今天的反思放回2025/2026年的原研究。`--as-of` 必须带时区，且不得是未来时间。

Python 显式读取：

```python
from tradingagents.memory.okx_simulation import OKXSimulationMemory
memory = OKXSimulationMemory(r'C:\Users\jerry_su\.tradingagents\logs\okx_feedback\d5_mock_example')
past = memory.load_feedback(as_of='2026-02-01T00:00:00Z')  # []，当时尚未写入本次反馈
```

这不是实时 Graph 的自动学习开关。原冻结 Agent 实验继续禁用 Memory，实时 OKX Graph 也尚未注入此适配器。任何将来接入需另定协议，不在本轮执行。

## 股票与测试

```powershell
# 原股票交互分析仍是主入口，不存在另一个 analyze 子命令
.\.venv\Scripts\python.exe -m cli.main --ticker AAPL --date 2026-10-22 --analysts market --save --no-show

# 原股票决策质量回测的帮助；不同于永续模拟收益账本
.\.venv\Scripts\python.exe -m cli.main backtest --help

# 全默认离线测试（当前已知有5项Windows既有失败）
.\.venv\Scripts\python.exe -m pytest -q --tb=short

# 明确允许联网的公开OKX测试；不执行全部外部服务测试
.\.venv\Scripts\python.exe -m pytest tests/test_okx_public_integration.py tests/test_okx_multitimeframe_public.py tests/test_perpetual_plan_public.py -m integration -q
```

股票示例会调用外部数据/模型，且可能因现有用户配置继续询问模型选择；本轮未执行它。不运行 --clear-checkpoints、login/logout、升级、安装或任何清理命令作为验收动作。

## 常见故障处理

| 状态 | 正确处理 |
|---|---|
| DATA_UNAVAILABLE / STALE_DATA / RESEARCH_INVALID | 保留失败报告，检查公开接口、已收盘柱与时间；不转 Yahoo 现货，不将其当方向信号。 |
| LLM_UNAVAILABLE | 保留安全分类/请求记录；不伪造研究；不要自动重跑冻结实验。 |
| QUOTA_EXHAUSTED / 权限错误 | 立即停止模型队列，处理服务限制后另行决定；不无限退避。 |
| PLAN_INCOMPLETE / WAIT | 阅读缺失证据、结构冲突及成本原因，不强制生成开仓候选。 |
| 导入不到 OKX 模块 | 先核对 cwd 与模块实际来源；本轮不自动重装。 |
| 冻结代码指纹不符 | 不修改原 seal，不放宽校验，不覆盖原结果；使用原保存记录或独立探索性协议。 |

完整验收评级见 [final_acceptance.md](D:/_01trading_agents/docs/acceptance_20261023/final_acceptance.md)。
