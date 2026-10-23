# Vultr 模型专用后台隧道

2026-10-22 配置。无需继续开着 SSH/PowerShell 隧道窗口，不保存 root 密码。

## 当前设置

- 服务器：202.182.110.153:22；用户 ta-tunnel。
- 本机 SOCKS：127.0.0.1:10809，仅监听本机。
- 专用私钥留在 C:\Users\jerry_su\.ssh\tradingagents_vultr_ed25519。
- 服务器公钥限制允许连接 api.openai.com:443 和 auth.openai.com:443，禁止命令执行。
- 后台工作程序使用 BatchMode、严格主机密钥校验、SSH 保活和断线重连。
- 当前用户登录 Windows 时启动；电脑休眠、断网或关机时仍会中断，恢复网络后重连。
- 已创建登录自启动入口，没有为验证而重启或注销用户。

模型配置是新建的 C:\Users\jerry_su\.tradingagents\model_network.json：

```json
{"llm_proxy":"socks5h://127.0.0.1:10809"}
```

TradingAgents 自动读取它，原有研究命令无需添加代理参数。它只影响 OpenAI/
ChatGPT 模型和现有订阅认证请求（续期、公钥、模型列表等），不改变 OKX、
Yahoo、其他模型供应商或全局代理。密钥、订阅凭据不存放在服务器端；HTTPS
证书验证保持开启。模型同步、异步 HTTP 客户端均显式指定隧道并关闭系统代理
自动读取，避免 Windows 系统代理覆盖本次配置。隧道不可用时报错，不自动换回直连。

TRADINGAGENTS_LLM_PROXY 可覆盖保存设置；设为空字符串可显式禁用。
TRADINGAGENTS_MODEL_NETWORK_FILE 可指定另一份设置文件。损坏的设置文件报错，
不会静默绕过。程序化 config 的 llm_proxy=None 也可关闭该路由。

## 日常运行

在 D:\_01trading_agents 下，仍然运行原命令：

```powershell
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h
```

检查后台状态：

```powershell
powershell.exe -NoProfile -File .\scripts\windows\vultr-model-tunnel.ps1 -Action Status
```

临时停止隧道（模型请求将失败，不会回退其他线路）：

```powershell
powershell.exe -NoProfile -File .\scripts\windows\vultr-model-tunnel.ps1 -Action Stop
```

重新在后台启动：

```powershell
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile -WindowStyle Hidden -File "D:\_01trading_agents\scripts\windows\vultr-model-tunnel.ps1" -Action Run'
```

工作脚本使用互斥锁避免同一会话重复启动。停止操作只终止与记录中的父进程、
用户、主机和专用密钥路径相符的 SSH 进程，不终止原先手动开的 root 隧道。

## 本机文件与撤销

- 工作脚本：D:\_01trading_agents\scripts\windows\vultr-model-tunnel.ps1
- 新建服务器连接配置：C:\Users\jerry_su\.tradingagents\tunnel\vultr-config.json
- 工作状态和最近 SSH 错误：C:\Users\jerry_su\.tradingagents\tunnel\
- 登录启动项：C:\Users\jerry_su\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\TradingAgents Vultr Model Tunnel.lnk

停止自动维护可先运行 Stop，再移除这一个启动快捷方式；将模型配置中的
llm_proxy 改成 null 可恢复原网络路径。不需要删除 SSH 密钥、旧报告或项目文件。
停止后台隧道不等于取消下次登录自启动，需单独移除启动项。

可选依赖声明在 pyproject.toml 的 socks-proxy 中，本机已安装 socksio 1.0.0
及 PySocks 1.7.1。没有升级 OpenAI 或 LangChain 依赖。

## 验证记录

- 专用用户密钥认证、严格主机校验通过；未读取私钥内容。
- 后台隧道通过公开 HTTPS 请求返回预期的未认证 HTTP 401。
- 主动结束本次工作程序持有的 SSH 子进程后，后台自动重新连接且端口恢复。
- 实际模型客户端端口为 10809，trust_env=false，未采用系统的 7897 代理。
- 订阅公开签名密钥请求成功，最小 gpt-6.1-sol 调用完整返回 OK，总计约 9.62 秒。
- 相关回归测试 234 项通过；最终代理配置专项复验 13 项通过。另一次包含认证文件权限测试的运行存在两个既有 Windows POSIX 0600 断言失败，未把它们计为通过。
- 通过持久化隧道设置执行 BTC-USDT-SWAP 4h 完整研究成功，退出码 0：Data quality=VALID、Model=AVAILABLE、workflow=COMPLETED、execution_enabled=False。
- 本次独立报告目录：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_4h_1b5ba9402758；已核验 complete_report.md、complete_report.html 及五组 Agent 分报告存在，未覆盖旧报告。
- 单次真实完整运行通过不代表未来永不中断；断线重连和模型错误分类仍需保留。

没有修改 Codex 全局 MCP、系统代理、OKX 凭据或任何交易能力。已有手动
10808 隧道保持原样，确认后台线路后可以自行关闭它及原 root 登录窗口。
