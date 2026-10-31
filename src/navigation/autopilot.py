"""简化导航：M 全屏地图点选自动驾驶（ISS-001 修复，实现补充 10.1）。

实机确认（2026-10-30，ISS-001）：直接点击战斗画面右下角的小地图**不会**设置航向；
正确交互是“按 M 打开全屏战术地图 → 等待地图界面就绪 → 在目标位置左键点选 →
再按 M 关闭地图返回战斗”。因此 autopilot 的运行默认是 ``navigation.surface=full_map``，
旧的小地图直点路径保留为 ``surface=minimap`` 的失败降级/兼容路径。

目标点口径不变（实现补充 9.1，用户确认）：
- 取开局时自身舰船标记在小地图内容区的归一化位置 ``(own_x, own_y)``；
- 敌方区域 = 我方出生点关于地图中心的对称点，横坐标固定到中线，
  目标 = ``(0.5, 1 - own_y)``（再叠加 ``navigation.autopilot_offset`` 与安全内缩）；
- 识别不到自身标记时最多等待 10 秒，之后回退到 ``(0.5, 0.5)`` 并记录 warning。

坐标换算（实现补充 10.1）：
- ``navigation.full_map_rect`` 是全屏地图**内容区**相对窗口客户区的归一化矩形；
- 目标归一化坐标 → full_map_rect 内像素 → 客户区像素 → 屏幕像素
  （``Observation.window_rect`` 是客户区屏幕矩形）；
- ``surface=minimap`` 降级路径沿用 ``profile.minimap`` 的归一化内容区换算。

full_map_rect 标定依据（2026-10-30 本地实机 M 地图截图 211242 / 211253，2559x1599，
用 numpy/OpenCV 检测网格外边框得到，两张截图结果一致）：
- 截图坐标下内容区四边：上边 y≈111.5、下边 y≈1399.5、左边 x 从顶部 656.7 收到
  底部 608.1、右边 x 从顶部 1902.0 展到底部 1950.5；
- 归一化到窗口客户区（按 2559x1599 归一化，2560x1600 客户区通用）：
  ``[0.2376, 0.0698, 0.7622, 0.8752]``（内容区外接矩形；不含网格字母/数字标签，
  不含右侧教程面板；顶部比分栏是覆盖在地图上的 HUD，不属于内容区边界）；
- 实机截图中全屏地图呈“上窄下宽”的透视梯形（左右边框在 1289px 高度内各偏约 48px，
  10 行网格间距从约 120px 递增到约 136px）。按外接矩形做线性换算时，纵向（v 方向）
  相对真实透视的残差不超过内容区高度的约 2%（≈24px），对“向敌方区域航行”的目标
  选择可接受；若后续要求更精确，应改为单应矩阵标定替换矩形换算；
- 假设（**待实机确认**）：全屏地图每次打开都整图显示、不随视角平移/缩放；
  M 键能可靠开/关地图；顶部比分栏不拦截地图点击。

非阻塞相位状态机（``tick`` 每次只推进当前步骤，只返回该步骤的动作；``stop()`` 重置，
不补发旧点击）：
- ``idle``：未启动；
- ``wait_marker``：等待自身标记（最多 10s）或重发时间到；
- ``open_map``：提交 M 开图（key_down/key_up 同批，由输入线程保证释放）；
- ``wait_map``：等待地图界面就绪（``full_map_open_wait_s``；固定时长属于**时序假设**，
  需实机验证，不是模板判断）；
- ``click``：在全屏地图内容区点选目标（或降级路径点小地图）；
- ``wait_close``：等待点击生效（``full_map_close_wait_s``）后提交 M 关图；
- ``verify``：关图后等待 ``full_map_verify_s``，用自身标记位移或船速确认航向生效；
- ``reclick``：等待下一次周期重发，或验证失败后的重试/降级。

失败处理：``verify`` 失败记 ``autopilot_verify_failed``，按 ``full_map_retry`` 重试；
仍失败则降级 ``surface=minimap`` 一次并记 warning，不重复盲点同一目标。成功记
``autopilot_confirmed``。``blocks_behavior`` 从提交开图到关图验证结束前为 True，
状态机据此暂停随机行为；本模块从不发送 WASD，也不用 ESC 关地图（只用 keymap 的
``full_map`` 动作）。

待实机确认（不得据此宣称已修复）：点击后是否真的设置航向、M 开/关时序、地图整图
居中假设、比分栏遮挡；``surface=minimap`` 降级路径无法验证航向，只是兼容保留。
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from enum import StrEnum

from ..common.types import Action, Observation, Rect
from ..config.models import ClientProfile, KeyMap, NavigationConfig

logger = logging.getLogger(__name__)

# 目标点离小地图内容区边缘的安全内缩（归一化）
_MINIMAP_EDGE_SAFE = 0.02
# 全屏地图内容区外接矩形：透视梯形在矩形左右上角留有空白，横向内缩要更大，
# 否则 u 取边缘时会点到地图内容之外（实测梯形上边比下边窄约 96px）。
_FULL_MAP_EDGE_SAFE_X = 0.04
_FULL_MAP_EDGE_SAFE_Y = 0.02
# 等待自身标记的上限（实现补充 9.1）
_MARKER_WAIT_S = 10.0
# 验证失败后的重试/中止间隔（秒）
_RETRY_GAP_S = 1.0
# 地图序列期间感知连续缺失的宽限时间（超过则中止当前序列，避免盲点）
_OBS_MISSING_GRACE_S = 2.0


class _Phase(StrEnum):
    """autopilot 相位（实现补充 10.1 的 8 个状态）。"""

    IDLE = "idle"
    WAIT_MARKER = "wait_marker"
    OPEN_MAP = "open_map"
    WAIT_MAP = "wait_map"
    CLICK = "click"
    WAIT_CLOSE = "wait_close"
    VERIFY = "verify"
    RECLICK = "reclick"


@dataclass(frozen=True, slots=True)
class AutoPilotTarget:
    """一次航点目标（归一化坐标 + 内容区像素 + 屏幕像素）。"""

    normalized: tuple[float, float]
    content_px: tuple[int, int]
    screen_px: tuple[int, int]
    from_own_marker: bool
    surface: str = "minimap"

    def to_dict(self) -> dict[str, object]:
        return {
            "normalized": [round(self.normalized[0], 4), round(self.normalized[1], 4)],
            "content_px": list(self.content_px),
            "screen": list(self.screen_px),
            "from_own_marker": self.from_own_marker,
            "surface": self.surface,
        }


@dataclass(slots=True)
class _VerifyState:
    """关图后的航向验证状态。"""

    started_at: float
    ref_px: tuple[float, float] | None = None
    displacement_px: float = 0.0
    speed_mps: float = 0.0
    confirmed: bool = False


class AutoPilotNavigator:
    """把“M 全屏地图点选”转成非阻塞动作序列的简化导航器。

    与 :class:`src.navigation.navigator.Navigator` 的接口保持一致的部分：
    ``start(now)`` / ``tick(observation, now)`` / ``stop()``；区别是不需要地图包与
    世界坐标标定，只依赖客户端小地图标定、全屏地图内容区矩形与自身标记识别。
    """

    def __init__(
        self,
        config: NavigationConfig,
        profile: ClientProfile,
        *,
        rng: random.Random | None = None,
        on_event=None,
        logger_: logging.Logger | None = None,
        keymap: KeyMap | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self._rng = rng or random.Random()
        self._on_event = on_event
        self._logger = logger_ or logger
        # 按键映射：显式注入优先；未注入时按 profile_dir/keys_file 懒加载（生产装配路径）
        self._keymap = keymap
        self._map_key: str | None = None
        self._map_key_resolved = False
        self._active = False
        self._phase = _Phase.IDLE
        self._surface = "full_map" if config.surface == "full_map" else "minimap"
        self._degraded = False
        self._map_sequence = False  # 开图开始到验证结束前为 True（暂停随机行为）
        self._map_open = False  # 已提交开图按键、尚未提交关图按键
        self._own_norm: tuple[float, float] | None = None
        self._next_click_at = 0.0
        self._phase_deadline = 0.0
        self._started_at = 0.0
        self._last_target: AutoPilotTarget | None = None
        self._last_click_at = 0.0
        self._clicks = 0
        self._missing_marker_warned = False
        self._attempt = 0  # 连续验证失败次数（用于 full_map_retry）
        self._verify: _VerifyState | None = None

    # ------------------------------------------------------------------
    # 属性（供状态机/测试观察）
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    @property
    def phase(self) -> str:
        return self._phase.value

    @property
    def surface(self) -> str:
        return self._surface

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def blocks_behavior(self) -> bool:
        """开图动作开始到关图验证结束前为 True，状态机据此暂停随机行为。"""
        return self._active and self._map_sequence

    @property
    def map_open(self) -> bool:
        return self._map_open

    @property
    def last_target(self) -> AutoPilotTarget | None:
        return self._last_target

    @property
    def clicks(self) -> int:
        return self._clicks

    @property
    def own_position_norm(self) -> tuple[float, float] | None:
        return self._own_norm

    @property
    def next_click_at(self) -> float:
        return self._next_click_at

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, now: float) -> None:
        """进入战斗时启动；首个可用的自身标记到位后开始开图点选。"""
        self._active = True
        self._own_norm = None
        self._last_target = None
        self._clicks = 0
        self._last_click_at = float(now)
        self._next_click_at = float(now)
        self._started_at = float(now)
        self._phase_deadline = float(now)
        self._missing_marker_warned = False
        self._phase = _Phase.RECLICK  # 首个 tick 立即开始一轮（等待标记/重发时间已到）
        self._surface = "full_map" if self.config.surface == "full_map" else "minimap"
        self._degraded = False
        self._map_sequence = False
        self._map_open = False
        self._attempt = 0
        self._verify = None
        self._emit("autopilot_start", now, surface=self._surface)
        self._logger.info(
            "autopilot 导航启动：surface=%s，目标=%s（M 全屏地图点选后交给游戏自动驾驶，不碰 WASD）",
            self._surface,
            self.config.autopilot_target,
        )

    def stop(self) -> None:
        """暂停/停止：立即重置相位，不补发旧点击。

        若在开/关全屏地图过程中被停止，游戏内地图可能仍处于打开状态；暂停期间输入
        门禁关闭，不能补发 M 关闭，恢复后的首轮行为需实机确认（见任务报告）。
        """
        was_map_sequence = self._map_sequence
        map_was_open = self._map_open
        self._active = False
        self._phase = _Phase.IDLE
        self._map_sequence = False
        self._map_open = False
        self._verify = None
        self._phase_deadline = 0.0
        self._next_click_at = 0.0
        if map_was_open:
            self._logger.warning(
                "autopilot 在开/关全屏地图过程中停止：地图可能仍处于打开状态，"
                "恢复后的首轮开图行为需要实机确认"
            )
        self._emit("autopilot_stop", self._last_click_at, map_maybe_open=map_was_open)
        del was_map_sequence

    def reset(self) -> None:
        self._active = False
        self._phase = _Phase.IDLE
        self._own_norm = None
        self._last_target = None
        self._clicks = 0
        self._next_click_at = 0.0
        self._phase_deadline = 0.0
        self._map_sequence = False
        self._map_open = False
        self._verify = None
        self._attempt = 0
        self._degraded = False
        self._surface = "full_map" if self.config.surface == "full_map" else "minimap"

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def tick(self, observation: Observation | None, now: float) -> list[Action]:
        if not self._active:
            return []
        now = float(now)
        if observation is None or observation.window_missing:
            return self._handle_missing_observation(now)
        self._remember_own_position(observation)

        phase = self._phase
        if phase is _Phase.WAIT_MARKER:
            if not self._marker_ready(now):
                return []
            return self._begin_cycle(now)
        if phase is _Phase.RECLICK:
            if now < self._next_click_at:
                return []
            return self._begin_cycle(now)
        if phase is _Phase.OPEN_MAP:
            return self._open_map(now)
        if phase is _Phase.WAIT_MAP:
            if now < self._phase_deadline:
                return []
            self._phase = _Phase.CLICK
            return []
        if phase is _Phase.CLICK:
            return self._click(observation, now)
        if phase is _Phase.WAIT_CLOSE:
            if now < self._phase_deadline:
                return []
            return self._close_map(observation, now)
        if phase is _Phase.VERIFY:
            self._update_verify(observation)
            verify = self._verify
            if verify is not None and verify.confirmed:
                self._confirm(now)
                return []
            if now >= self._phase_deadline:
                self._verify_failed(now)
            return []
        return []

    # ------------------------------------------------------------------
    # 相位推进
    # ------------------------------------------------------------------
    def _begin_cycle(self, now: float) -> list[Action]:
        if not self._marker_ready(now):
            self._phase = _Phase.WAIT_MARKER
            return []
        if self._surface == "full_map":
            self._phase = _Phase.OPEN_MAP
            self._map_sequence = True
        else:
            self._phase = _Phase.CLICK
        return []

    def _open_map(self, now: float) -> list[Action]:
        if self.config.full_map_rect is None:
            self._degrade_to_minimap(now, "missing_full_map_rect")
            return []
        key = self._resolve_map_key()
        if key is None:
            self._degrade_to_minimap(now, "missing_full_map_key")
            return []
        self._map_sequence = True
        self._map_open = True
        self._phase = _Phase.WAIT_MAP
        self._phase_deadline = now + max(0.0, float(self.config.full_map_open_wait_s))
        self._emit("autopilot_map_open", now, key=key, wait_s=self.config.full_map_open_wait_s)
        self._logger.info(
            "autopilot 按 M 打开全屏地图（action=%s key=%s），等待 %.2fs",
            self.config.full_map_toggle_key,
            key,
            self.config.full_map_open_wait_s,
        )
        return self._key_actions(key, "autopilot_open_map")

    def _click(self, observation: Observation, now: float) -> list[Action]:
        full_map = self._surface == "full_map" and self._map_open
        target = self.compute_full_map_target(observation) if full_map else self.compute_target(observation)
        if target is None:
            self._logger.warning(
                "autopilot 无法换算点击坐标（surface=%s，full_map_rect=%s），本周期不盲点",
                self._surface,
                self.config.full_map_rect,
            )
            self._emit("autopilot_click_skipped", now, surface=self._surface)
            actions: list[Action] = []
            if full_map:
                # 地图已经打开但无法安全点选：先尽力按 M 关图，避免地图残留在打开状态
                actions = self._close_map_best_effort(now, "target_unavailable")
                self._degrade_to_minimap(now, "target_unavailable")
            else:
                self._map_sequence = False
                self._map_open = False
                self._phase = _Phase.RECLICK
                self._next_click_at = now + _RETRY_GAP_S
            return actions
        self._last_target = target
        self._last_click_at = now
        self._clicks += 1
        self._emit("autopilot_click", now, **target.to_dict())
        action = Action.click_abs(target.screen_px[0], target.screen_px[1], label="autopilot_click")
        if full_map:
            self._phase = _Phase.WAIT_CLOSE
            self._phase_deadline = now + max(0.0, float(self.config.full_map_close_wait_s))
        else:
            self._phase = _Phase.RECLICK
            self._next_click_at = now + self._sample_interval()
        return [action]

    def _close_map_best_effort(self, now: float, reason: str) -> list[Action]:
        """异常路径下尽力按 M 关图：地图打开期间任何失败都不应把地图留在打开状态。"""
        if not self._map_open:
            return []
        self._map_open = False
        key = self._resolve_map_key()
        if key is None:
            self._logger.warning(
                "autopilot 全屏地图异常（%s）且无可用按键映射，地图可能仍处于打开状态，需实机确认",
                reason,
            )
            self._emit("autopilot_map_close_failed", now, reason=reason, map_maybe_open=True)
            return []
        self._logger.warning("autopilot 全屏地图异常（%s），先尽力按 M 关图", reason)
        self._emit("autopilot_map_close", now, key=key, reason=reason)
        return self._key_actions(key, "autopilot_close_map")

    def _close_map(self, observation: Observation, now: float) -> list[Action]:
        key = self._resolve_map_key()
        if key is None:  # 开图前已解析成功，正常不会走到这里；保守中止
            self._abort_sequence(now, "missing_full_map_key")
            return []
        self._map_open = False
        self._phase = _Phase.VERIFY
        self._phase_deadline = now + max(0.0, float(self.config.full_map_verify_s))
        self._verify = _VerifyState(started_at=now)
        self._snapshot_verify_reference(observation)
        self._emit("autopilot_map_close", now, key=key, verify_s=self.config.full_map_verify_s)
        self._logger.info(
            "autopilot 按 M 关闭全屏地图，等待 %.2fs 验证航向（点击时目标 %s）",
            self.config.full_map_verify_s,
            self._last_target.normalized if self._last_target else None,
        )
        return self._key_actions(key, "autopilot_close_map")

    # ------------------------------------------------------------------
    # 验证
    # ------------------------------------------------------------------
    def _snapshot_verify_reference(self, observation: Observation | None) -> None:
        verify = self._verify
        if verify is None or observation is None:
            return
        ship = observation.ship
        if ship is None or ship.source == "lost":
            return
        verify.ref_px = (float(ship.minimap_px[0]), float(ship.minimap_px[1]))

    def _update_verify(self, observation: Observation) -> None:
        verify = self._verify
        if verify is None:
            return
        ship = observation.ship
        if ship is None or ship.source == "lost":
            return
        current = (float(ship.minimap_px[0]), float(ship.minimap_px[1]))
        if verify.ref_px is None:
            # 关图时自身标记不可用：用验证窗口内首个可用样本作为参考
            verify.ref_px = current
            return
        displacement = math.dist(current, verify.ref_px)
        verify.displacement_px = max(verify.displacement_px, displacement)
        verify.speed_mps = abs(float(ship.speed_mps))
        if (
            verify.displacement_px >= float(self.config.autopilot_verify_min_move_px)
            or verify.speed_mps >= float(self.config.autopilot_verify_min_speed)
        ):
            verify.confirmed = True

    def _confirm(self, now: float) -> None:
        verify = self._verify
        self._map_sequence = False
        self._map_open = False
        self._attempt = 0
        self._verify = None
        self._phase = _Phase.RECLICK
        self._next_click_at = now + self._sample_interval()
        detail = {
            "displacement_px": round(verify.displacement_px, 2) if verify else 0.0,
            "speed_mps": round(verify.speed_mps, 3) if verify else 0.0,
            "clicks": self._clicks,
        }
        self._emit("autopilot_confirmed", now, **detail)
        self._logger.info(
            "autopilot 航向确认生效：位移 %.2fpx 或船速 %.2f m/s（阈值 %.2fpx / %.2f m/s），"
            "下一次重发在 %.0fs 后",
            detail["displacement_px"],
            detail["speed_mps"],
            self.config.autopilot_verify_min_move_px,
            self.config.autopilot_verify_min_speed,
            self._next_click_at - now,
        )

    def _verify_failed(self, now: float) -> None:
        verify = self._verify
        self._attempt += 1
        attempt = self._attempt
        self._map_sequence = False
        self._map_open = False
        self._verify = None
        detail = {
            "attempt": attempt,
            "retry_limit": int(self.config.full_map_retry),
            "displacement_px": round(verify.displacement_px, 2) if verify else 0.0,
            "speed_mps": round(verify.speed_mps, 3) if verify else 0.0,
            "target": list(self._last_target.normalized) if self._last_target else None,
        }
        self._emit("autopilot_verify_failed", now, **detail)
        self._logger.warning(
            "autopilot 全屏地图点选后 %.1fs 内未确认航向（位移 %.2fpx、船速 %.2f m/s，第 %d 次尝试）",
            self.config.full_map_verify_s,
            detail["displacement_px"],
            detail["speed_mps"],
            attempt,
        )
        if attempt <= int(self.config.full_map_retry):
            self._phase = _Phase.RECLICK
            self._next_click_at = now + _RETRY_GAP_S
            return
        self._degrade_to_minimap(now, "verify_failed")

    def _degrade_to_minimap(self, now: float, reason: str) -> None:
        if self._surface == "minimap":
            self._phase = _Phase.RECLICK
            self._next_click_at = now + _RETRY_GAP_S
            return
        self._logger.warning(
            "autopilot 全屏地图点选失败（%s），降级为直接点击小地图旧路径并记录 warning；"
            "旧路径无法验证航向，仅作兼容保留",
            reason,
        )
        self._emit("autopilot_degrade", now, reason=reason, to_surface="minimap")
        self._surface = "minimap"
        self._degraded = True
        self._map_sequence = False
        self._map_open = False
        self._attempt = 0
        self._verify = None
        self._phase = _Phase.RECLICK
        self._next_click_at = now + _RETRY_GAP_S

    def _abort_sequence(self, now: float, reason: str, *, map_maybe_open: bool = False) -> None:
        if self._map_sequence:
            self._logger.warning(
                "autopilot 中止当前全屏地图动作序列：%s（不补发旧点击）%s",
                reason,
                "；全屏地图可能仍处于打开状态，需实机确认/人工按 M 关闭" if map_maybe_open else "",
            )
            self._emit("autopilot_abort", now, reason=reason, map_maybe_open=map_maybe_open)
        self._map_sequence = False
        self._map_open = False
        self._verify = None
        self._phase = _Phase.RECLICK
        self._next_click_at = now + _RETRY_GAP_S

    def _handle_missing_observation(self, now: float) -> list[Action]:
        """地图动作序列期间感知不可用：不盲点；超过宽限时间则尽力关图后中止。"""
        if not self._map_sequence or now < self._phase_deadline + _OBS_MISSING_GRACE_S:
            return []
        map_was_open = self._map_open
        actions = self._close_map_best_effort(now, "observation_missing")
        self._abort_sequence(
            now,
            "observation_missing",
            map_maybe_open=map_was_open and self._resolve_map_key() is None,
        )
        return actions

    # ------------------------------------------------------------------
    # 目标与坐标换算
    # ------------------------------------------------------------------
    def _marker_ready(self, now: float) -> bool:
        if self.config.autopilot_target != "symmetric":
            return True
        if self._own_norm is not None:
            return True
        return now - self._started_at >= _MARKER_WAIT_S

    def _remember_own_position(self, observation: Observation) -> None:
        ship = observation.ship
        if ship is None or ship.source == "lost":
            return
        if self._own_norm is not None:
            return
        window = Rect(*observation.window_rect)
        if window.w <= 0 or window.h <= 0:
            return
        content = self._minimap_content_rect(observation)
        if content is None or content.w <= 0 or content.h <= 0:
            return
        px, py = ship.minimap_px
        x = min(max(float(px) / content.w, 0.0), 1.0)
        y = min(max(float(py) / content.h, 0.0), 1.0)
        self._own_norm = (x, y)
        self._emit("autopilot_spawn", self._last_click_at, x=round(x, 4), y=round(y, 4))
        self._logger.info("autopilot 记录我方出生点（小地图归一化）: (%.3f, %.3f)", x, y)

    def compute_target(self, observation: Observation) -> AutoPilotTarget | None:
        """minimap 降级路径：目标归一化 → 小地图内容区像素 → 屏幕像素。"""
        content_rel = self._minimap_content_rect(observation)
        window = Rect(*observation.window_rect)
        if content_rel is None or window.w <= 0 or window.h <= 0:
            return None
        target_x, target_y, from_marker = self._normalized_target()
        target_x = min(max(target_x, _MINIMAP_EDGE_SAFE), 1.0 - _MINIMAP_EDGE_SAFE)
        target_y = min(max(target_y, _MINIMAP_EDGE_SAFE), 1.0 - _MINIMAP_EDGE_SAFE)
        local_x = content_rel.x + int(round(target_x * content_rel.w))
        local_y = content_rel.y + int(round(target_y * content_rel.h))
        return AutoPilotTarget(
            normalized=(target_x, target_y),
            content_px=(local_x, local_y),
            screen_px=(window.x + local_x, window.y + local_y),
            from_own_marker=from_marker,
            surface="minimap",
        )

    def compute_full_map_target(self, observation: Observation) -> AutoPilotTarget | None:
        """全屏地图路径：目标归一化 → full_map_rect 内像素 → 屏幕像素。"""
        full_map_rect = self.config.full_map_rect
        window = Rect(*observation.window_rect)
        if full_map_rect is None or window.w <= 0 or window.h <= 0:
            return None
        rect = full_map_rect.to_pixels(window.w, window.h)
        if rect.w <= 8 or rect.h <= 8:
            return None
        target_x, target_y, from_marker = self._normalized_target()
        target_x = min(max(target_x, _FULL_MAP_EDGE_SAFE_X), 1.0 - _FULL_MAP_EDGE_SAFE_X)
        target_y = min(max(target_y, _FULL_MAP_EDGE_SAFE_Y), 1.0 - _FULL_MAP_EDGE_SAFE_Y)
        local_x = rect.x + int(round(target_x * rect.w))
        local_y = rect.y + int(round(target_y * rect.h))
        return AutoPilotTarget(
            normalized=(target_x, target_y),
            content_px=(local_x, local_y),
            screen_px=(window.x + local_x, window.y + local_y),
            from_own_marker=from_marker,
            surface="full_map",
        )

    def _normalized_target(self) -> tuple[float, float, bool]:
        offset_x, offset_y = self.config.autopilot_offset
        if self.config.autopilot_target == "center":
            return 0.5 + offset_x, 0.5 + offset_y, False
        if self._own_norm is not None:
            # 对称点：x 固定中线，y 取我方出生点关于中心的对称位置
            _own_x, own_y = self._own_norm
            return 0.5 + offset_x, (1.0 - own_y) + offset_y, True
        if not self._missing_marker_warned:
            self._missing_marker_warned = True
            self._logger.warning("autopilot 未识别到自身标记，敌区目标回退地图中心")
            self._emit("autopilot_fallback_center", self._last_click_at)
        return 0.5 + offset_x, 0.5 + offset_y, False

    def _minimap_content_rect(self, observation: Observation) -> Rect | None:
        minimap = self.profile.minimap
        if minimap is None:
            return None
        window = Rect(*observation.window_rect)
        if window.w <= 0 or window.h <= 0:
            return None
        rect = minimap.rect.to_pixels(window.w, window.h)
        border = max(0, int(minimap.border_px))
        width = rect.w - border * 2
        height = rect.h - border * 2
        if width <= 8 or height <= 8:
            return None
        return Rect(rect.x + border, rect.y + border, width, height)

    def _sample_interval(self) -> float:
        low, high = self.config.autopilot_reclick_s
        low = max(1.0, float(low))
        high = max(low, float(high))
        return self._rng.uniform(low, high)

    # ------------------------------------------------------------------
    # 按键与事件
    # ------------------------------------------------------------------
    def _resolve_map_key(self) -> str | None:
        if self._map_key_resolved:
            return self._map_key
        self._map_key_resolved = True
        keymap = self._keymap
        if keymap is None and self.profile.profile_dir is not None:
            try:
                from ..config.loader import load_keymap

                keymap = load_keymap(self.profile.profile_dir / self.profile.keys_file)
            except Exception as exc:  # 文件缺失/解析失败：不阻塞导航，后续降级
                self._logger.warning("autopilot 加载 keys.yaml 失败: %s", exc)
                keymap = None
        if keymap is None:
            self._logger.warning("autopilot 没有可用的按键映射，无法发送全屏地图开关按键")
            return None
        try:
            self._map_key = keymap.key(self.config.full_map_toggle_key)
        except KeyError:
            self._logger.warning(
                "autopilot 的 keys.yaml 缺少全屏地图动作映射 %r（navigation.full_map_toggle_key）",
                self.config.full_map_toggle_key,
            )
            self._map_key = None
        return self._map_key

    def _key_actions(self, key: str, label: str) -> list[Action]:
        """开/关地图按键按 down/up 同批返回，由输入线程保证释放。"""
        return [
            Action.key_down(key, label=label),
            Action.key_up(key, label=label),
        ]

    def _emit(self, event: str, now: float, **detail) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event, {"event": event, "at": round(float(now), 3), **detail})
        except Exception:  # 事件回调失败不影响导航
            self._logger.debug("autopilot 事件回调失败: %s", event)
