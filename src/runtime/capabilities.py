"""能力开关 ``CapabilityFlags``（GUI 设计文档 15.2，M0 实施纪律）。

规则（15.2）：

1. 只有**已落地且离线测试通过**的修复才能置 True；每置 True 的项必须在本注释里写清
   “flag ← 代码位置 / 测试名”；
2. GUI 只读本模块导出的开关，不得自行推断“应该已经修好了”；
3. 未实机验证的项保持 False，由 Lead 在实机回归后统一回填（不在此处提前打开）。

M0 只把“运行服务真实接线并有测试”的项置 True；其余（尤其依赖实机样本的
``exit_authorized_gate`` / ``autopilot_*`` / ``ship_selected_evidence``）保持 False。
"""

from __future__ import annotations

from .messages import CapabilityFlags

#: 当前代码事实（M0）。每项注释即“flag ← 代码位置 / 测试名”的追溯记录。
CAPABILITIES = CapabilityFlags(
    # InputExecutor.close_gate_and_release/open_gate 已实现（10.4）；
    # M0 服务把 GUI 暂停/停止接到同一门禁：src/runtime/runner.py SessionRunner.request_pause/request_stop；
    # 测试：tests/test_runtime_service.py::test_pause_closes_gate_and_confirms_release
    input_gate=True,
    # 暂停/停止后由 runner 读取 InputExecutor.release_count 并展示释放确认（10.4/10.5）；
    # 测试：tests/test_runtime_service.py::test_pause_closes_gate_and_confirms_release
    release_confirmed=True,
    # M0 RuntimeService.start() 在 STARTING 前获取 SingleInstanceLock（10.3）；
    # 测试：tests/test_runtime_service.py::test_start_rejects_when_lock_held
    single_instance=True,
    # 统一清理函数 runner.cleanup_runtime() 生成 CleanupView；异常路径复用同一清理（10.8）；
    # 测试：tests/test_runtime_service.py::test_cleanup_failure_enters_error
    cleanup_on_error=True,
    # _EventBridge.set_battle_id 由 MachineDeps.on_battle_context 驱动（10.10）；
    # 测试：tests/test_main_safety.py::test_event_bridge_uses_current_battle_context
    battle_context=True,
    # 运行主循环检查 executor.failed 并转 PAUSED(input_failed)，恢复前 reset_after_failure（10.9）；
    # 测试：tests/test_main_safety.py::test_reset_input_after_failure_helper、
    #       tests/test_input_queue.py 的连续失败/释放用例
    input_failure_pause=True,
    # 以下保持 False（待 Lead 按实机回归口径回填）：
    # pause_keeps_battle / exit_authorized_gate / autopilot_full_map / autopilot_move_verified /
    # ship_selected_evidence / performance_monitor / overload_protection
)


def default_capabilities() -> CapabilityFlags:
    """返回当前代码事实对应的能力开关（快照/RuntimeService 使用）。"""
    return CAPABILITIES


__all__ = ["CAPABILITIES", "default_capabilities"]
