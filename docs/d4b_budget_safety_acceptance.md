# D4-B.0 本地验收记录 — 2026-10-26

结论：**安全守卫本地工程有条件通过；不批准真实研究或72小时调度启动。**

## 基线与变更边界

- 开发分支：`codex/d4b-budget-safety-guards`。
- HEAD/基线：`43e320d2c93ad5363ca71c31137babc426128b8a`，包含已验收R1.4提交。
- 所有原有已跟踪文件相对基线无变化；单独核对R1.4、预算旧实现、F1/F2/F3与交易预案亦无变化。
- 保留AGENTS.md和已有F3.1.1未跟踪诊断文件。本轮仅新增9个文件，未commit/push。
- 未部署、未启动D4-B、未启用F3.1、未调用真实模型或私有交易接口。

新增文件：

1. tradingagents/safety_budget.py
2. tradingagents/guarded_research.py
3. tests/test_safety_budget.py
4. tests/test_guarded_research.py
5. docs/d4b_safety_budget.json
6. docs/d4b_safety_budget.schema.json
7. docs/d4b_btc_research.service.example
8. docs/d4b_budget_safety.md
9. docs/d4b_budget_safety_acceptance.md

## 本地测试

新增专项最终收集78项，全部包含在最终成功回归中。覆盖：

- 原始与规范化SDK计量一致、缓存/推理子项不重复相加、缺失核心用量/算术错误、重复响应。
- 当前安装SDK真实Responses SSE解析路径的完整结束与截断，全部使用MockTransport，不访问模型服务。
- 逻辑调用与底层HTTP关联、HTTP错误、配额/认证类状态停止下一发送、重复发送拒绝。
- 单研究/日/周期预算、已知用量跨午夜保守占额、阈值超额后停止、操作余量检查。
- 线程与独立进程竞争、模型发送串行化、待决调用重启后不重跑。
- 每24小时最多一次、72小时最多3次、失败消耗任务、末期不足900秒不新开任务。
- 人工暂停/资源复核恢复、未知用量不可直接恢复、超时终止专属进程组、资源故障在模型前停止。
- 复用原生worker与F1的候选/WAIT/失败登记，回放记录隔离。
- status不创建缺失账本、配置/Schema一致、安全字段Literal[False]。

资源合同在Windows中使用隔离Linux元数据夹具验证，不等于在Vultr运行了新版cgroup任务。

首轮不排除的全量回归：

```text
2340 passed, 6 failed, 4 skipped, 13 deselected, 100 subtests passed
耗时503.21秒
```

6项失败在新的独立进程中定点执行全部复现，该执行没有加载新守卫模块：

|原有失败项|复核结果与范围|
|---|---|
|test_chatgpt_auth 两项0600断言|Windows返回POSIX mode 0666；仅临时模拟认证文件，不是读取真实凭据，未改认证代码|
|test_cli_updates 两项更新命令断言|Windows路径/安装识别返回None，原源码未变；本轮不修复更新器|
|test_opening_observation_f31 的未提交代码激活拒绝断言|当前旧F3.1代码已提交并与HEAD一致，故该“未提交”前提不成立；未改旧协议或测试|
|test_suite_isolation 的yfinance缓存断言|Windows临时目录位于用户HOME下面，与测试的路径前提不一致；未使用用户真实缓存|

随后补充模型发送串行化和周期末期时限测试，最终显式排除上述6项原有失败后执行其余全量：

```text
2342 passed, 4 skipped, 19 deselected, 100 subtests passed
耗时487.83秒
```

19项deselected包括原默认标记筛选的13项和本次显式排除的6项；不能将此结果表述为全分支零失败。
4项跳过为3项POSIX权限场景及缺少可选langchain_aws；未安装额外依赖。

依赖实际版本：openai 3.36.1，langchain-openai 1.7.2，langchain-core 1.6.13，
httpx 0.28.1，httpx2 2.16.0，pydantic 2.14.1。

## 固化前指纹

Windows本地实现字节组合指纹（两个新模块及两个旧复用模块）：

`e6a7a8d99c3db35ab754b31830810274875205bd4748c65db8ec049ad920fbc4`

默认策略语义指纹：

`e427b4a146415cb840d389f78ab1dc5f2b4bd21c6341497cc3cfa2e9d9190e94`

默认配置enabled=False、accepted_post_response_exposure=False，三项最终安全许可False。
未来Linux隔离部署必须核对目标字节及已审阅提交，不能复制Windows账本到新根或改旧哈希绕过校验。
目前新代码未提交，生产入口的reviewed-commit检查会拒绝启动，这是有意保留的上线阻断。

## 实机只读资源复核

2026-10-26 10:55:15 UTC通过现有授权SSH只读检查：2核/3392MiB，可用2282MiB，
磁盘约92GiB剩余，负载0.10/0.10/0.09，NTP同步；hermes、Codex、宝塔等已有进程保留，
未发现相关长期timer。没有运行新守卫或新研究，也没有修改正式目录和服务。

## 计量及上线限制

- D3.3原442522 Token仅旧汇总，无法追溯拆成逐调用。本轮没有回填或伪造。
- input/output/total原始核心字段必须齐全；缓存等细项未提供时UNKNOWN，不假定折扣。
- Token在完整返回后才能计量，默认600000/日/研究和1800000/周期是软阈值，
  一次在途请求仍可超出。100000余量不是经证明的最大暴露量。费用始终UNKNOWN。
- 仅约束经过新入口、同一审批账本的D4-B任务，不是旧CLI或整个共享账户的全局费用上限。
- 真实模型下的逐调用覆盖、订阅额度、认证刷新、序列化后的完整研究耗时、Linux cgroup行为
  均尚未通过本轮实机验证；不能从Mock SSE成功推定真实服务稳定。
- 正式F3.1候选成交/结算验收未完成，本轮不接入它；不影响仅研究阶段保持其关闭。

下一步需另行授权：代码固化与隔离部署、明确接受软预算在途风险、审批固定账本及认证目录，
然后先执行文档提出的一次低成本真实计量探针。不得自动开始72小时研究。
