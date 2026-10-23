# D.5：OKX 模拟 Memory / Reflection

范围：仅 trading_research，`execution_enabled=False`、`allow_new_risk=False`。不自动调参、不授予交易权限，不改变原研究协议。

## 模型服务探测

2026-10-23 06:07:59 UTC（北京时间 14:07:59）启动一次简短真实请求，当前配置为 chatgpt / gpt-6.1-sol，现有认证与模型代理路径。仅一次调用、SDK 重试 0、传输超时 45 秒，无工具、无交易副作用。返回完整 `OK`，Responses 完成状态为 `completed`，耗时 10.630739 秒。实际返回输入 21、输出 5、合计 26 Token。订阅路径不接受 max_tokens，故没有声称硬性输出 Token 上限，也没有据假设价格计算费用。

记录：`C:\Users\jerry_su\.tradingagents\logs\okx_feedback\d5_service_probe\probe_result.json`。启动标志使相同探测目录不能隐式重试。未重跑原 14 例，也未进行真实 Reflection 调用。本次短请求成功不能证明长流程已稳定。

官方参考：[Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)。本项目沿用 D.4.2 的客户端完整响应校验，不把部分输出视为完成。

## 复用与改动

原股票链路保持原样：TradingAgentsGraph → TradingMemoryLog.store_decision → settlement.settle_pending/fetch_returns → Reflector.reflect_on_final_decision → update_with_outcome → get_past_context。

OKX 模拟链路：封存历史研究与审计账本 → `okx_feedback.import_experiment/build_feedback` → `OKXSimulationMemory.store_feedback` → `settle_simulation_feedback` → 原 `Reflector` 新增的 `reflect_on_simulation_feedback` → 独立模拟日志 → 显式、按时间过滤的查询。

新增/修改文件：

- `tradingagents/memory/okx_simulation.py`：继承 TradingMemoryLog，复用分隔符、解析器、跨进程日志锁、settling 锁和原子更新方式。新增严格模拟反馈/反思 Schema、时间过滤、来源校验和幂等写入。固定子目录 `okx_simulation/memory.md`，不接受股票日志路径覆盖。
- `tradingagents/memory/reflection.py`：仅新增模拟反馈方法；原股票方法和提示词未改。调用原 Reflector 所持有的模型，不新增 Agent。
- `tradingagents/okx_feedback.py`：复用 D.4.1 seal、候选归属/状态机核验、D.3.1 费用审计和 D.1 feedback 时间选择器。导入与 CLI，模拟示例模型。
- `scripts/probe_okx_llm.py`：一次性真实连通性诊断。
- `scripts/report_okx_feedback.py`：离线示例、Schema、时间隔离与原文件保存审计报告。
- `tests/test_okx_feedback.py`：固定模拟响应与成交、时间、错误及股票隔离测试。
- 本文：用法和验收说明。

股票 settlement 的收益率和 benchmark alpha 不适用于永续账本，因此不调用 Yahoo，也不填造 0% alpha。复用其 `Settlement` 结果类型、逐条完成及单次结算锁模式；永续费用和收益来自原模拟账本，不另开发撮合或结算引擎。

## 反馈结构与语义

`SimulationFeedback` 保存：

- candidate_id、research_id、snapshot_id、来源协议/输入/账本/研究/代码摘要。
- COMPLETED / MODEL_FAILED / DATA_ERROR / UNSTUDIED / INCOMPLETE_RESEARCH 和原 ACCEPT / REJECT / ABSTAIN。无完整判断时 decision=null。
- 基准规则模拟成交、数量、费用、资金现金流、净损益及状态机事件；未成交需要明确终态事件，不能当作零收益。
- PROFIT / LOSS / FLAT / UNFILLED / WAIT / MODEL_FAILURE / DATA_ERROR / UNSTUDIED。
- 模型版本、冻结策略版本与指纹、历史参数/标记价格代理等限制。
- research_cutoff_ms、research_recorded_at_ms、outcome_known_at_ms、source_available_at_ms、recorded_at_ms、available_at_ms。
- simulated=true、execution_enabled=false、allow_new_risk=false、strategy_validated=false，Schema 拒绝提升这些状态。

`SimulationReflection` 保存来源反馈摘要、完整文本、提示词版本、模型版本、生成/可用时间、实际返回 usage、MOCK / REAL_LLM，以及不可改成有效策略的 `UNVALIDATED_RESEARCH_NOTE` 状态。

REJECT 关联的原规则收益是反事实基准，不是 Agent 实际组合收益；ABSTAIN、模型失败和数据错误不算成功避险。若要计算筛选后的组合，仍必须走原连续账本重放。本轮不改原 14 笔账本。

## 时间隔离

反馈可用时间为 max(研究实际记录、结果可知、来源可用、反馈写入时间)。原成交 `resolved_at_ms` 由 D.1 确定；无法证明原档案发布时间时，使用首次 D.5 导入时间作为保守可用时间，绝不追溯填为历史决策日期。

Reflection 必须在反馈已可用后生成，并单独按实际生成时间过滤。查询必须显式传入 UTC 毫秒或带时区 ISO 时间；不接受 None、纯日期、无时区时间。今天生成的 Reflection 不会进入过去的研究。未来盲测区间禁止作为本轮原实验导入源。

默认只读完整研究反馈；失败等诊断需 `include_diagnostics=True`，它们不进入 Reflection。MOCK 反思可以查看作示例，但不能进入正式 get_past_context。真实模型生成的笔记也只标为未验证建议，不能调整策略或权限。

**未自动接入冻结 D.4/D.4.1 的 Agent 提示词，也未打开现有实时 Graph 的 Memory 注入。** 如未来要给后续研究使用，应显式调用本适配器、传入研究 cutoff 并制定新实验协议。不能把今天的反思回填原历史对照。

## 幂等与失败

同一候选及来源摘要确定 feedback_id。重复导入复用首次写入时间；来源或判断发生变化则拒绝覆盖。反思按 feedback_id 一次完成，调用前落盘 STARTED。失败或异常中断保留尝试记录，不自动重放或伪装成完整笔记；需后续明确诊断恢复。模型失败后停止本次结算后续调用，未知 usage 不当作零费用。

重复执行小批次命令可继续处理下一条尚未复盘的完整反馈；不会重复同一条反馈/反思。MOCK 示例与后续真实反馈实验应使用不同输出根目录，不把固定模拟文本冒充模型产出。

## Windows PowerShell

以下命令只读原实验，导入独立模拟 Memory，并最多为一条未复盘完整记录生成固定模拟 Reflection；不调用真实模型：

```powershell
cd D:\_01trading_agents
$experimentSource = 'C:\Users\jerry_su\.tradingagents\logs\okx_traded_comparison\1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261'
$ledgerSource = 'C:\Users\jerry_su\.tradingagents\logs\okx_continuous\runs\c5970efc09e2819c8144b806d4fd4cb3ba452c454789132ce24447bcda0771ee'
$feedbackOutput = 'C:\Users\jerry_su\.tradingagents\logs\okx_feedback\d5_mock_example'
.\.venv\Scripts\python.exe -m tradingagents.okx_feedback --experiment $experimentSource --run $ledgerSource --output $feedbackOutput --mock-reflection --max-reflections 1
```

仅导入时移除 `--mock-reflection`。`--as-of '2026-02-01T00:00:00Z'` 可验证历史隔离：今天导入的记录不会被用于当时的 Reflection。

生成独立交付报告（没有模型调用）：

```powershell
.\.venv\Scripts\python.exe -m scripts.report_okx_feedback --memory $feedbackOutput --experiment $experimentSource --probe 'C:\Users\jerry_su\.tradingagents\logs\okx_feedback\d5_service_probe\probe_result.json' --preservation-manifest 'C:\Users\jerry_su\.tradingagents\logs\okx_llm_recovery\d4_2_audit\original_artifact_hashes.json'
```

Python 显式查询：

```python
from tradingagents.memory.okx_simulation import OKXSimulationMemory
memory = OKXSimulationMemory(r'C:\Users\jerry_su\.tradingagents\logs\okx_feedback\d5_mock_example')
records = memory.load_feedback(as_of='2026-02-01T00:00:00Z')  # []，不是当时可用数据
```

CLI 提供显式 `--real-reflection`，默认关闭，本轮未使用。真实反思依然是独立研究记录，不能混入冻结实验。不要重新运行探测命令去挑选成功结果。

## 整体验收建议与限制

验收：最终 D.5 专项 **35 项通过**；相关既有回归 **515 项通过**，覆盖股票 Backtest/Memory/CLI、阶段 A/B/C/C.1/D.1–D.4.2、代理与客户端。中间组合运行包含当时 33 项 D.5 测试，共 548 项通过（71.72 秒）；随后新增两项防护并单独重跑全部 D.5 专项。计数按独立用例区分，不把重跑重复计入。`git diff --check` 通过。

专项覆盖 ACCEPT/REJECT/ABSTAIN、未成交与 WAIT、模型与数据失败、交易归属、精确时区/毫秒边界、来源实际可用时间、反思生成时间、结果不得早于退出、未来 cutoff 阻断、幂等性、调用中断、部分输出/空输出及股票日志/Yahoo 隔离。原 14 例真实封存输入均无法在原历史研究 cutoff 读到本次反馈/反思，原实验 146 个文件哈希保持一致。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_okx_feedback.py -q
```

A/B/C/C.1：以合约身份、三周期数据与指标、数据质量闸门、预案状态机和时间边界作为确定性验收依据。D.1–D.3.1：复核账本幂等、跨批次持仓/费用和不确定性披露。D.4–D.4.2：保留 8 个失败和回顾性样本限制，不宣称 Agent 提升收益或服务完全稳定。D.5：验收独立存储、结果关联、实际可用时间和未验证笔记状态，不验收为“策略已经自我优化”。

历史合约参数连续性、精确资金结算标记价格、原始数据发布延迟仍不完全可证。没有实际杠杆、保证金或强平安全模拟。14 笔样本不足以判断盈利优势，LLM 历史知识污染仍存在。真实 Reflection 的质量和长期收益改善尚未验证。当前 D.5 只接入单合约原封存成交样本；不是全 279 候选或未成交层的完整 Reflection 实验。没有新数据库、RAG、新 Agent 或自动策略优化。

完整示例及原文件哈希核验见 `d5_mock_example/delivery/`。本轮完成后停止，不自动扩展其他功能。
