# V2.1 第一批最终验收与固化准备

验收日期：2026-10-23。分支：`codex/okx-orderflow-spot-perpetual`。
结论：**有条件通过，可固化为可选的短窗口研究增强版本；不是完整小时 CVD 服务，也不是实盘版本。**

本轮只进行了验收、公开 GET 验证及证据归档，没有修改功能代码、策略、提示词或回测规则，没有执行 git add/commit/push。`execution_enabled=False`。

## 版本与完整性

- 基准提交：`a6d94f470c46eb2d017daf44fa0f01f43b3c0a6a`。V2.1 仍为未提交工作区，不能把基准提交当作 V2.1 提交。
- 冻结策略指纹：`b61aa92741f23a9db0120211654ab56317490b22878622adeb77fe538eca79cf`。
- perpetual_plan.py、plan_lifecycle.py、okx_multitimeframe.py 的原冻结字节校验全部通过。
- 原 D.4.1 实验 146 个文件逐一比对原档案哈希，全部未变。
- `.gitattributes` 固定上述三文件的原换行形式，避免 Windows checkout 改变冻结字节；multitimeframe 在状态中显示修改，但归一化文本 diff 为空，没有数值规则变化。
- `okx_orderflow_v2_1_manifest.json` 保存源码指纹、测试摘要和真实证据指纹。报告与原始逐笔数据保存在用户日志目录，不加入 Git。

## 原生流程接入验收

调用链：`okx-research --orderflow` → 原 preflight → collect_orderflow → state.orderflow_evidence → 原共享 get_instrument_context_from_state → Market Analyst / Bull / Bear / Research Manager / Trader / 三位 Risk Analyst / Portfolio Manager → 原 final_gate → evidence_view → Markdown、HTML、JSON。

没有新增角色或重建 Graph。context.py 条件性追加证据，原九角色均调用该上下文入口。专项 Mock Graph 测试验证至少九次模型调用；已有真实完整 BTC 研究验证 native workflow COMPLETED，角色文字引用了 CVD、冲突和时效限制。

原交易动作、数值预案、证据 ID 白名单不变。新增证据通过独立 `evidence.orderflow` 和角色文字呈现，不构成新的策略入场条件。默认 `okx_orderflow_enabled=False`；未传 `--orderflow` 时不采集、不追加上下文。开关专项测试和全量股票/历史功能回归均已执行，不能将已知五项测试失败描述成全绿。

## 计算与质量复核

- 实际 taker side 来自逐笔 side，不以 K 线颜色估算。永续 sz 是张数，币量=sz×ctVal×ctMult，USDT 名义量=币量×成交价；现货 sz 本身是币量。
- 同一 UTC cutoff，窗口为 `[floor(cutoff/60000)*60000-minutes*60000, floor(cutoff/60000)*60000)`，只纳入截止前成交及已确认分钟柱。
- 买卖、Delta 和 CVD 分别记录币量与 USDT。CVD 在窗口起点从零累计，不表示从上市起累计。现货与永续按相同窗口 VWAP 比较溢价，不用异步最后报价冒充同步溢价。
- 完整性同时要求分页到达窗口起点之前、逐分钟币量和 USDT 量对账通过。覆盖率是观测币量/分钟柱币量，不是逐笔绝对完整性的证明；不能用总量近似或百分比接近 100% 代替所有检查。
- 重复冲突、乱序、不推进、未来成交、缺柱、未收盘柱、换算异常及数量不符均受确定性校验。覆盖不足时不输出该市场正式 statistics/CVD，双边不全时不输出联动结论。
- 历史研究显式 DATA_UNAVAILABLE，不请求当前逐笔和现货替补历史。不宣称支持完整历史 CVD。
- TTL 最大 300 秒，按窗口结束时间计算。每次构建角色上下文及最终输出重新检查，过期 effective view 清除统计、CVD、背离和联动；原采集档案不覆盖。多 Agent 耗时可能使全部新增证据过期，这是已知限制。
- 半窗口极值/CVD 比较只是短窗口观察，不是已确认摆动背离；相关性、Delta 方向相反不证明现货或永续主导。

## 本轮真实公开 API 窗口验证

固定结束时间：2026-10-23 08:49 UTC（北京时间 16:49）。所有窗口共用 cutoff 和请求缓存，每个市场每次逻辑采集最多 60 页、每页 100 条，不追加预算追求完整覆盖。

| 窗口 | 永续条数/页数 | 永续币量覆盖 | 现货条数/页数 | 现货币量覆盖 | 双边结果 |
|---|---:|---:|---:|---:|---|
| 1 分钟 | 440 / 5 | 100%，1/1 分钟匹配 | 107 / 2 | 100%，1/1 匹配 | VALID |
| 5 分钟 | 2775 / 28 | 100%，5/5 匹配 | 707 / 8 | 100%，5/5 匹配 | VALID |
| 15 分钟 | 6000 / 60 | 44.2842%，9/15 匹配，未达起点 | 2173 / 22 | 100%，15/15 匹配 | PARTIAL，联动抑制 |
| 60 分钟 | 6000 / 60 | 6.0316%，9/60 匹配，未达起点 | 6000 / 60 | 16.4370%，32/60 匹配，未达起点 | PARTIAL，两边完整 CVD 抑制 |

四次合计 125 次底层公开 GET（120 次逐笔分页、2 次分钟柱、服务器时间、两种合约信息），复用缓存 134 次。缓存只在本次验收进程内存在，不代表新增长期缓存服务。约 65.5 秒为四次采集计算总耗时，不含初始元数据查询；没有模型调用。

5 分钟在本次市场活跃度下可完整采集，不保证今后总能完成；15/60 分钟仅支持请求和可靠降级，不能宣称在默认预算下支持完整 CVD。覆盖量不能换算为时间覆盖率。

官方文档：<https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-trades-history>。history-trades 文档描述最近三个月、最多 100 条/页、IP 限流 20 次/2秒；项目使用 0.25 秒请求间隔及最多三次有界重试。官方历史上限不等于本适配器承诺的可靠历史支持。实际可得性以本轮归档为准。

## 测试及真实研究证据

本轮全量：**1777 passed、5 failed、4 skipped、13 deselected、100 subtests passed，106.76 秒**。其中订单流离线专项 29 项包含在全量内，不重复计数。默认排除联网 integration；本轮真实窗口探针独立执行，成功和降级均按实际结果记录。

五项失败与已验收基准在 Windows 上复现的失败相同：两个 ChatGPT 登录文件 POSIX 权限断言、两个升级命令测试、一个 yfinance 用户 home 缓存隔离测试。四项跳过涉及 POSIX 权限和可选 langchain_aws 缺失。本轮未安装依赖或修复这些既有问题。

已有完整真实研究：`okx_BTC-USDT-SWAP_multi_8da1872bee47`，cutoff 2026-10-23 08:24:48.418 UTC。实际 HTML、research_result.json、orderflow_snapshot.json 已重新读取并保存 SHA256。

- 原始 08:23–08:24 UTC 成交窗口双边 VALID，永续 Delta −7.9215 BTC、现货 +0.0757491 BTC。
- 最终原生流程完成，WAIT / plan_valid=False / execution_enabled=False。
- 原始档案 VALID，最终 effective_view 和 research_result.evidence.orderflow 均 STALE_DATA。不能将这份过期证据用于当前入场。
- 本轮没有再次运行昂贵的完整模型研究，也没有做新旧版本固定快照 A/B，不能证明预测或收益改善。

本地详细档案：`C:\Users\jerry_su\.tradingagents\logs\okx_orderflow_v2_1_acceptance\`，含 pytest.xml/txt、source_audit.json、coverage.json、window_1m/5m/15m/60m.json、公开验证脚本 probe.py。旧报告未覆盖。

## 待提交范围与安全检查

已有修改：cli/main.py；agents/context.py、state.py；dataflows/okx_research.py；vendors/okx.py、okx_multitimeframe.py（换行固定）；default_config.py；graph/trading_graph.py；reporting.py。

新增：.gitattributes；vendors/okx_orderflow.py；tests/test_okx_orderflow.py；docs/okx_orderflow_batch1.md；本验收文档及 manifest。用户自有未跟踪 AGENTS.md 保留，不默认纳入建议提交。

对待提交源码、测试及文档做私钥头、API key、JWT 和长字面凭据模式检查，未发现命中；没有读取凭据库、.env 或认证文件。此为有限静态扫描，不是不存在任何敏感内容的数学保证。git diff --check 通过。无账户、交易、资金写入调用。

## 使用与固化建议（以下命令未执行）

```powershell
cd D:\_01trading_agents
# 可选的五分钟公开数据验证；不保证完整，默认不开启后台采集
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --orderflow --flow-minutes 5 --flow-max-pages 60 --data-only
# 旧模式：不传 --orderflow
.\.venv\Scripts\python.exe -m cli.main okx-research --instrument BTC-USDT-SWAP --interval 4h --multi-timeframe --data-only

git add -- .gitattributes cli/main.py tradingagents/agents/context.py tradingagents/agents/state.py tradingagents/dataflows/okx_research.py tradingagents/dataflows/vendors/okx.py tradingagents/dataflows/vendors/okx_multitimeframe.py tradingagents/dataflows/vendors/okx_orderflow.py tradingagents/default_config.py tradingagents/graph/trading_graph.py tradingagents/reporting.py tests/test_okx_orderflow.py docs/okx_orderflow_batch1.md docs/okx_orderflow_v2_1_acceptance.md docs/okx_orderflow_v2_1_manifest.json
git diff --cached --check
git diff --cached --stat
git commit -m "Add V2.1 optional OKX orderflow and spot-perpetual research"
git push -u origin2 codex/okx-orderflow-spot-perpetual
```

正式提交后另行记录提交 SHA；当前 manifest 是基准提交加工作区源码指纹，不冒称已生成发布提交。回滚研究行为只需去掉 --orderflow；无需触碰原历史档案或冻结策略。

## 遗留限制及停止范围

P1：15/60 分钟在预算内不保证完整；长模型研究会使五分钟 TTL 证据过期。P2：已有五项 Windows 测试失败；跨市场采集并非同时完成，仅统计窗口一致。历史逐笔/长期 CVD、持续 WebSocket、OI 同窗变化、策略收益贡献均未验收。

维持功能默认关闭，不把可选证据缺失转换为有效入场预案。后续如获授权可研究预算内分段缓存与时效展示；本轮不开发第二批，不新增 Agent，不购买服务，不提交或推送，不启用交易。
