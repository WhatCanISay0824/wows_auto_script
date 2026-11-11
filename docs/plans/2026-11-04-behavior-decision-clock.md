# 防挂机行为决策时间修复方案

## 背景与结论

本次按 `CLAUDE.md`、行为设计文档及「问题修复#1」「问题修复#2」的相关记录核查。用户已要求由 Codex 制定方案并直接执行。本轮由 Codex 在现有工作区完成，不向外部模型发送日志、截图或源码。

`logs/session_20261104_163239.log` 对应会话 39、第 60 场，行为已经启动，但 SQLite `battle_actions` 中没有 `idle_camera_move`、`combat_aim` 或 `combat_fire`。273 条模式记录包括 133 次 `future_frame`、53 次正常无目标状态、20 次己方位置缺失及 67 次镜头方向缺失（其中 4 次尚在候选确认）。前几次负帧龄为约 -0.056～-0.065 秒。这证明调度启动，并不证明动作执行。

`SessionRunner.step()` 在截屏前取得 `now`；`WindowCapture` 在完成截屏后给帧写入实际单调时间。感知完成后仍用截屏前的 `now` 调用状态机，新帧便被调度门禁判为未来帧，清空无目标等待和交战控制状态。每次更新截图都会重置无目标等待，因而等不到 3～4.5 秒的动作期限。原离线回放的截屏没有耗时，且直接在感知完成后读取时钟，未覆盖生产调用顺序。

## 方案及取舍

采用在运行器完成感知后重新读取时钟的局部修复。普通观察、GUI 返回强制截屏及短暂失焦返回均使用处理完成后的时间；状态机实际决策前再读取一次，以计入小地图编码等处理耗时。

不采用允许负帧龄的容差，也不把决策时间强制提高到帧时间：这两种方式会掩盖真正的错误时间戳。不改写截图时间，不放宽 1 秒过期门槛，不重置截图年龄，不改变无目标节奏和交战策略。

镜头缺失是日志确认的另一项限制。当前只保留死亡截图，没有本场存活交战时的截图，不能据此证明存活时应识别出镜头方向。死亡图中的标记和扇区不作为存活标定依据；本轮保留证据不足时等待的规则，明确该项实机证据缺口。

## 文件职责与执行步骤

- `src/runtime/runner.py`：只修正感知完成与实际决策之间的时间读取。
- `tests/test_runtime_behavior_timing.py`：生产运行器、状态机、感知和行为调度联合回归，截屏使用保存的小地图并推进替身时钟，输入使用替身。
- 本文：保存原因、范围和实际验收结果。
- `CLAUDE.md`：补充本轮修复与验证边界。

1. 新增耗时截屏的生产运行器回归，先验证旧代码无法产生无目标动作。使用当前真实小地图样本，每次截屏推进 0.06 秒，运行超过配置的最长等待时间；断言出现 `idle_camera` 提交且没有 `future_frame`。
2. 覆盖 GUI 强制重截屏、失焦恢复，以及感知处理超过帧龄期限、真正未来帧被拒绝。将合成敌舰加入同一真实小地图，验证 idle → candidate → combat → idle，候选必须停止晃动，两帧确认后才交战。
3. 在 `observe()` 返回之后与 `machine.step()` 调用之前使用 `clock.now()`；失焦分支使用 `self.runtime.clock.now()`。不修改感知帧和门禁。
4. 运行相关回归、完整测试、Ruff、四套配置校验和差异检查。检查所有本轮变更，保留既有修改，不删除、暂存、提交或推送。

验收命令：

```powershell
py -3.14 -m pytest tests/test_runtime_behavior_timing.py tests/test_gui_cooperation.py tests/test_combat_replay.py tests/test_behavior_resume_switch.py tests/test_idle_camera.py -q
py -3.14 -m pytest tests -q
py -3.14 -m ruff check src tests
py -3.14 -m src.tools.validate --profile cn360 --ship iron_duke --json
py -3.14 -m src.tools.validate --profile wg_asia --ship iron_duke --json
py -3.14 -m src.tools.validate --profile wg_eu --ship iron_duke --json
py -3.14 -m src.tools.validate --profile wg_na --ship iron_duke --json
git diff --check
```

## 执行状态

- [x] 失败复现与限定修复。
- [x] 独立审核、真实小地图与耗时时序联合回放。
- [x] 完整检查与结果记录。

## 验收结果

1. 最终新增的 7 项回归在进程内换回 Git HEAD 中原始 `step()` 和 `_focus_watch()` 方法，得到 6 项失败、1 项通过；工作区源码未被回退。失败覆盖无目标动作饥饿、感知和小地图耗时未计入帧龄、两种返回路径以及完整行为往返。真正未来帧拒绝测试在原实现中也通过。输出为 `pytest_work/idle_activation_1104/red-final.txt`。
2. 修复后相关 66 项通过，耗时 28.75 秒，见 `pytest_work/idle_activation_1104/related.txt`。回归经过真实 SessionRunner、StateMachine、PerceptionEngine 和 BehaviorScheduler；截屏用真实小地图样本，界面依赖与输入用替身。合成敌舰首先进入 candidate 且不移动、不转向、不开火；两帧确认后开火，移除目标后恢复 idle。
3. 12.1 秒替身时钟回放含 20 次截屏，每次推进 0.06 秒。首次无目标移动在 3.46 秒，开始 3 轮动作，向替身执行器提交 11 个无目标移动步骤；只记录一次正常 idle 模式，没有未来帧反复阻断。摘要为 `pytest_work/idle_activation_1104/replay-summary.json`。这只证明动作生成和提交，不证明真实 SendInput 投递或游戏视角变化。
4. 全套 1344 项通过，1 项因缺少本地实机血条样本跳过，耗时 76.41 秒，见 `pytest_work/idle_activation_1104/full.txt`。Ruff 通过，见同目录 `ruff.txt`。四套客户端配置均零错误，国服 5 条提示、直营服各 6 条提示，见 `validation.json`；包含选船覆盖、空状态特征、未配置装备检查、autopilot 不需要地图包及直营服待标定等已有提示。
5. `git diff --check` 通过。本轮运行代码只修改 `src/runtime/runner.py` 的时间读取，新增一份回归文件和本方案，并在 `CLAUDE.md` 补充记录。已有行为控制器、配置、客户端标定和其他未提交修改均保留；没有暂存、提交、推送或修改运行数据库。日志核查使用 SQLite 只读连接，摘要见 `pytest_work/idle_activation_1104/log-audit.json`。

所有离线检查使用替身输入，不操作真实游戏。游戏是否认可这些动作，以及本场镜头方向缺失的具体视觉原因，仍需存活画面和实机复测确认。
