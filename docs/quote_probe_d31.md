# D3.1 公开报价诊断与重验准备

基线：01b82f435bb3631d3325e3a440b356d32bbb1312。
修复分支：codex/d31-quote-probe-diagnostics。仅新增诊断入口、专项测试和本说明，不修改任何已跟踪冻结源码、F3时序或旧档案。

## 已知事实与未知根因

D3原探针在BTC/ETH完成后返回ValueError。`forward_paper.Market.opening`可能在目标柱匹配/时间检查、confirm/成交量检查、数字解析处拒绝；`ubuntu_operations.run_probe`仅封存异常类，不封存异常消息、失败标的或响应。旧日志不能确定SOL的具体分支，不能归因于网络超时。

旧03:00结果SHA256保持：b9785cadb099b9561bf42cbf90759e25f20ca530426d18007a621ed07d4a693c。

## 实现

新增 `tradingagents/quote_probe_diagnostics.py`：

- DiagnosticClient继承原PublicBudgetClient，以进程内、单线程、临时Session.send观察钩子记录实际HTTP尝试次数/状态/时间；原GET白名单、限速、有限重试均不改变。
- 记录逻辑请求开始/结束、HTTP发送/接收或失败时间，分别区分限速等待与HTTP耗时；时间使用epoch毫秒及明确UTC。
- 每次仅保存最多两根K线的类型、长度、timestamp/open/合约成交量/confirm，不保存未来HLC，不保存请求头、Cookie、Token、URL参数或无限响应。
- 仅允许有限固定安全错误消息，其余消息省略；保存异常类别及末四个代码文件名、函数、行号。公共错误内容也不直接无条件str(exc)写日志。
- check模式调用原合约身份校验和Market.opening，窗口外原生拒绝照实记录；shape_check仅给出独立结构检查结果，不得解释为时效通过或模拟成交。
- timed模式调用原run_probe，必须提供原协议预先封存的未来整点计划，原15秒、时钟、重启/去重及失效规则不变。只在本诊断进程内包装Market.opening以保留各标的校验错误，不影响F3正式执行。
- 同一路径拒绝重复运行；先预留STARTED记录，崩溃残留不能自动重跑冒充第一次观测。输出应放私有目录并使用umask0077。
- run_probe外层吞掉的其他异常（例如部分时钟跳变分支）仍可能只有原异常类型；不得声称所有旧路径均已获得完整回溯。

## Windows专项命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_quote_probe_diagnostics.py tests/test_ubuntu_r14.py tests/test_forward_paper.py tests/test_paper_instruments.py
```

专项覆盖字段摘要/上限、敏感内容抑制、非法数值、目标柱缺失、confirm/成交量、500后重试、超时/连接故障三次上限、观察钩子恢复、窗口外不能算成功，以及SOL失败保留前两个有效结果。

## Ubuntu隔离运行

不覆盖已验收release，诊断文件以root:root/0644存放在：
`/opt/tradingagents/diagnostics/d31-20261025/quote_probe_diagnostics.py`。
执行解释器仍为已锁定release的.venv，PYTHONPATH仅指向该release，身份ta-research、独立HOME。该诊断是未提交的独立附加文件，不得冒称其本身已包含于基线Commit。

诊断文件SHA256：135a13fced716aadf60e36265373a61b0d59de96516aa67203481bcd4e6218fe。

真实检查档案：`/var/lib/tradingagents/r14/probes/d31-20261025/non-hour-check.json`。
UTC2026-10-25 00:38:01至00:38:03，7次请求均200、各一次HTTP尝试。三标的最新2根柱均九字段，当前小时柱confirm=0且成交量大于零。合约面值BTC0.01BTC、ETH0.1ETH、SOL1SOL；tick分别0.1/0.01/0.01，lot/min均0.01。SOL无持续字段/精度故障证据。
三次原生opening均因窗口外触发`Opening not observed on time`，定位forward_paper.py:254，为本检查的预期拒绝。shape状态VALID只证明数据形状，不保证整点初始毫秒已发布新柱。
时钟样本偏移+3.5ms、RTT37ms。正式F1/F2/F3档案目录仍不存在，交易样本0。

## 待用户明确授权的下一次整点试验

本轮没有安排新试验、没有创建timer。

1. 获准后选一个仍在未来且留足准备时间的UTC整点；用原arm_probe封存全新plan。若错过计划就拒绝，不回填。
2. 原计划提前80秒，使用一次性transient systemd任务、ta-research身份和D3原资源上限，运行本诊断timed入口；不安装长期unit，不改认证/SSH/原release。
3. 直接调用原run_probe，最多4次逻辑GET（三标的和时钟），每次原生GET仍最多三次HTTP尝试；140秒外层资源上限保持。
4. 保存新路径下原生封存attempt/result、诊断sidecar、文件指纹和journal启动时间。三个标的逐一核对请求发起/接收/校验完成，不能以进程退出0代表业务通过。
5. 任一标的失败则如实保存失败；仅观察，不产生F1/F2/F3样本；一次成功不代表长期可靠。

当前结论：诊断增强和非整点结构验证可有条件通过；历史根因未知，新三标的15秒重验未执行，不宣称D3第一阶段已通过。

execution_enabled=False / allow_new_risk=False / simulation_admitted=False。

## 本轮测试最终结果

独立专项：12 passed。
最终专项与兼容集合（quote_probe_diagnostics、ubuntu_r14、forward_paper、paper_instruments）：152 passed in 30.64s。
未安装新依赖、未调用模型、未执行全量A—D/股票测试；原已跟踪文件diff为空，冻结协议和策略未改动。
