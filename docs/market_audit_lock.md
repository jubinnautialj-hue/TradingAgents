# Market审计跨进程容量保护最小修复

状态：READY_FOR_COMMIT（本地实现及离线工程验收）。不代表原Vultr部署已解除BLOCKED。
基线597ebeae4c218ac303366e925d52ad847ed300d5，继续当前codex/f1-revalidation-admission-oi-diagnostics分支；未提交、未推送、未部署。

## 本地累计六个文件（含前轮未提交锁修复）

- tradingagents/agents/analysts/market_contract.py：仅将原线程锁写入段替换为append_audit调用；诊断Schema、容量常量、参数验证和模型行为均未改。
- tradingagents/dataflows/audit_file.py：新增标准库安全追加接口。
- tests/test_market_audit_file.py：19项文件/OS互斥测试，可直接在Linux运行，不依赖模型包。
- scripts/role_observer.py：将独立行政观察器纳入版本管理，仅替换共享文件写入段及回调失败传播设置。
- tests/test_role_observer_audit.py：混合写入、多进程容量、回调传播及元数据脱敏测试。
- docs/market_audit_lock.md：本文。

## 锁对象与接口

append_audit(path, row, limit, timeout=5.0)复用原审计路径、JSONL行和4MiB上限；不创建审计父目录，不自动重试写入。
不直接锁审计inode：日志可能被替换，两个进程可能因此锁住不同文件。
改为永久保留同目录path.lock，Linux使用fcntl.flock排他锁，Windows使用msvcrt字节锁。通过非阻塞尝试及单调时钟限制锁等待，默认最多5秒。
现有dataflows.files.locked使用普通打开方式，缺少O_NOFOLLOW、身份/权限绑定和超时；为避免改变其他调用方，本轮不修改通用锁，而在专用接口实现所需约束。

临界区覆盖：
1. 规范化父目录并核验其归属、不可被组/其他用户写入；所有别名使用同一实际父目录。
2. 打开锁文件但不截断，核验regular、单硬链接、当前UID、0600及命名路径与fd的dev/ino一致。
3. 取得跨进程锁后重查锁、父目录身份与权限。
4. 锁文件保存不含凭据的版本/锁dev-ino/审计dev-ino绑定。审计文件首次创建使用O_EXCL，0600；已绑定文件缺失不重建，替换inode不重新接受。
5. 空/丢失绑定的锁文件不得自动接管已有日志；复制旧锁内容到新inode也会被拒绝。损坏锁状态不自动重置。
6. 核验审计文件归属、0600、regular/单硬链接、路径/句柄身份、大小和剩余容量、上一条完整换行尾部。
7. 容量检查与一次os.write及fsync在同一排他临界区内；写前再验证身份/大小，写后核验大小和身份，完成后才返回成功。

锁文件不是可清理的临时锁，不能由运行时unlink或轮转。容量满后拒绝新增记录，不压缩、截断、删除或覆盖历史。Windows不能提供同等POSIX权限语义，相应权限检查在真实Linux补证。

## 失败语义

- 超容量：MARKET_DIAGNOSTIC_CAPACITY，原审计字节不变。
- 锁争用超时/锁系统错误：LOCK_TIMEOUT/LOCK_FAILED，未执行审计写入。
- 文件/父目录替换、错误权限、锁状态损坏、无绑定旧文件：明确失败，不自动修复或迁移。
- 进程持锁时被终止：内核释放锁；其他进程可继续，库不重放被终止调用。
- 短写：INCOMPLETE，绝不报告成功或重发整条记录；若留下不完整尾部，后续追加以INCOMPLETE_TAIL拒绝并保留现场。
- fsync错误：调用失败，即便完整字节可能已经写出也不声称持久化成功，不自动重放。
- 不承诺任意掉电/磁盘故障下记录全有或全无：未引入WAL或修复性截断。正常并发测试记录完整、无重复；人为注入短写会有保留的残缺尾部并安全停止。这是可审计失败边界，不伪称不存在物理短写。

## 对照测试

本地Ubuntu WSL2，Linux6.6.87.2，标准库Python3.12.3，以非特权nobody（UID/GID65534）在Linux原生临时目录运行实际接口。
没有Vultr连接或正式部署变更；这不是Vultr Python3.13.13全环境复验，未来固定提交仍需独立Linux验收。

19项Linux检查全部通过，0失败、0跳过：
- 同一旧检查/追加顺序的双进程屏障fixture复现超限：上限4194304，旧实现得到4194339字节（本地测试使用较短JSON行；不冒充此前334字节真实诊断行）。
- 修复后同样临界容量竞争只有一个成功、一个CAPACITY，文件4194303字节，不超过上限。此前Vultr4194622字节/超318字节的旧证据保留。
- 4线程80条，8进程320条，JSON完整、ID唯一，无丢失或重复。
- 锁超时有界、持锁进程异常终止后释放，无死锁。
- audit/sidecar替换、空sidecar替换、绑定审计缺失、坏锁状态、权限变化、父目录可写、符号链接/硬链接均拒绝。
- 满容量不修改历史，注入I/O、短写、fsync失败不报告成功，残缺尾部阻止后续追加。

Windows Python3.13回归：
- 最终受影响回归276通过、4跳过，覆盖原41项Market参数专项、合法输出字节兼容、图、预算、F1证明及观察资格和锁测试。
- 4项跳过仅是Windows不具备的POSIX权限/链接测试，均已在上述Linux19项中真实执行。
- 最终专项56通过、4跳过；补充跨线程/跨进程检查1通过。
- 两项既有F3.1失败明确重跑并原样保留：Reviewed DID NOT RAISE、与旧b01436c源码字节比较失败。未修改、跳过或隐藏它们，不宣称全量零失败。
- 初轮Windows测试暴露了POSIX旧竞态在Windows CRT追加行为不同，以及Windows拒绝重命名持有文件的OS差异；仅将测试按平台真实语义核对，Linux旧竞态复现没有删去。

原始日志在本地独立market_audit_lock_fix_20261103目录：
linux-lock-tests.txt、final-affected-regression.txt、final-specialized.txt、windows-thread-process-check.txt、known-baseline-failures.txt。

## 统一写入入口审查（2026-11-03接续）

实际旧观察器位于/opt/tradingagents/diagnostics/btc-prospective-trial-002/round-003/role_observer.py，root:root、0444。sitecustomize仅在TA_SINGLE_ROLE_OBSERVER=1且实际euid=999时安装回调；r3服务实际User/Group=ta-research（UID999/GID981）。日志为/var/log/tradingagents/btc-prospective-trial-002/round-003/model-metadata.jsonl，ta-research持有、0600，父目录0700。两种写入者使用同一个TA_SINGLE_AUDIT_FILE环境变量，因此未来共同启用时确实共享目标。

必须区分：旧trial-002研究代码8222a95尚无新Market参数诊断，不能据此宣称历史trial已发生这两种写入者的并发冲突。之前实机容量失败证据保持原样。

仓库生产入口全量搜索只有market_contract.diagnose及新增scripts/role_observer.write两处；两者均调用同一append_audit，无直接os.write。实际OS写入只由audit_file持锁执行。Vultr只读扫描发现6份旧行政role_observer副本（内容相同），以及隔离597的旧Market写入段，均属于保留历史部署，未修改。已检查服务引用，不能把旧脚本当成已经接入新协议的写入者。完整路径及哈希在独立deployment-inventory-before.json/after.json中。

版本化观察器保留install接口、角色映射、MODEL_START/END/ERROR字段、at_ms生成、归一化计量标记及摘要脱敏。7个原有非write函数AST与实际旧脚本完全一致。去除独立线程锁及直接os.open/write，使用append_audit；新增静态版本role-observer-audit-v1。唯一必要的失败语义调整为raise_error=True：原False会让LangChain吞掉审计写入异常；现由原生回调管理器向调用方传播，不自动重放、不伪装追加成功。正常事件构造及同一回调顺序不变；跨进程只保证临界区追加顺序，不宣称时间戳全局有序。

同一UID及现有私有目录已足够，不扩大组权限。不同UID不允许共同写0600文件；若未来运行身份不同，须停止并重新审批权限方案，不放宽当前接口。

## 本轮接续验证

- Linux本地WSL，真实Linux文件系统/tmp、非root nobody执行：19项原审计锁测试全通过、0跳过；新增6项共享写入OS测试全通过。框架测试类因该标准库解释器未安装LangChain，明确记录1项类级跳过，不将其称为Linux完整框架通过。
- 4进程混合追加240条MODEL_START/Market诊断/MODEL_END；逐调用顺序正确，JSON完整，唯一ID/事件组合无重复，0600。
- 真实4MiB临界测试，两进程分别走Market和观察器原始函数体，一个成功、一个CAPACITY，实际4193978字节，小于4194304；没有截断、覆盖或丢失历史。两条并非相同长度记录，因此不要求恰好填满上限。
- 两种入口均验证真实锁竞争超时、持锁进程强制退出后释放、文件替换拒绝、POSIX权限错误与符号链接拒绝；原19项补足侧锁替换、损坏、短写、fsync和目录权限场景。故障不自动重试，未写入不返回成功。
- Windows锁定Python3.13环境执行真实模块及LangChain CallbackManager，确认审计失败传播、原始正文不落盘、真实两模块共用侧锁；Linux OS测试仅为避免安装依赖而编译不改写的生产writer函数AST，未替换锁接口。
- 受影响回归283通过、5项POSIX跳过，8个参数化子检查通过（该轮收集后补充了真实双模块集成测试；随后专项复测覆盖）。5项跳过均在Linux独立OS测试中执行。
- 最终Market参数及观察器专项49通过、1项POSIX跳过，8个参数化子检查通过；包含追加的真实双模块集成测试。
- 两项既有F3.1基线失败原样重跑：Reviewed DID NOT RAISE、旧b01436cc源码字节比较断言失败；未修改、跳过或隐藏，不宣称项目全量零失败。
- 只读前后核验17个已登记远端脚本/源码/服务/总账哈希全部未变；trial-002 journal.json仍为8d402989ced9d5af2602c273383bc7c93db74dd5c8389242bd940e8e1e77af50。该核验只证明列明资产，不扩大声称检查了服务器所有文件。

本轮证据目录：本机.tradingagents/logs/unified_market_audit_20261103。保存Linux mixed/lock、Windows回归/专项、既有失败及前后部署清单；不提交运行日志或服务器脚本原始副本。

## 后续单独安装与Linux验收方案（本轮未执行）

1. 先验收并固化本地累计六文件，获得新固定Commit。随后另行授权部署到新的只读隔离代码目录，核验Git blob及实际Linux字节，不覆盖旧597或任何trial目录。
2. 需要安装验证的观察器文件为新Commit的scripts/role_observer.py；依赖同Commit的tradingagents/dataflows/audit_file.py及market_contract.py。可在新隔离行政目录安装已核哈希的观察器，或让新fixture入口从新代码/scripts加载。原sitecustomize接口可复用；必须验证role_observer.__file__、append_audit.__module__及全部实际导入路径，拒绝误载旧副本。
3. 使用ta-research、全新独立0700目录和不存在的审计文件/永久侧锁；不能直接接管旧日志，不能复制、删除或改写旧锁绑定。保持0600；不扩大文件或目录权限。
4. 在Vultr锁定Python3.13及86依赖环境运行完整新专项、回调集成和混合进程测试；用模型替身，不调用模型/业务HTTP，不创建正式总账或授权。
5. 保留旧脚本及服务原样。只有新隔离验收通过后才可申请未来运行的单独安装授权；不得用旧正式服务启动来验证。回滚仅停止新fixture并撤销新入口引用，保留证据；不清理或迁移历史审计文件。

本地状态READY_FOR_COMMIT。原Vultr整体验收仍BLOCKED/待新版本单独安装验证；本轮没有自动解除该状态，没有部署、启动或消费授权。

## 不变项及停止

market-tool-contract-v1事件结构、字段位图、结构指纹、调用ID关联及脱敏规则不变。
不改变Market参数验证、原计算结果、工具错误恢复、历史折叠、SDK、Agent数量、TTL、策略、Token预算或F1/F2/F3。
通用文件锁未改，预算实现指纹算法及其输入文件未改。
没有读写trial-001、trial-002账本或报告，没有新研究窗口、模型/业务HTTP、timer、F2/F3或交易。
AGENTS.md及原有未跟踪文件保持不变。execution_enabled=False、allow_new_risk=False、simulation_admitted=False。
完成后停止，等待验收，不自动Commit/Push。


## 714edbd后Linux容量Fixture兼容修正（仅测试）

本轮基线714edbd6221a40c97348e2e7d959d9716b0d8d35，继续原分支。生产market_contract.py、audit_file.py、role_observer.py及所有预算/策略/TTL/F1—F3逻辑完全不改。

原test_metadata_sink_capacity_stops_without_unbounded_output的目的为容量拒绝。旧fixture通过write_text创建未绑定文件，Linux默认0644导致权限检查先拒绝，与容量保护无冲突。

最小修正仅在tests/test_market_tool_contract.py：使用原生append_audit创建合法JSONL、0600审计文件和永久身份绑定侧锁；填充至真实4194304上限前1字节，再经Market diagnose验证CAPACITY。删除把AUDIT_LIMIT改成1的测试替换；拒绝后核验内容、4194303字节、audit dev/ino及侧锁绑定字节均不变。

新增独立POSIX用例创建明确0644的无效fixture，验证FILE_PERMISSIONS及原字节/权限保留。仅在创建该隔离测试文件时临时设置umask022并立即恢复；没有chmod，没有修复或接管正式/旧审计文件。Windows缺少同等权限语义，明确平台跳过该新增用例；原既有失败用例未改、未跳过。

本地WSL Linux、非root UID65534验证：
- 两个修正/新增测试函数体原样执行，0600/4194303字节返回CAPACITY，0644/23字节返回FILE_PERMISSIONS，全部通过。该标准库解释器没有LangChain/pytest，外部验证驱动仅提供pytest/import支架并编译实际diagnose/test函数体；原生append_audit及OS检查真实执行，不冒充完整Linux框架回归。
- 19项审计锁测试全部通过、0跳过。双进程临界容量结果一个APPENDED、一个CAPACITY，文件4194303；旧竞态fixture4194339仍可复现，未隐藏。
- 6项混合写入OS检查全部通过；未安装框架导致1项类级跳过，框架集成另由Windows完整依赖回归覆盖。

本轮不连接或部署Vultr。新固定提交的完整Vultr Python3.13框架验收仍须后续独立授权，不将本地POSIX检查等同于已解除服务器验收阻塞。

本地证据目录audit_fixture_fix_20261103保存regression.txt、linux-fixture.txt、linux-locks.txt及linux-mixed.txt；不纳入Git提交。未提交/推送、未修改旧资产、未调用模型或业务HTTP。三个安全字段均False。

完整Windows Python3.13受影响回归：385通过、2项既有F3.1失败、6项POSIX平台跳过，另8个参数化子检查通过，无新增失败。包含Market参数、锁与观察器、展示/身份、Graph、CLI、预算、F1/F2.1及完整F3.1测试。两项失败仍为Reviewed DID NOT RAISE及旧b01436cc源码字节比较，测试未修改或跳过。

本地状态：READY_FOR_COMMIT（仅测试fixture修正）；不代表已完成新提交的Vultr框架复验。最终修改范围仅本测试文件与本文，未暂存、Commit或Push。
