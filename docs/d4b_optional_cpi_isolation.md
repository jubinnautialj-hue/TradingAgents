# D4-B：精确可选CPI来源降级与异常链保留

本修复基线为d59064fb783d8144ffb516ce25190e7691ceea32，分支codex/d4b-optional-cpi-error-chain。
仅本地开发与模拟验证；不部署Vultr，不恢复旧活动，不创建定时任务，不调用真实模型或行情。
execution_enabled=False、allow_new_risk=False、simulation_admitted=False。

## 范围与运维修复分离

D4-B.4的PrivateTmp=yes及activate_campaign.py失败输出修复属于独立运维文件，保存在本机运维日志目录，不在此基线Git树中。本修复不复制或提交这些文件；旧服务器部署、失败预算账本及授权均保持不变。
AGENTS.md与已有F3.1.1未跟踪文件不属于本次交付。

## 必需与可选来源

| 来源 | 研究角色与原有约束 | 本轮变更 |
|---|---|---|
| OKX时间、合约规格、1d/4h/1h闭合K线 | 核心研究必需；统一cutoff、预热、连续性、收盘及时间有效性不变 | 不降级放行 |
| 已启用的OI、资金费率、盘口等衍生品证据 | 按既有配置、质量闸门和入场重新验证执行 | 不改变缺失或TTL规则 |
| V2.1—V2.3订单流、跨市场、深度等增强 | 原可选开关及原质量语义 | 不改变 |
| V2.4官方宏观/项目新闻 | 可选辅助证据；不生成缺失数据，不单独决定交易方向 | 仅精确CPI RSS的403可显式按来源降级 |
| 模型与原生认证 | 完整多Agent研究的必要服务；计量、权限、失败必须安全停止 | 不豁免 |

CPI的可选性依据okx_research.preflight和okx_events.prepare_events/bind_events：新闻按来源记录缺失，核心研究由既有行情质量决定。此前预算守卫对所有403一律暂停，造成可选新闻反向阻断核心行情。

## 显式配置与精确匹配

Policy新增独立版本化字段optional_source_policy：

```json
{"optional_source_policy": "strict-v1"}
```

这是默认值；原有严格403停止语义保持。docs/d4b_safety_budget.json仍enabled=false、accepted_post_response_exposure=false，预算数值未变。
新周期如另获批准，才可显式使用：

```json
{"optional_source_policy": "bls-cpi-403-v1"}
```

该配置不会自行启用运行、改变风险许可或审批任何周期。
唯一可降级组合：GET，HTTPS，www.bls.gov，端口默认443或显式443，精确路径/feed/cpi.rss，无用户名密码、查询串、fragment或混淆字符，非模型逻辑调用上下文，HTTP403且非传输异常。
原生FEEDS中的CPI地址还必须与这一已审核地址一致；变更原生地址会拒绝而不是自动扩大范围。
不是基于域名前缀、子串、任意PUBLIC_GET或调用者提供的optional布尔值判断。Ledger再次检查配置、来源、调用上下文与请求类别；必要OKX、AUTH及MODEL请求不能伪装为可选来源。

## 成本、审计与停止规则

每次发送仍先通过预算检查，追加HTTP_STARTED与HTTP_FINISHED，计入研究、UTC日和全周期HTTP预算。
符合规则的403额外追加OPTIONAL_SOURCE_UNAVAILABLE，记录固定来源、规则版本、wire_id、403、HTTP_APPLICATION_ERROR、HTTP_403与available=false；UTC时间由原事件元数据保存。
事件不会增加Token，也不会被视为成功研究或有效CPI证据。
新闻报告来源包含http_status、failed_at_ms、failure_category和unavailable_reason；不生成CPI数值、预期或宏观结论。

其他来源403、CPI的401/429/5xx等原有停止状态、请求超时/连接异常、认证异常、未知用量、用量不一致、预算异常继续原有安全停止。403不会自动重试。
启用精确策略后，该CPI请求的重定向被拒绝：Requests关闭自动跟随，httpx单次发送层收到3xx即暂停并阻止后续转发。没有通过重定向拓展来源范围。
模型与认证原有精确白名单不变。HTTP限额阻止新发送；原worker失败清算继续追加全局暂停。原暂停与恢复原因白名单完全不变。

## 异常链与worker诊断

build_multitimeframe保留配置、服务器时间、规格、衍生品、逐周期K线及分析阶段。失败结果保存error_context：阶段、异常类别及允许名单中的错误码；不记录任意异常正文。
上游已失败的bundle继续返回无效，不再因timeframes={}二次覆盖PAUSED等根因。对于声称VALID却不含三个必需周期的输入，仍拒绝为RESEARCH_INVALID；没有放宽核心质量检查。

worker失败时在本周期ledger根目录的diagnostics/保存每run_id最多result/exception两份有限记录：固定错误码、最多8个栈帧的文件名/行号/函数名，必要的核心错误及精确CPI状态。
不保存凭据、环境变量、请求头、原始提示词、模型输出、原始市场数据或任意异常文本。报告必须位于同一周期batch树内；run_id限制为32位十六进制，防止诊断路径逃逸。已存在的诊断不覆盖；诊断写入失败不代替原业务失败，也不恢复预算。
失败研究仍由原有F1流程归档，不能因生成报告或WAIT占位而成为有效候选。

## 旧账本与实现指纹

safety_budget.py及guarded_research.py变化会改变implementation_hash。旧D4-B.4账本因此不能在新实现下直接使用；保持IMPLEMENTATION_CHANGED拒绝，不写回指纹、不迁移、不清零。
旧活动的TRANSPORT_OR_SERVICE_FAILURE和RESEARCH_FAILURE_OR_RECONCILIATION仍不能由resume清除。本轮不恢复10月29日时段，原截止不变；10月27、28日均不补跑。
F1/F2/F2.1/F3/F3.1协议、C.1生命周期、阶段D账本与交易策略不变。okx_multitimeframe中的策略计算函数保持原样，仅改失败诊断与校验顺序。

## 验证方式与后续审批

专项使用网络禁止夹具、MockTransport、独立FIXTURE预算和现有多周期/F1测试资料。覆盖精确匹配、403后必需取数仍允许、来源缺失、核心缺口仍阻断、错误状态/认证/重定向/传输/未知计量仍停止、计数及重启幂等、根因不被覆盖、诊断脱敏和旧指纹拒绝。
最终结果：新增专项48项通过；定稿后六个核心相关模块共236项通过，耗时81.59秒。较大范围的18模块相关回归655项通过，耗时284.58秒，包含F1/F2/F3、C.1、自动研究、调度、股票Backtest及Memory测试。两组有重叠，不相加，也不是整个项目全量测试。
较大范围回归后增加的流式断连等边界已包含在定稿236项中。没有真实模型、行情或Vultr验收。
测试过程保留两次未通过记录：第一次默认配置JSON未同步新增strict-v1字段（已修正）；一次测试运行期间修改源码，使夹具账本按设计拒绝IMPLEMENTATION_CHANGED。随后冻结源码，未排除用例，重跑236项全部通过。
本地JUnit及版本证据保存在独立运行日志目录，不纳入Git。

旧实现指纹为0270fd17c43a6493582fbe8388117c100e7f2e08516e4950ffcbcf66ea89be67；本次定稿指纹为e325d5ff13a963bdd9bb505cf1e2c7d8ec990abbfcf6565003b292e39e779782。
已确认C.1、F1/F2/F3/F3.1、阶段D账本、research_budget.py及auto_research.py相对基线无差异；structure_rules、analyze_timeframe、confluence三个策略计算函数AST保持不变。

文件清单：tradingagents/safety_budget.py、tradingagents/guarded_research.py、tradingagents/dataflows/vendors/okx_events.py、tradingagents/dataflows/vendors/okx_multitimeframe.py、docs/d4b_safety_budget.json、docs/d4b_safety_budget.schema.json、tests/test_optional_cpi_isolation.py及本文。
后续PPI、就业等其他来源如果返回403仍按原规则暂停；没有证据或本轮授权扩大白名单。本次不保证完整研究已经能够成功。

验收建议：有条件通过本地工程验收。建议在代码审阅、用户授权版本固化后，另行申请独立Linux目录的隔离实机验证；当前不部署或运行。

未来只能申请新运行周期：先完成代码验收与用户批准的版本固化，再隔离部署固定新提交、Linux模拟测试、公开源只读验证；如需真实模型/完整研究须另外批准预算。新周期使用独立预算根目录、新T0/截止、明确的optional_source_policy、调用与资源限制及单独启动授权。
旧失败活动永久保留，不以新周期冒充旧第三时段。当前改动未提交时，reviewed_implementation仍会阻止正式生产启动。

本地示例（无真实模型或服务器部署）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_optional_cpi_isolation.py tests/test_safety_budget.py tests/test_guarded_research.py tests/test_auth_renewal_compatibility.py
```
