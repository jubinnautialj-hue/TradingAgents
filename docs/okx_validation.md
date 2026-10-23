# 第一阶段交付与验证记录

验证日期：2026-10-22；Windows 11；项目 `.venv` Python 3.13.13。

## Git 与安全边界

- 初始分支 main，唯一未跟踪内容为已有 history_report/。
- 开发分支 feature/okx-usdt-perpetual；未提交、未 push、未 Fork、未修改远程。
- 既有报告、个人配置和原供应商未覆盖。新报告使用唯一目录；OKX 状态日志使用唯一文件名。
- 未读取/复制/输出 OKX 凭据明文；未调用账户、订单、资金划转或账户设置接口。
- MCP 仅执行 system_get_capabilities；其结果为已认证实盘、服务端非只读。
  这不能证明 API Key 的具体权限，权限未作试探。研究运行时不连接该 MCP。
- 研究适配器仅允许三个公开 GET 路径；不包含实盘或模拟执行能力。
- 为运行项目已声明的测试依赖，使用 uv 向现有虚拟环境补装 pytest、
  pytest-subtests 及其测试依赖；没有重建或覆盖虚拟环境。ruff 通过 uvx 运行。

## 实际验证结果

| 验证 | 结果 |
|---|---|
| OKX 数据与完整图模拟测试 | 40 项通过 |
| 股票/CLI/报告等重点回归，包含上述 OKX 测试 | 146 项通过 |
| 图、检查点、报告等另一组重点验证 | 78 项通过 |
| 公开行情联网集成 | 4 项通过：1h、4h、1Dutc 各 400 根；2026-10-01 历史 1h 截止时间 |
| 完整非 integration 测试最近一次运行 | 1393 通过、5 失败、4 跳过；另有 100 subtests 通过 |
| 原始 HEAD 源码快照对照 | 同样的 5 项失败全部复现 |
| 修改范围的 ruff 与 git diff --check | 通过 |

全部 OKX 模拟测试无网络；公开联网测试仅访问时间、合约信息、历史 K 线。
核心数据无效时不调用 LLM。另以完整 LangGraph 测试模拟模型返回 Buy，
并在返回时让数据超时，最终结果确实变为 REVIEW、STALE_DATA 和
allow_new_risk=false。最终报告写入也再次检查；执行权限始终为 false。

五项既有 Windows 失败，未修改其测试或产品逻辑：

- test_chatgpt_auth.py 两项：断言 POSIX 0600 文件模式，Windows 返回不同模式。
- test_cli_updates.py 两项：安装升级命令的路径/环境假设。
- test_suite_isolation.py 一项：假定临时目录不在用户 home 下，Windows TEMP 在 home 内。

原始快照从本地 HEAD 导出到独立临时目录验证，没有切换或覆盖当前源码。
不能将上述结果表述为“全量测试全绿”。完整真实 LLM 研究未运行；完整流程
用模拟模型验证，真实联网验证针对公开数据和数据校验 CLI。

实际执行的 CLI 数据校验产生了独立报告：

`C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_1h_2011c4ef2912\complete_report.html`

该报告为采集时点的数据证据，不是持续更新的交易信号。

## 新增核心调用链

CLI okx-research / TradingAgentsGraph.propagate
→ Data Preflight / preflight
→ OKXPublicClient.get + instrument + candles
→ validate_instrument + validate_candles + build_bundle + check_bundle
→ 单份 market_snapshot
→ 行情工具 render_prices / render_indicator / render_snapshot
→ 原有研究、交易建议、风险辩论、投资组合经理
→ Data Final Gate / final_gate
→ record_decision / save_reports / checked_report_state。

snapshot 指纹、UTC 截止时间、已收盘柱、单位、预热和新鲜度都由程序检查。
指标周期是 K 线根数，1h 的 200 SMA 不是 200 日均线。研究许可不等于下单许可。

## 完整文件清单

修改现有 12 个文件：

1. D:\_01trading_agents\cli\main.py
2. D:\_01trading_agents\cli\prompts.py
3. D:\_01trading_agents\tradingagents\agents\analysts\market_analyst.py
4. D:\_01trading_agents\tradingagents\agents\state.py
5. D:\_01trading_agents\tradingagents\agents\tools.py
6. D:\_01trading_agents\tradingagents\dataflows\router.py
7. D:\_01trading_agents\tradingagents\default_config.py
8. D:\_01trading_agents\tradingagents\graph\propagation.py
9. D:\_01trading_agents\tradingagents\graph\setup.py
10. D:\_01trading_agents\tradingagents\graph\trading_graph.py
11. D:\_01trading_agents\tradingagents\report_html.py
12. D:\_01trading_agents\tradingagents\reporting.py

新增 8 个文件：

1. D:\_01trading_agents\tradingagents\dataflows\vendors\okx.py
2. D:\_01trading_agents\tradingagents\dataflows\okx_research.py
3. D:\_01trading_agents\tests\test_okx_research.py
4. D:\_01trading_agents\tests\test_okx_graph.py
5. D:\_01trading_agents\tests\test_okx_public_integration.py
6. D:\_01trading_agents\docs\okx_research.md
7. D:\_01trading_agents\docs\okx_mcp_inventory.md
8. D:\_01trading_agents\docs\okx_validation.md

history_report/ 是原先已有的未跟踪内容，不属于本次新增实现。

## 当前限制与下一阶段

一期只启用 market 分析师，再沿用原有多空研究和风险决策链。新闻、社交、
企业基本面尚未映射至 SWAP；不允许静默使用 Yahoo 现货替代。当前合约信息
明确标记为当前元数据，不宣称历史最小数量或精度已经重建。缓存仅限本次研究
快照，未实现磁盘缓存、跨进程限流或持续监控。

下一阶段优先为公开资金费率、OI、标记/指数价格定义独立数据类型；逐接口
验证历史保留范围、时间字段和单位，按 available-at 截止时间对齐 K 线。
深度先做带采集时间的价差、分档深度及假设规模的滑点，不把快照当历史簿。
WebSocket 和本地历史归档之后再考虑批量扫描、机会发现。强平历史只在官方
确实提供对应覆盖时接入，不能从当前数据推造过去。

模拟交易应独立实现本地账本、费用、资金费率和滑点；默认不持有生产凭据。
若选择官方模拟盘，必须使用独立模拟凭据和明确环境隔离。人工审批应绑定
合约、方向、规模、快照指纹和有效期，研究评级不能自动成为执行授权。

回滚可通过恢复 stock 模式/原有命令停用新增路径；Git 修改保持未提交，
方便逐文件审阅。不要使用清理未跟踪文件的方式回滚，以免删除既有报告。

运行方式见 okx_research.md；当前 MCP 完整工具分类见 okx_mcp_inventory.md。
官方字段依据：https://www.okx.com/docs-v5/en/ 。
