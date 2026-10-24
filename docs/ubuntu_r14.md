# R1.4 Ubuntu 部署准备与操作手册

基线 `10472fb2a59199261722f2727db9e1b126f46de4`；本轮仅开发部署准备。
**未登录 Vultr、未安装/启用定时任务、未证明长期稳定运行。** Windows 的 `.venv` 不能复制到 Linux。
`execution_enabled=false / allow_new_risk=false / simulation_admitted=false` 始终保留。

## 架构与兼容性

```text
systemd research ── R14 检查/独立监督锁 ── R12 research ── R11 串行 BTC/ETH/SOL ── F1
systemd observe  ── R14 检查/独立监督锁 ── R12 observe  ── 原 F2/F2.1
systemd paper    ── R14 检查/独立监督锁 ── R13 paper    ── 原 F3 + 标的准入扩展
systemd timely   ── R14 时钟检查/监督锁 ── R13 timely   ── 提前80秒预热/15秒观测约束
                    └─ R14 只读综合报告及运行审计
```

没有新 Agent、交易策略或撮合引擎。研究四小时一次，F2 每分钟 :02 扫描，常规纸面评价每五分钟 :20，
及时任务每小时 :58:40 启动。研究默认串行，原 R11 去重、每任务/整批预算和额度限制暂停保持。
F3 最大持仓仍是原协议的12根小时柱，不因研究定位1—3天而修改。

R14 独立监督进程还具有阶段锁、整组子进程终止、硬超时、2MiB 子进程输出上限及运行封存记录。
模型任务不会占用观察监督锁；原 R12/R13 的 paper/timely 共享状态锁保持。系统调度仍可能迟到。
原 BTC 使用 forward-paper-v1；ETH/SOL 使用 forward-paper-instruments-v1。
不复制、不迁移 Windows 的 F1/F2/F3、旧观察链、测试样本或历史开发集到 Ubuntu 的正式样本池。

## 环境、依赖与权限

目标：非 root 用户 `ta-research`，Linux x86_64，独立 CPython3.13 虚拟环境。
`ubuntu_r14.requirements.lock` 由现有 uv0.11.14 以当前86项已安装版本为约束，针对Linux/Python3.13，
仅使用公开PyPI、仅二进制包并生成分发哈希。解析通过不等于Linux安装和应用导入通过。
`ubuntu_r14_dependency_lock.json` 保存锁指纹与验证边界。没有升级当前Windows依赖。

默认部署参数见 `ubuntu_r14.json`；Schema 见 `ubuntu_r14.schema.json`，另有确定性路径/权限检查。
代码、配置和数据目录必须隔离：

- `/opt/tradingagents/releases/<已审批R14提交>`：root控制、服务用户只读，必须保留 `.git`。
- `/etc/tradingagents-r14/releases/<版本>`：root控制的封存部署包；模型私有环境文件不进Git。
- `/var/lib/tradingagents/r14`：服务用户所有、0700；包含独立HOME、缓存、批量研究、观察、账本、审计和日志。

服务仅通过进程环境为**这个已审批代码路径**设置 Git safe.directory，不修改全局Git或Codex配置。
以root安装代码后不要对整个release执行 `chown -R ta-research`。服务不得修改代码或协议。
HOME位于数据目录，项目ChatGPT认证与现有root/Codex/宝塔认证隔离；不自动复制任何Token或Key。
不需要OKX/Binance账户Key。服务设置UTC、0077、只读系统、无新增权限和有限内存/线程数。
内存限制为研究2GiB、其他通道各1GiB；它们是上限，需根据服务器实际资源验收，不保证同时满载可运行。

## 先在 Windows 生成预览

```powershell
cd D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.ubuntu_deployment prepare --output "$env:USERPROFILE\.tradingagents\logs\r14_bundle_review"
.\.venv\Scripts\python.exe -m tradingagents.ubuntu_deployment validate --bundle "$env:USERPROFILE\.tradingagents\logs\r14_bundle_review" --source-root D:\_01trading_agents
.\.venv\Scripts\python.exe -m tradingagents.ubuntu_operations dry-run --bundle "$env:USERPROFILE\.tradingagents\logs\r14_bundle_review"
.\.venv\Scripts\python.exe -m tradingagents.ubuntu_operations report --automation docs/automation_r12.json --instruments docs/paper_instruments_r13.json --output "$env:USERPROFILE\.tradingagents\logs\r14_monitor_review"
```

输出目录须不存在。预览不安装、联网、调用模型或启动研究。
Windows/LF/CRLF 会影响原生源文件字节指纹：**在最终Linux代码树和虚拟环境中重新prepare并审查**，
不得编辑manifest消除不匹配，也不得把本机预览直接标为已部署。正式版本尚未提交推送时不能从Fork拉到R14。

## 获得服务器访问及部署授权之后的步骤（本轮没有执行）

以下是操作说明，不能理解为已经取得部署授权。先只读检查 Ubuntu版本、`id`、`df -h`、`free -m`、
`python3.13 --version`、`uv --version`、`timedatectl show -p NTPSynchronized --value` 和网络。
使用服务器已有的合法管理方式，不更改其他服务、全局MCP、系统Python或代理。
没有Python3.13/uv0.11.14时先停止，由管理员审批独立安装；不将WSL的Python3.12测试当作应用运行验收。

以下变量应填写**经过确认的真实值**，不得使用未提交的R14工作区冒充已发布版本：

```bash
RELEASE=/opt/tradingagents/releases/已审批R14提交
BUNDLE=/etc/tradingagents-r14/releases/已审批部署版本
DATA=/var/lib/tradingagents/r14
```

1. 由管理员创建独立用户（已存在时先核对用途，不接管）：

```bash
sudo useradd --system --user-group --home-dir "$DATA/home" --shell /usr/sbin/nologin ta-research
sudo install -d -m 700 -o ta-research -g ta-research "$DATA" "$DATA/home" "$DATA/cache" "$DATA/logs" "$DATA/probes"
```

2. 在新的release目录克隆Fork、checkout已审批R14提交，不覆盖旧release，不复制本地认证或运行档案。
管理员准备可读的独立Python3.13。若使用uv管理Python，安装位置应在 `/opt/tradingagents/python`，
不能让服务解释器依赖 `/root/.local` 或 `/root/.cache`（ProtectHome会阻止访问）。

```bash
sudo env UV_PYTHON_INSTALL_DIR=/opt/tradingagents/python uv venv --python 3.13 "$RELEASE/.venv"
sudo uv pip sync --python "$RELEASE/.venv/bin/python" --link-mode copy --require-hashes "$RELEASE/docs/ubuntu_r14.requirements.lock"
sudo uv pip check --python "$RELEASE/.venv/bin/python"
```

这里不需要额外安装pip或setuptools；应用从release工作目录通过 `python -m` 启动。
`--link-mode copy` 避免运行时包指向root私有缓存。记录Python完整补丁版本、uv版本、OS、锁指纹。
在目标环境执行导入、专项与回归验证；开发测试依赖应使用另一个验证环境，不静默修改基准运行锁。

3. 将配置副本中的release_dir/config_dir改为上述已审批目录，data_dir保持稳定。以目标虚拟环境prepare到
一个新临时目录，validate通过后由管理员复制到新的BUNDLE，root所有、目录0755/文件0644。
必须保留manifest、原协议、来源指纹、锁和独立准入限制。`model.env.example` 只能人工复制成
`model.env`，root所有、0600；模板仅有非秘密模型名。不要复制Windows的localhost代理地址。

4. 手动操作服务身份时，显式使用相同HOME、模型名与进程范围Git设置（不写全局配置）：

```bash
ta() {
  sudo -u ta-research env HOME="$DATA/home" TZ=UTC PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 \
    XDG_CACHE_HOME="$DATA/cache" GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$RELEASE" \
    TRADINGAGENTS_LLM_PROVIDER=chatgpt TRADINGAGENTS_QUICK_THINK_LLM=gpt-6.1-sol TRADINGAGENTS_DEEP_THINK_LLM=gpt-6.1-sol "$@"
}
ta "$RELEASE/.venv/bin/python" -m cli.main login chatgpt
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_deployment check --bundle "$BUNDLE"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations dry-run --bundle "$BUNDLE"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations model-probe --bundle "$BUNDLE" --output "$DATA/model_probe_一次性新目录"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations public-probe --bundle "$BUNDLE"
```

登录由用户在项目自己的CLI完成，不向聊天、Git或日志粘贴登录材料。登录命令可能需要人工交互；
服务用户的nologin shell不妨碍上面直接执行Python。认证文件只检查存在和权限，不在健康检查中读取内容。
模型探针复用 `python -m scripts.probe_okx_llm`，一条短请求、无工具、无SDK重试；同输出目录不能重试。
模型配置若不同，手动环境必须与实际service一致。Token用量不可得时记录未知，不能当成0成本。

5. 审查资源、路径、时钟和脱敏结果后，手动执行一次 observe/paper/timely。
研究调用昂贵，单独批准后才手动执行 `run research`。命令统一为：

```bash
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations run observe --bundle "$BUNDLE"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations run paper --bundle "$BUNDLE"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations run timely --bundle "$BUNDLE"
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations report --bundle "$BUNDLE" --output "$DATA/reports"
```

## 独立15秒时效探针

没有正式候选时，先arm未来整点，再提前80秒通过操作系统启动单次run-probe。
探针完全独立于F1/F2/F3，输出 `paper_trades=0`，不产生交易样本。严禁补采冒充准时观测。

```bash
TARGET_MS=$(( ($(date -u +%s) / 3600 + 1) * 3600000 ))
ta "$RELEASE/.venv/bin/python" -m tradingagents.ubuntu_operations arm-probe --bundle "$BUNDLE" --plan "$DATA/probes/plan.json" --target-ms "$TARGET_MS"
```

由管理员在**另行授权的短期测试**中安装/启动可选 `ta-r14-probe.service/timer`（不属于默认4条运营任务）。
计划文件不能覆盖；每次计划有固定未来时间、指纹、attempt和result封存。早于预热时间不发请求；
进程中断后不重用部分结果；超过15秒或时钟跳变时不认定有效报价。三标的逐一观测，最多4次逻辑GET。
原生公开HTTP仍有最多3次尝试；超时由单次服务上限控制。
记录预计启动、实际启动、启动延迟、服务器时钟、请求/接收时间、K线整点及采集方式。
运行任务的cadence参考延迟是日历网格参考，**不是经验证的systemd触发时间戳**；受控探针使用事先固定的计划时间。
最终必须用Vultr实际证据评估15秒窗口，不能把Windows延迟样本或WSL语法测试当作生产SLA。

## 安装与启用任务（须用户再次确认，本轮只给出命令）

先预建日志，更新时不清空旧日志：

```bash
for phase in research observe paper timely probe; do
  test -e "$DATA/logs/$phase.log" || sudo install -o ta-research -g ta-research -m 600 /dev/null "$DATA/logs/$phase.log"
done
sudo systemd-analyze verify "$BUNDLE"/ta-r14-{research,observe,paper,timely}.service "$BUNDLE"/ta-r14-{research,observe,paper,timely}.timer
# 只有在用户确认安装后才执行以下操作；如同名unit已存在，先停止并审查，禁止覆盖。
sudo install -m 644 "$BUNDLE"/ta-r14-{research,observe,paper,timely}.service /etc/systemd/system/
sudo install -m 644 "$BUNDLE"/ta-r14-{research,observe,paper,timely}.timer /etc/systemd/system/
sudo install -m 644 "$BUNDLE/tradingagents-r14.logrotate" /etc/logrotate.d/tradingagents-r14
sudo systemctl daemon-reload
# 用户确认无人值守启用后：
sudo systemctl enable --now ta-r14-{research,observe,paper,timely}.timer
```

timer `Persistent=false` 防止重启补发迟到的整点任务；开启后会随系统启动，在后续日历时刻恢复。
原F2增量补采单独标记；原F3错过的开盘不能补签。systemd活动oneshot不排队启动重复实例，原生锁继续防重。
不默认安装可选probe任务，不修改现有Codex、宝塔或其他服务。

## 状态、暂停、停止、更新与回滚

```bash
systemctl list-timers 'ta-r14-*'
systemctl status ta-r14-{research,observe,paper,timely}.service
ta "$RELEASE/.venv/bin/python" -m tradingagents.automation pause --phase research --config "$BUNDLE/automation.json"
ta "$RELEASE/.venv/bin/python" -m tradingagents.automation resume --phase research --config "$BUNDLE/automation.json"
# 完全停止（经授权操作），保留所有档案：
sudo systemctl disable --now ta-r14-{research,observe,paper,timely}.timer
sudo systemctl stop ta-r14-{research,observe,paper,timely}.service
```

quota/权限错误由原R12暂停研究，不暂停原有效候选观察；服务不自动Restart，无无限模型重试。
R14对F1索引达到80%容量时锁存暂停新增研究：默认800/1000条，约18条/日时约44天就需人工规划。
这是有界系统，不承诺无限期无人维护。账本32MiB附近也报警；不自动删除、压缩原始审计或扩大协议限制。
日志仅轮转独立的supervisor日志（daily/10MiB/7份）；copytruncate可能丢失少量运行日志，正式审计不依赖这些日志。
研究档案、行情证据、账本和operations审计不轮转删除，磁盘低于2GiB时阻断推进，需人工备份规划。

更新使用新的已审批release和配置目录，停本项目任务，保留并备份完整当前数据，目标环境重新prepare/validate，
审查协议和代码指纹后替换本项目unit。不要在看到收益后改变成本或配置接管旧账本。
回滚只使用先前已验收且协议兼容的**Ubuntu release**，保留最新数据；不能用旧备份覆盖已经产生的新前向档案。
若旧代码无法验证当前账本，保持只读并停止，不修改指纹绕过。不曾有可用旧release时，首次部署的回滚就是停止并保留数据。

综合报告保留各标的独立账本统计和源报告；不同标的各有独立模拟权益，不伪造共享资金组合回撤。
没有真实成交时胜率、PF、收益保持未定义。WAIT、研究失败、无效预案和数据故障分别展示。
未完成的操作状态是 RUNNING_OR_INTERRUPTED_UNKNOWN，不伪装成功；近期操作汇总最多200条，完整原始审计保留。

## 四层验收边界

1. Windows确定性及原有回归：见 `ubuntu_r14_acceptance.json`。
2. 本机Ubuntu24.04 WSL、非root、Python3.12：原生POSIX锁/终止恢复、systemd255语法夹具和日历、logrotate调试。
   **夹具中的Python链接不是生产venv；没有验证Python3.13应用安装。**
3. 本机官方公开行情与一次低成本模型探针：记录独立结果，不代表服务器网络/认证可用。
4. Vultr实际部署、短期真实调度时效、未来正式成交及长期稳定性：本轮未授权、未执行、未通过生产验收。

官方说明：[systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)、
[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。
timer精度参数和资源优先级不是15秒时效保证，需要真实观测证据。
