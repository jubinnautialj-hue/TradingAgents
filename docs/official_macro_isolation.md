# official-macro-403-v1 本地实现与验收

基线4e60ccf961f6dcd2cf84e7c621b3bda5e6419aa7；分支codex/d4b-official-macro-isolation。
范围：本地实现、网络禁止的确定性验证。没有Vultr连接、真实模型、正式账本、调度或交易操作。此前已消费的一次性授权不恢复。

## 实际策略与来源

默认仍strict-v1；Policy新增显式official-macro-403-v1，bls-cpi-403-v1仍仅允许CPI。原预算JSON保持strict-v1且enabled=False。本轮不改变d4b-safety-budget-v1版本、费用UNKNOWN规则或预算阈值。

新策略仅限HTTPS、GET、无用户信息、无查询/片段、默认端口或443，以及以下精确主机/路径：

|source_id|主机|精确路径|
|---|---|---|
|fed_policy|www.federalreserve.gov|/feeds/press_monetary.xml|
|fed_speeches|www.federalreserve.gov|/feeds/speeches.xml|
|cpi|www.bls.gov|/feed/cpi.rss|
|ppi|www.bls.gov|/feed/ppi.rss|
|employment|www.bls.gov|/feed/empsit.rss|

MACRO_SOURCES为守卫内固定登记定义；每次匹配先核对原生FEEDS的URL、MACRO类别与非项目绑定属性。原生来源改变立即拒绝，不静默扩表。禁止路径前缀、子串、编码相似路径、非HTTPS及其他HTTP方法。调用者不能通过传入optional标签把核心数据、AUTH或模型请求变成宏观可选来源。

必需性依据：原生prepare_events逐源捕获不可用并保留元数据；bind_events接受空事件且不生成实际/预期值；News/Sentiment只解释同一固定快照，并被原提示词要求说明缺失。核心build_bundle/check_bundle与交易预案质量独立。五项是可选辅助发布标题，而非核心行情。完整来源及其他新闻调用顺序见optional_macro_sources_review.md。

SEC、项目RSS及OKX公告没有加入新豁免；它们403仍停止。其他HTTP状态没有新增降级许可，维持原规则；200代表HTTP正常，不等于解析或业务证据有效。此修复不保证任何一次完整研究必然成功。

## 请求、计量及停止

Session.send在真实发送前完成URL、方法、类型、CALL上下文检查；新策略仅适用于PUBLIC_GET且没有模型call_id。Ledger.begin_http再次核验固定来源，并将策略和source_id追加到原HTTP_STARTED。所有发送先占用原有HTTP预算。

只有接收到403且无传输失败、来源/类型/策略均匹配才追加OPTIONAL_SOURCE_UNAVAILABLE并继续；事件包含wire_id、固定来源、source_id、阶段、错误类别与原因，事件本身含UTC毫秒时间和哈希链。没有虚假CPI/PPI值、标题或方向。

所有受保护宏观请求在requests中提前关闭重定向；requests/httpx实际返回3xx会安全停止，目标主机不会被继续访问。认证401/403、网络/流式正文中断、模型部分响应/未知用量及原预算门禁不享有豁免。已有采集器重试如遇暂停，在再次发送前被PAUSED拒绝，不产生第二次HTTP；不增加重试范围。

HTTP预算或Token余量拒绝依旧按原路径阻断新增发送/调用，worker失败对账负责追加暂停，不更改其既有语义。账本锁、幂等HTTP完成、持久化计数、重启恢复与未知计量停止保持不变。

## 报告与异常链

OfficialFeedUnavailable统一403公开元数据，保留CPIUnavailable异常身份兼容旧调用。报告来源状态DATA_UNAVAILABLE（语义UNAVAILABLE）、http_status=403、failed_at_ms、failure_category、unavailable_reason及failure_stage。空事件不被转换成中性或有利信号。

HTTP_SAFETY_FAILURE在全局HTTP故障暂停前追加，包含wire_id、HTTP状态、固定阶段与原因，不保存请求正文、响应正文、头、凭据或任意异常文本。worker_diagnostic保留最多五个宏观失败元数据，旧optional_source_failure键继续提供CPI兼容视图；另以first_global_pause明确首次全局暂停及其HTTP事件关联，避免把可降级CPI故障误称为暂停根因。下游多周期error_context及原质量规则不变。

## 兼容性与指纹

|配置|CPI403|其他四项宏观403|核心/认证/预算规则|
|---|---|---|---|
|默认strict-v1|全局停止|全局停止|原样保留|
|bls-cpi-403-v1|精确降级|全局停止|原样保留|
|official-macro-403-v1|精确降级|精确降级|原样保留|

这只扩展显式可选来源策略，不改F1—F3/F3.1、C.1、阶段D或策略数值。F1不允许BLOCKED_DATA/失败占位WAIT成为有效候选；plan_valid、allow_new_risk、simulation_admitted及execution_enabled不受可选降级授权。

旧实现指纹e325d5ff13a963bdd9bb505cf1e2c7d8ec990abbfcf6565003b292e39e779782；本次定稿实现指纹e4d91175230e22cc04cb0bc1158d208602bf5484a42cdff762e610909d264f30。旧账本不得迁移、改写实现哈希、恢复暂停或重新初始化。未来运行使用已固化的新提交和独立新账本。未提交源码不能绕过reviewed_implementation用于正式运行。

## 文件范围

修改三个应用文件：tradingagents/safety_budget.py、tradingagents/guarded_research.py、tradingagents/dataflows/vendors/okx_events.py。
更新docs/d4b_safety_budget.schema.json；不修改默认预算JSON。
新增tests/test_official_macro_isolation.py，更新此前未跟踪的tests/test_optional_macro_design_review.py，使其验证新策略显式可用、默认仍严格。
新增本文。此前审查文档保持设计历史；AGENTS.md及F3.1.1未跟踪文件保留。

## 验证

所有新专项使用socket拒绝联网、内存响应、MockTransport、确定性模型及独立FIXTURE账本；没有读取正式凭据。实际覆盖guard → HTTP计量 → 官方采集 → 多周期原生质量 → 缺失报告 → 原News/Sentiment节点；另验证F1失败占位不进入候选池。

原生多周期客户端使用既有确定性MultiClient，三周期计算/质量逻辑未替换为强制通过。有效与缺4h两种情况分别验证：辅助源均403不暂停不等于核心必然有效。HTTP计数只统计通过HTTP发送层的请求，不把MultiClient内部确定性调用冒称为实机HTTP。

首轮保留两项测试失败证据：网络/正文故障的预期异常应是首个故障暂停后重试门禁返回PAUSED，而非ConnectionError；只修正断言，未放宽守卫。第二轮F1失败夹具因残留VALID_CANDIDATE状态被原Schema正确拒绝，已将夹具改为一致的RESEARCH_INVALID/WAIT，未修改Schema准入。

随后一次新增Token余量夹具将阈值恰好设为100，首次100 Token完成即按原规则暂停，无法继续测试“下一次调用前余量不足”。将夹具阈值改为150、余量仍100，验证下一次调用发送前拒绝；未修改产品预算规则。

定稿结果：

|范围|真实本地结果|
|---|---|
|实际守卫新增专项|45通过，19.03秒|
|包含新专项、原CPI/预算/认证/事件/多周期/原生图/F1/预案及设计测试的12模块|428通过，135.68秒；未排除用例|
|补充F2/F3/F3.1/多标的纸面四模块|195通过、1失败，216.98秒；未排除用例|
|固定4e60基线副本单独复现该失败|相同用例失败，9.12秒|

补充失败为tests/test_opening_observation_f31.py::test_init_is_opt_in_and_no_production_activation_of_uncommitted_code，断言在文件已固化时仍要求Reviewed异常。F3.1的_reviewed只检查其六个文件，这些文件当前及基线完全相同，故实际初始化隔离临时测试档案不抛此异常。基线从git archive导出，只读引用本地Git对象以保持同一HEAD审查上下文；没有改变主工作区、提交、旧协议或真实档案。不修改旧测试、不声称全量零失败，也不把这个用例的临时隔离资料当成正式纸面记录。

JUnit证据位于本机独立日志目录official_macro_local_20261028的related.xml、paper_compatibility.xml及baseline_f31_known.xml，不加入Git。设计测试与实际守卫专项、相关回归互有包含，不将数字重复累加。

```powershell
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_official_macro_isolation.py
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_official_macro_isolation.py tests/test_optional_cpi_isolation.py tests/test_safety_budget.py tests/test_guarded_research.py tests/test_auth_renewal_compatibility.py tests/test_okx_events.py tests/test_okx_multitimeframe.py tests/test_okx_research.py tests/test_okx_graph.py tests/test_forward_validation.py tests/test_perpetual_plan.py tests/test_optional_macro_design_review.py
```

配置示例仅表示显式选择，不授权运行：在新获审批的Policy中设置optional_source_policy="official-macro-403-v1"。原docs/d4b_safety_budget.json保持strict-v1、enabled=False、accepted_post_response_exposure=False。本轮不提供绕过审批的生产启动命令。

结论：READY_FOR_COMMIT（有条件本地工程验收，保留上述基线F3.1测试限制）。允许申请固化与随后Linux隔离验收，不等于已经有实机或完整研究成功证据。本轮不自动提交。

## Linux隔离实机方案（本轮不执行）

1. 本地验收后由用户单独批准版本固化；以新固定提交建立独立只读目录，复用已锁定的Python3.13/依赖环境并核验权限和哈希。
2. 独立测试目录和FIXTURE预算，显式启用新策略；先执行相同专项、认证、预算、数据质量与F1工作流回归，不访问模型。
3. 另获公开数据验证授权后，固定请求预算检查五来源与BTC核心接口；只对真实403记录降级，200不能冒充403实测。SEC等非白名单失败必须停止，不临时扩表。
4. 检查实际HTTP与账本、异常链、报告缺失状态，比较旧失败账本及业务档案哈希。模型/Token未知计量分支通过确定性测试，不借机发真实模型请求。
5. Linux验收后，单独审批新账本、单次预算、原子启动门禁及一次BTC完整研究；不使用旧授权、旧账本或旧日程。

本轮安全字段全部False；不自动Commit/Push、部署、启用timer或F2/F3/F3.1。
