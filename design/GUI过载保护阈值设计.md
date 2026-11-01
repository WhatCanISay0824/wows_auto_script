# GUI 过载保护阈值设计（已确认，2026-10-31）

状态：用户已确认；实现中。本文是 [GUI 交互与预览设计](GUI交互与预览设计.md) §6.2 待定项的裁决，
与 [GUI 界面设计文档](GUI界面设计文档.md) 的运行服务/安全契约共同生效。

## 1. 目标与边界

过载保护只回答“什么时候让脚本主动交出控制权”，**不杀进程**：

- 不调用 `taskkill` / `kill` / `os.kill`，不结束游戏或其它进程；
- 最重动作是**优雅停止**：停止脚本控制、释放键鼠、写本地记录，游戏继续运行；
- 停止后不自动重启；整机严重卡死到采样线程得不到调度时，不承诺停止时延（沿用原文档口径）；
- 保护对象：① Commit 提交内存耗尽（分配失败/游戏崩溃风险）；② 清醒状态下的多秒级调度饥饿
  （脚本无法按时序安全操作）；③ 脚本自身资源异常的告警（v1 不自动停止脚本内存）。
- 不纳入 v1 停止判据：GPU/温度/显存/磁盘 I/O/CPU 占用/物理可用内存/心跳年龄/截图耗时/帧龄。
  这些只展示或告警；磁盘空间已有 `logging.disk_warn_gb/disk_pause_gb`，但尚无调用方，另行接线。

## 2. 监测对象与口径

采样 1 Hz，环形缓冲 300 样本（5 分钟）；窗口 30/60/120 秒；有效样本覆盖率 ≥90% 才判定。
采样在安全看门狗线程内进行（不新增线程、不经 GUI 主线程、不伪造心跳）。

| 指标 | API（无新依赖） | 角色 |
| --- | --- | --- |
| 整机 CPU % | `GetSystemTimes`（kernel 含 idle，差分；首拍丢弃） | 仅告警 |
| Commit 余量/使用率/上限 | `GetPerformanceInfo`（psapi，页数×PageSize）为主，`GlobalMemoryStatusEx` 交叉校验 | **暂停/停止主判据** |
| 物理内存可用 | `GlobalMemoryStatusEx`（含 standby 缓存） | 告警/展示 |
| 脚本进程私有内存 | `GetProcessMemoryInfo`（`PROCESS_MEMORY_COUNTERS_EX`，`cb=80`） | 告警（v1 不停止） |
| 脚本进程 CPU | `GetProcessTimes` | 告警 |
| 采样迟到/睡眠 | `monotonic` + `GetTickCount64` + `QueryUnbiasedInterruptTime` | **暂停/停止佐证** |
| 截图耗时/主循环 tick/帧龄 | 既有 `PerceptionStats` / 运行循环统计 | 展示+告警 |

启动自检：64 位解释器、`CommitLimit ≥ 物理内存`、GPI 与 GMS 的 Commit 口径差 ≤64MB；任一不满足
→ 内存类规则禁用，只展示并标“监测不可用”。

## 3. 判定级别与阈值（比例 + 下限；括号为本机 24 核/31.4GB/CommitLimit≈48GB 折算值）

| 级别 | 规则 | 阈值 | 持续时间/覆盖率 | 动作 |
| --- | --- | --- | --- | --- |
| W 告警 | CPU 忙 | ≥90% | 60s | 记录/提示 |
| | 物理可用低 | < max(2500MB, 8% 总内存) | 60s | 记录/提示 |
| | Commit 偏紧 | < max(6000MB, 12% 上限) | 60s | 记录/提示 |
| | 脚本私有内存高 | > max(1024MB, 2×预热基线) | 60s | 记录/提示 |
| | 偶发调度迟到 | >2.5s 的事件 1–2 次 | 120s 内 | 记录/提示 |
| | 脚本操作变慢 | capture_ms_avg >600ms 或主循环 tick p95 >500ms | 60s | 记录/提示 |
| T1 保护性暂停（可恢复） | Commit 余量紧张 | < max(2000MB, 5% 上限) | 最近 60s ≥80% | 请求暂停 → 关门禁+释放；可自动恢复 |
| | 清醒调度饥饿 | 迟到 >2.5s 事件 ≥3 次 | 120s 内 | 同上 |
| T2 优雅停止（终态） | 快速通道 Commit | < max(400MB, 1% 上限) | 连续 10s | 直接请求停止 |
| | 暂停后 Commit 仍恶化 | < max(1200MB, 2.5% 上限) | 暂停后最近 30s ≥90% | 停止 |
| | 暂停后调度仍恶化 | 清醒迟到事件 ≥3 次 | 暂停后 180s 内 | 停止 |

- **T1/T2 只在“脚本持有输入控制权且生命周期 RUNNING/PAUSED(overload)”时动作**；
  LOADING/START_COOP/结算/退出菜单/换号等过渡态宽限 60s、不暂停；启动预热 120s；
  睡眠/唤醒/时钟跳变后清窗 + 90～120s 宽限。
- 睡眠识别：`asleep = ΔGetTickCount64 − ΔQueryUnbiasedInterruptTime`；`asleep` 覆盖迟到
  （≥max(1s, 0.5×迟到)）视为系统睡眠/挂起，清窗且不计迟到；清醒下的 >2.5s 才记事件。
  wall 与 monotonic 差跳变 >2s 视为系统时间调整，清窗。
- 告警滞回：低于阈值 ×0.8 且持续 60s 才清除；同类告警 5 分钟最多一条。
- 有效样本 <90% 的窗口不判定；不把缺失值当健康、当 0、当过载。
- 检测到调试器（`IsDebuggerPresent`）时降级为仅告警。

## 4. 恢复与升级规则

- 自动恢复（仅 `overload_pause` 原因）：暂停 ≥60s；Commit 余量 >1.5×暂停阈值且 60s ≥90%；
  120s 内无迟到事件；游戏前台校验通过（复用现有恢复路径，不在监控线程抢焦点）。
- 每会话最多自动恢复 2 次；超过后保持暂停并提示人工处理（不自动升级停止）；10 分钟内反复暂停
  → 取消自动恢复。
- 手动 F11/GUI 恢复始终可用。

## 5. 触发动作与记录

- 暂停：`StopController.request_pause("overload_pause", detail, hook="overload")` →
  监控线程内先关闭输入门禁并请求释放（与 F12/心跳同一独立路径）→ 主循环转 PAUSED。
- 停止：先关门禁/释放，再 `request_stop("overload_commit"/"overload_starvation", detail, hook="overload")`
  → 主循环 STOPPING → 统一清理；`reason` 为终态，`_run_accounts` 的白名单必须终止轮换而不是切下一账号。
- GUI：先显示“停止中（电脑过载）”；输入释放确认后才显示“已停止 · 电脑过载”。
  monitor 模式显示“保护阈值：监测中（不自动停止）”，并记录“本应暂停/停止”的 shadow 证据；
  阈值确认并启用后才显示“保护已启用”。
- 记录字段：规则名、阈值、实际值、持续时长、触发时间、释放结果、窗口 min/p50/p95/max 摘要、
  配置哈希；可选把环形缓冲摘要写入 `logs/` 诊断文件（不得长期保留截图）。
- 不杀游戏/其它进程；不自动重启；旋转（`--rotate`）遇到 `overload_*` 终止整个轮换。

## 6. 上线顺序（防误停）

1. `mode: monitor`：只采样、展示、记录 shadow 结论，不动作；跑 2–3 场含加载/战斗/死亡/结算的实机。
2. 标定：正常场次 Commit 最低余量 > T1 阈值 2 倍；清醒迟到事件为 0；脚本私有内存峰值
   < 告警线 50%；否则先调阈值。
3. 启用 T1 暂停，观察是否误暂停（可自动恢复）。
4. 启用 T2 停止；做“持续过载”（另进程逐占 Commit）与“24 线程满载 5 分钟”对照测试，
   并验证停止后的键鼠释放。
5. 验收对应 GUI 交互设计 §10：持续过载、瞬时峰值、GUI 阻塞、运行循环阻塞、监测失败；
   模拟测试不替代实机释放检查。

## 7. 配置

config.yaml 新增 `overload:` 段（默认 `mode: monitor`；未知键/越界值在 validate 中报 error）。
实现采用“**余量下限 floor + 比例 ratio**”的命名（实际生效值 = `max(floor, ratio × 本机上限)`），
完整键与中文注释以 `config.yaml` 为准，核心键如下：

```yaml
overload:
  mode: monitor                 # off | monitor | pause_stop | stop
  sample_hz: 1
  window_warn_s: 60
  window_long_s: 120
  window_stop_after_pause_s: 180
  window_resume_s: 60
  min_coverage: 0.9
  # W 告警（只记录）
  cpu_warn_pct: 90
  physical_warn_floor_mb: 2500
  physical_warn_limit_ratio: 0.08
  commit_warn_floor_mb: 6000
  commit_warn_limit_ratio: 0.12
  script_private_warn_floor_mb: 1024
  script_private_baseline_ratio: 2.0
  # T1 暂停（可恢复）
  commit_pause_floor_mb: 2000
  commit_pause_limit_ratio: 0.05
  commit_pause_occupancy: 0.8
  # T2 停止（终态）
  commit_stop_floor_mb: 1200
  commit_stop_limit_ratio: 0.025
  commit_stop_window_s: 30
  commit_stop_occupancy: 0.9
  commit_fast_stop_floor_mb: 400
  commit_fast_stop_limit_ratio: 0.01
  commit_fast_stop_sustained_s: 10
  # 清醒采样迟到
  starvation_lateness_s: 2.5
  starvation_pause_events: 3
  starvation_stop_events: 3
  # 睡眠/时间跳变
  sleep_detect_s: 1.0
  sleep_cover_ratio: 0.5
  sleep_grace_s: 120
  clock_jump_s: 2.0
  # 自动恢复
  pause_min_s: 60.0
  resume_commit_ratio: 1.5
  resume_quiet_s: 120
  auto_resume_max: 2
```

## 8. 测试与验收

- 纯策略单测（假样本序列）：CPU 满载/单峰值不动作；Commit 慢降触发 T1/T2；瞬时低于阈值不触发；
  睡眠/挂起、时钟跳变、缺样本、覆盖率不足不触发；monitor 模式不动手；恢复/滞回/自动恢复上限；
  调试器/自检失败降级。
- 监控器集成：注入 FakeClock/假采样/假 StopController，验证事件、PerformanceView、暂停/停止请求。
- 实机：见 §6；停止后检查键鼠已释放、游戏未退出、无进程被杀。

## 9. 实现落点

`src/common/sysmetrics.py`（ctypes 指标）、`src/execution/overload_policy.py`（纯状态机）、
`src/execution/overload.py`（监控器）、`src/execution/safety.py`（OVERLOAD_* 事件与 1 Hz 轮询）、
`src/config/*` + `config.yaml`（overload 段）、`src/runtime/{messages,runner,service}.py`
（PerformanceView/快照/接线）、`src/gui/*`（性能面板/设置）。

## 10. 已知盲区

磁盘 I/O/驱动/DirectX 卡顿、温度降频、GPU 显存问题不纳入 v1 停止判据；整机彻底冻死时无动作保证；
这些场景只告警或依赖既有 F12/心跳/帧龄护栏。
