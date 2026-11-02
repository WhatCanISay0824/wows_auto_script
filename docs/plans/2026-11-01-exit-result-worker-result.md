# 第三场死亡退出结算续战修复结果

## 原实现失败证据

- 新增 `test_exiting_result_takes_priority_over_exit_timeout` 后，在原实现上运行失败：
  `assert State.ERROR_RECOVERY is State.BATTLE_FINISHED`，复现 EXITING 超过 `exit_s` 时
  同一帧已有结算页却进入异常恢复的问题。
- 失败用例运行受沙箱临时目录 ACL 限制；使用内联安全包装（`os.mkdir` 忽略 `0o700`）后
  完成运行。未删除临时目录，未使用真实输入或游戏。

## 修改

- `src/state_machine/machine.py`：新增当前场次结算分流 helper，供 `EXIT_MENU_OPEN`、
  `EXIT_CONFIRMING` 和非结算按钮进入的 `EXITING` 复用；出现结算页时清除待发送退出动作并转
  `BATTLE_FINISHED`。
- `EXITING` 记录是否由继续/回港按钮进入，结算动画留屏时不重复进入结算或重复点按钮。
- `ERROR_RECOVERY` 仅在识别到 `BATTLE_FINISHED`、仍有当前场次上下文且结算证据为真时转结算；
  无上下文或缺证据仍按原逻辑暂停。
- 退出菜单首次点击与确认框菜单兜底共享最后点击时间及 `(frame_id, frame_timestamp)`；补点需
  满足至少 500ms/配置等待，并且必须使用等待期到后首次看到的新帧；确认按钮优先，不重置超时
  起点，确认等待超时先于菜单补点处理。
- `src/runtime/runner.py`：仅启动日志增加本次场次目标、全局安全上限和既有会话时长上限。

## 验收

- 新增回归覆盖：退出三阶段结算分流、EXITING 超时同帧结算优先、按钮点击后留屏不重复、
  恢复上下文/证据边界、第三场死亡结算后开第四场、十场目标回港停止、补点同帧/间隔限制、
  持续新帧仍按确认等待超时、确认优先。既有暂停、停止、轮换和会话测试保持通过。
- `py -3.14 -m pytest tests/test_state_machine.py tests/test_gui_cooperation.py tests/test_runtime_service.py tests/test_session.py --basetemp pytest_work/exit_result_flash_1101/wrapped_acceptance_review --disable-warnings --maxfail=3`：
  `112 passed, 1 warning`。
- `py -3.14 -m ruff check src/state_machine/machine.py src/runtime/runner.py tests/test_state_machine.py`：
  `All checks passed!`。
- 以上为伪感知/输入回归；未进行真实十场连续运行，实机证据边界不变。
