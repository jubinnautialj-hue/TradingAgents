# D.4.2：LLM 服务稳定性诊断与失败恢复

仅限 research。`execution_enabled=False`，不访问账户或交易接口。

## 诊断与证据

原 D.4.1 封存实验 `1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261`：14 例中 6 例完成，8 例失败。原 calls.json 与 sealed result.json 内容一致。8 个错误全部是 `APIError / MODEL_SERVICE_ERROR`、`partial_response=True`。两例已经完成 7 / 6 个角色，另外六例在 Market Analyst 失败。

**不能确定原始服务故障细因**：旧 `classify_failure()` 只保存异常类型、笼统分类、部分响应标志，随后 `ChatGPTPlanChatOpenAI._stream()` 通过 `raise failure from None` 隐去原始异常链；原记录没有错误码、HTTP 状态、request ID。不能据此断言代理中断、限流、额度、上下文或账户问题。

安装 SDK `openai/_streaming.py` 的 `Stream.__stream__` 会把 SSE 数据内的 `error` 对象转成 APIError；这说明 APIError 并不等同于网络错误，但不能反推原八例一定经过该分支。

客户端来源：`ChatGPTPlanChatOpenAI.__init__/_stream`，`OpenAIClient.get_llm`。原协议使用 chatgpt 订阅认证路径、quick/deep 均为 `gpt-6.1-sol`，SDK max_retries=0、流式 Responses、timeout=120 秒、原案例并发 2。未读取登录凭据，也没有重新验证当前账户权限。显式代理采用 SDK 默认 httpx2 客户端、TLS 验证开启、trust_env=False，连接复用保留。SDK 默认池限制为 1000 连接 / 100 保活；没有连接池耗尽证据。120 秒是传输超时，不能当作严格流总时限；原工作流的 1200 秒预算在每个角色调用前检查。

已安装：openai 3.36.1、langchain-openai 1.7.2、httpx 0.28.1、httpcore 1.0.9、httpx2/httpcore2 2.16.0。本轮未升级依赖、切换模型、关闭流式、调整代理或关闭证书验证。

官方依据：[OpenAI 错误分类](https://developers.openai.com/api/docs/guides/error-codes)。429 需区分瞬时速率限制和额度/计费问题；本项目订阅路径的具体错误仍需以实际返回的结构化证据为准，不能照搬 API 计费推断。

## 实现与文件

- `tradingagents/llm_clients/reliability.py`：保留 allowlist 内的 provider code/type、HTTP 状态、数字 Retry-After 和 request ID 哈希；区分额度、权限、上下文与瞬时服务错误。不保存错误 message、任意 body、凭据或 URL。
- `tradingagents/llm_clients/recovery.py`：RecoveryController、RoleJournal、RecoveringModel。新实验单请求串行，最小调用间隔 2 秒；每角色跨恢复累计最多 3 次尝试。指数退避加抖动，单次等待不超过 30 秒；更长 Retry-After 停止并交给后续诊断，不提前请求。SDK 重试必须关闭，避免叠加。
- `tradingagents/okx_llm_recovery.py`：核验原实验 seal、输入摘要、调用日志，输出逐例诊断；严格复用原 run_case 和九角色图。复用角色时核对原 prompt/tier/schema；新检查点核对结果摘要、代码、模型与配置指纹。新 Schema 仍为原 ACCEPT/REJECT/ABSTAIN，没有研究提示词变化。
- `scripts/report_okx_llm_recovery.py`：零真实模型调用的原八例检查点重建与独立报告。
- `tests/test_llm_recovery.py`：专项故障注入及原生图恢复测试。
- 本文：使用方法、验收与局限。

自动重试仅适用于连接建立失败、具有明确速率错误码的 429、未收到部分响应的明确瞬时服务错误。读取超时、读写中断、部分流、未知 APIError、未知 429、上下文错误和格式错误不自动重试。额度、认证/权限等终止错误停止后续调用，持久化 campaign_stop，重新启动同实验也阻断。

每次调用前原子保存 STARTED，完成后保存状态、角色进度、返回 token 用量、耗时及安全错误分类。进程在 STARTED 中断时不自动重放；失败且没有 usage 的费用是未知，不记作零。完整响应通过 Schema 校验后才保存检查点；部分响应不保存成角色结果。角色检查点仅供同一冻结输入重建原图，不作为新 Memory。

旧八例的部分未知错误不自动恢复。显式恢复开关代表人为发起一次新的、无工具副作用的完整角色生成，可能再次产生费用，不是续接部分文本。新的瞬时错误才按上述策略有限重试。成功原案例禁止重新采样；成功的新恢复结果缓存封存，不重复请求。不同恢复目录不能用于挑选有利结果。

## 验收

验收结果：专项与客户端/代理回归 **61 passed（10.30 秒）**；原阶段 A/B/C/C.1/D.1–D.4.1、股票 Backtest/Memory/原生图相关回归 **447 passed（70.29 秒）**，共 508 项。`git diff --check` 通过。模拟验证覆盖连接建立失败、服务 503、限流/额度区分、读取超时、流中断、未知错误、退避上限、崩溃 STARTED、跨恢复总尝试上限、Schema/提示词/检查点变化阻断、额度停止、token 记录及完整九角色恢复。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_llm_recovery.py tests/test_llm_stream_reliability.py tests/test_model_proxy.py -q
.\.venv\Scripts\python.exe -m pytest tests/test_okx_traded_comparison.py tests/test_okx_agent_comparison.py tests/test_okx_cost_audit.py tests/test_okx_continuous.py tests/test_okx_diagnostics.py tests/test_okx_backtest.py tests/test_plan_lifecycle.py tests/test_perpetual_plan.py tests/test_okx_derivatives.py tests/test_okx_multitimeframe.py tests/test_okx_research.py tests/test_okx_graph.py tests/test_backtest.py tests/test_memory_log.py tests/test_memory_pointintime.py tests/test_graph_end_to_end.py -q
```

原八例真实输入的离线重建全部通过，共验证 13 个完整角色（7+6）；每例在首个未缓存角色处由模拟哨兵停止，**没有新决策，也没有真实模型调用**。原报告、14 例决策、A/B 账本及盲测协议未改写。

## PowerShell：只读源、独立输出的离线诊断

```powershell
cd D:\_01trading_agents
$sourceExperiment = 'C:\Users\jerry_su\.tradingagents\logs\okx_traded_comparison\1ceb89389f8bd70c46a1de9fbd0b9c7c3d8338a7552afd6f1faeb4db47290261'
$recoveryOutput = 'C:\Users\jerry_su\.tradingagents\logs\okx_llm_recovery\d4_2_audit'
.\.venv\Scripts\python.exe -m scripts.report_okx_llm_recovery --experiment $sourceExperiment --output $recoveryOutput
```

如已完成服务诊断，以下命令**会调用真实模型并可能产生费用**，一次只恢复一个案例。恢复目录与离线哨兵目录分开；本轮没有执行此命令：

```powershell
$freshRecovery = 'C:\Users\jerry_su\.tradingagents\logs\okx_llm_recovery\d4_2_exploratory_real'
.\.venv\Scripts\python.exe -m tradingagents.okx_llm_recovery --experiment $sourceExperiment --output $freshRecovery --candidate 0c4619572fcf974f2722920e5f372f299fe1abd85d75461f844ddfe39b92ec64 --recover --restart-incomplete-role
```

## 限制与结论

具备受控的后续研究运行与完整角色恢复机制；**尚不具备宣称远端服务已稳定的证据**。原八例仍失败，原六个 ACCEPT 不变。新恢复只能作为探索性分析，不能补写成原冻结实验，也不能当作更好的 A/B 收益。需要后续一次小额真实服务诊断/单案例恢复来取得具体服务错误码与稳定性证据。

当前机制是单进程/单活动实验的本地检查点，不提供跨机器分布式 exactly-once。服务收到请求但本机尚未写入完成结果时，仍存在费用不确定性，因此默认阻断自动重放。不能恢复原错误已丢失的 provider 信息，也无法消除 LLM 历史知识污染。

升级后的客户端代码指纹与原实验不同是预期结果；不放宽 D.4.1 的冻结代码检查，不修改原 seal 或协议。原始结果仍可离线读取；新研究使用独立版本和输出。本轮结束于 D.4.2。
