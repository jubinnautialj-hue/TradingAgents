# F1 最终补充验收

本轮结论：**补充验收项目通过；F1 整体继续按有条件通过管理**。
当前分支 codex/forward-validation-f1；基线 c16266672928c44b7769d8ac5f8b71599cfa1fbd。
本轮未修改项目源码、协议、策略、提示词或回测规则，只补充审计证据与本说明。未 commit/push，未开发 F2/F3，execution_enabled=false。

## 真实模型与完整研究

先执行一次原 probe_okx_llm 最小调用，完整返回OK：12.0726秒，input21/output5/total26 Token，零重试。
随后仅执行一次新研究：

```powershell
cd D:\_01trading_agents
$env:TRADINGAGENTS_LLM_MAX_RETRIES='0' # 仅本次进程环境；未改全局配置
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --trade-plan --taker-fee-bps 5 --forward-register
```

实际结果：Model AVAILABLE，workflow COMPLETED，核心数据 VALID，原生九角色报告均存在且非空；CLI退出码0。
研究约483.5秒。完整研究实际Token总量未由本次现有报告暴露，不用最小调用用量代替，也不估算费用。
多空辩论、风险讨论各一轮；V2.1—V2.4 四批可选证据开关均保持默认关闭，并非本次重新验收全部增强证据。

- research_id：c1da79c653d04666962d10e9dd51832c。
- registration_id：8a2327573a1b6d0e12e5174b2066034283afbdd41d26a82aad00d9a76b1949fe。
- 类型PROSPECTIVE，原动作WAIT，plan_status=WAIT，plan_valid=false，allow_new_risk=false，simulation_pool_eligible=false。
- 状态ARCHIVED_ONLY符合正常观望定义，不能当作模型故障或成交记录。
- 报告：C:\Users\jerry_su\.tradingagents\logs\reports\okx_BTC-USDT-SWAP_multi_f2b0b72d5a45\complete_report.html。

时间（2026-10-24，北京时间，UTC为下列时间减8小时）：

| 字段 | 时间 |
|---|---|
| 研究开始 | 14:11:52.322 |
| 统一cutoff | 14:11:59.231 |
| 数据最晚采集/本地可用 | 14:12:10.312 |
| 研究完成 | 14:19:55.824 |
| 正式登记 | 14:19:55.844 |
| 首根允许观察柱开盘 | 15:00:00 |
| 首根柱收盘后最早可检查 | 16:00:00 |

完成至登记20毫秒。首柱时间只是未来检查边界，本轮没有访问未来行情，没有自动跟踪或模拟成交。
1h、4h、1d最新收盘分别为UTC06:00、04:00、00:00，均早于cutoff，父子快照cutoff一致。
HTML/Markdown/JSON原文件与封存内容、父子快照指纹、result、协议版本/指纹以及审计事件链全部验证通过。
协议仍是forward-f1-v1；hash为7a1676999fd95a0173576403a6c2ef7a119d8b7d1d768ccdbdfeef8b2720a76e。
另加独立验收审计事件，未改原登记内容。

本次OI、当前资金费率、标记/指数/成交快照及盘口已STALE_DATA，衍生品整体PARTIAL；历史衍生品序列仍VALID。
过期状态和原始时间保留，没有延长TTL、移动cutoff、替换研究依据或将WAIT升级为有效候选。
两侧未来突破及后续回踩均尚未确认；本次WAIT无可执行入场、止损止盈或有效风险收益比，不提供实盘授权。

## 原始失败复核

原失败研究/登记648e415e0cbb99ba30a21a25cc106268febbabb3a0f69240a665d791e5ea0605保持不变，独立归类RESEARCH_FAILED。
安全来源为TradingAgentsStrategy_logs/okx_4h_2026-10-24_7ce22d0362ff.json的llm_failure字段：

- Market Analyst / MODEL_SERVICE_ERROR / APIError。
- partial_response=true，不能复用为完整角色输出，不能盲目重试。
- HTTP状态：原记录未提供；请求追踪ID/哈希：原记录未提供；diagnostics={}。

未保存的原始SDK错误信息或响应元数据不能事后重建，不据此判定是网络、额度、认证或服务端哪一种根因。
既有safe_diagnostics只允许记录HTTP状态、已知错误码、retry-after和请求ID哈希，不记录任意正文、Cookie或Authorization Header。
本轮另用纯模拟异常验证503状态和追踪ID哈希可保留、任意错误正文被排除；此夹具不是原失败的证据，不能据其503推断原失败状态。
新成功也只证明这一次完整调用可用，并未证明原故障根因已修复或长期稳定。

## 三种状态与兼容性

| 状态 | 验证证据 | 结果 |
|---|---|---|
| 正常WAIT | 本次真实研究及test_native_mock_workflow_registers_wait_and_default_off | 通过 |
| 有效候选 | 隔离确定性夹具test_valid_candidate_and_sealed_read | 通过；不冒充真实市场候选 |
| RESEARCH_FAILED | 原真实失败档案及test_failure_not_direction | 通过；不计正常WAIT或成功避险 |
| 默认关闭 | 模拟完整原生工作流先不带开关，确认不产生registry目录，再带开关登记 | 通过 |
| 历史导入隔离 | test_import_cannot_claim_prospective及时间/晚登记用例 | 通过 |

模拟用例均运行在pytest隔离目录，没有导入正式前向档案；真实新样本使用本轮cutoff，没有把旧研究回放成实时样本。

测试：F1/OKX CLI/预案相关93项通过（65.77秒）；模型可靠性/恢复/重试67项通过（7.97秒）；另有1个安全诊断模拟夹具通过。
本轮未改源码，不重复全量回归。上一轮全量1905通过、5项已知Windows失败的限制仍保留，不能报全量全绿。
原源码/协议指纹和146个冻结实验文件逐项与F1验收清单匹配。

## 证据位置与限制

结构化补充报告：docs/forward_f1_supplement_acceptance.json。
用户日志目录中保留forward_f1_final_probe_20261024_01、forward_f1_final_btc_20261024_01.txt、forward_f1_supplement_real_verification.json、两个supplement测试XML/文本和安全诊断夹具记录。

尚有：原失败缺HTTP/追踪元数据；单次成功不保证持续稳定；有效候选正式登记仅由确定性夹具验证；本地时间/哈希不能代替外部签名或WORM；5项既有Windows回归失败；Memory/Reflection没有自动学习；F2/F3未实施。
本轮没有实际入场、收益或方向准确率结论，也没有访问实盘交易接口。
