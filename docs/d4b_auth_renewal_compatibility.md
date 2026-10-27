# D4-B.1.1 认证续期白名单兼容修复

## 范围与结论

本地工程有条件通过；尚未进行新版 Vultr 实机续期验证。

- 基线：`fddc5111e87c1cd952ef9404347975fa2a8de9b9`。
- 工作分支：`codex/d4b-auth-renewal-compatibility`。
- 仅修改预算守卫，新增确定性测试与本说明；未提交、推送、远程部署或发起真实模型请求。
- 不改变冻结策略、F1—F3/F3.1、预算算法、默认预算配置及历史档案。
- `execution_enabled=False`、`allow_new_risk=False`、`simulation_admitted=False`；默认预算仍未启用。

## 根因与调用关系

原 `tradingagents/guarded_research.py` 的 `endpoint()` 将认证 POST 路径硬编码为 `/oauth/token`。
原生 `tradingagents/llm_clients/chatgpt_auth.py` 的 `TOKEN_URL` 则是
`https://auth.openai.com/api/accounts/oauth/token`。

调用链为模型适配器调用原生 credential/access_token，必要时进入 `_refresh()`，先请求 `JWKS_URL`，
然后由 `_token_request()` POST 至 `TOKEN_URL`。原守卫在网络发送之前拒绝后一请求。
这与 D4-B.1 已报告的签名公钥 HTTP 200、续期 POST 零发送、模型 POST 零发送一致；
本轮没有重新检查远程原始日志，也不将此根因推广到此前其他 APIError。

## 修改文件与边界

1. `tradingagents/guarded_research.py`：
   - 路径唯一来源改为原生客户端的 `TOKEN_URL` 和 `JWKS_URL`；可信认证主机仍固定为 `auth.openai.com`。
   - 要求 HTTPS、默认端口或 443；禁止 URL 用户信息、反斜线、制表符和换行。
   - 认证 POST 精确匹配 TOKEN_URL 路径，不允许 query、fragment、尾随斜线、相似路径或前缀匹配。
   - 签名公钥只允许精确 JWKS GET；认证主机上其他方法或路径拒绝。
   - 仅在原生 `_refresh()` 的进程内上下文允许续期 POST，不开放登录、撤销或任意 POST。
   - 原生 `_refresh()` 仅被透明包裹；守卫不解析表单、响应正文、凭据或登录文件。
   - 续期异常先写入 `AUTH_RENEWAL_FAILED` 暂停事件，再交还原生异常处理；旧登录回退不能掩盖失败。
   - 模型 POST 收紧为两个精确端点：`api.openai.com/v1/responses` 和
     `chatgpt.com/backend-api/codex/responses`；不再接受任意 `/responses` 后缀。
   - 研究公开 GET 保留查询参数，但守卫内统一要求 HTTPS；旧版未启用此守卫的 CLI 不受影响。
   - 对认证、公钥和模型请求禁用重定向。Requests 在发送前设置 `allow_redirects=False`，
     httpx/httpx2 在单次请求返回层拒绝 3xx，保证外层重定向循环不能继续发送。
     同主机、跨主机及 HTTPS 降级重定向均拒绝并暂停，不能通过跳转扩大白名单。
   - 认证非 200 响应立即拒绝并暂停，不触发守卫重试。
2. `tests/test_auth_renewal_compatibility.py`：60 项新增隔离测试。
3. `docs/d4b_auth_renewal_compatibility.md`：本说明与重新验收计划。

原生认证实现、Token 归一化/对账代码、`safety_budget.py`、资源限额及默认配置均未修改。

## 预算与安全验证

测试使用临时合成登录、测试 RSA 签名及模拟 HTTP，不读取真实登录文件，不访问外网。
原生刷新函数、签名验证、文件锁、原子写入、实际安装的 SDK、完整 SSE 用量转换与守卫共同执行。

- 正常链路：1 次逻辑模型调用；JWKS GET、续期 POST、模型 POST 共 3 次 HTTP；
  模型用量 input=80、output=20、total=100、cached input=30、reasoning=7，均为固定测试值。
- 公钥/续期 HTTP 计入适用的持久化 HTTP 预算，不产生独立模型 Token 记录；缓存与 reasoning 不重复累加。
- 只有 2 次 HTTP 预算时，公钥和续期耗尽预算，模型请求在发送前被拒绝。
- 非法主机、非 HTTPS、其他端口、用户信息、相似路径、旧路径、任意 POST、其他方法、重定向均被拒绝。
- 认证 400/401/403/408/429/500/503、连接失败、HTTP 200 但不可解析的续期数据均暂停；
  后者分别覆盖旧登录已过期和仍短暂有效的情形。
- 重定向测试验证实际模拟传输没有发送到第二个目标，而不只是检查最后响应。
- 原有部分响应、模型错误、UNKNOWN 用量、计量不一致、重试暴露、并发与重启保护继续通过原专项。
- 暂停账本不能因新守卫自动恢复；实现指纹不一致仍拒绝载入，文件字节保持不变。

守卫实现变化会自然改变预算账本的 `implementation_hash`。这是原有保护的预期行为，
不得改写旧账本的指纹、清零失败账本或迁移旧授权以绕过检查。
24 次逻辑调用、240 次 HTTP、900 秒、未知用量停止及请求后 Token 暴露边界保持原样。
Token 不是实际货币费用，也不是已验证的请求前硬上限。

## 本地测试记录

最终专项及必要兼容回归：**518 passed, 3 deselected，202.65 秒**。

运行模块：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_auth_renewal_compatibility.py tests/test_safety_budget.py tests/test_guarded_research.py `
  tests/test_chatgpt_auth.py tests/test_llm_stream_reliability.py `
  tests/test_forward_validation.py tests/test_forward_tracking.py tests/test_forward_paper.py `
  tests/test_paper_instruments.py tests/test_opening_observation_f31.py `
  tests/test_auto_research.py tests/test_automation.py tests/test_ubuntu_r14.py `
  -q --tb=short `
  -k 'not test_a_token_near_expiry_is_renewed_with_the_stored_refresh_token and not test_signing_in_by_pasting_the_redirect_saves_a_private_sign_in and not test_init_is_opt_in_and_no_production_activation_of_uncommitted_code'
```

其中包括新增 60 项、原 D4-B.0 的 78 项，以及原认证、流式故障、F1—F3/F3.1、多标的与 Ubuntu 部署兼容测试。
实际 SDK + 原生 credential + 合成登录续期 + 完整 SSE 的端到端测试通过，不能等同于真实服务验收。

排除的 3 项为先前已知问题：

- 原认证续期与登录测试的两处 POSIX 0600 断言，在 Windows 上与文件模式表示不一致。
- F3.1 的一项测试假定实现尚未提交，而当前基线已包含提交实现。

本轮还曾启动未过滤全项目测试，但在完成前主动中断，以补充原生续期回退保护；
该次运行不是全量回归结论。补充后的最终验证是上述 518 项，**不能描述为全量零失败**。
此前全量另有两项 CLI 安装路径判断和一项 yfinance Windows HOME/TEMP 路径假设失败；
本轮未修复这六项既有问题，也未重新完成未过滤的全量套件。

Git 检查仅有守卫这一项已跟踪代码变动；其余已跟踪文件与基线一致。
原有未跟踪 AGENTS.md 与零成交量诊断四文件保留，不属于本次改动。

## Vultr 隔离重验方案（尚未执行）

1. 本地验收获批并另行固化提交后，获取明确的新提交；不热修原固定提交目录。
2. 在新的独立验证目录部署该提交，核对 HEAD、文件 SHA-256、依赖、最小权限、资源与时钟。
   不覆盖 R1.4、F3.1、认证材料、正式前向档案或 D4-B.1 失败账本。
3. 先执行 Linux 上新增 60 项及原 78 项专项，按已披露的兼容边界复核所需回归。
4. 原 D4-B.1 账本保留原字节和暂停状态。新的隔离诊断账本必须由用户单独批准；
   不继承或重置旧授权，不绕过实现指纹校验，不自动启用默认预算。
5. 在独立实机授权下，只允许原生 SDK 续期已有 Ubuntu 登录；不读取、复制或输出凭据。
6. 经新守卫做 1 次固定短输入探针；成功且计量一致后，最多追加 2 次间隔请求。
   认证、网络、模型或计量失败即停止。记录路由类别、HTTP 状态、请求标识哈希、耗时和可获得的数值用量，
   不记录表单、请求/响应正文、Cookie、Authorization 或原始令牌。
7. 核对原生续期只计 HTTP、模型逐调用与聚合一致、UNKNOWN 停止、重启不恢复或重置额度。
   不运行完整研究、不启动 F2/F3、不安装调度、不据此自动批准 72 小时运行。

本轮不证明真实续期服务或长期稳定性。建议完成上述隔离实机重验后，再审查 D4-B 的启动前置条件。
