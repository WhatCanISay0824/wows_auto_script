"""公共数据类型：状态、动作、感知结果、观察结果。

本模块是各模块之间的契约（详细设计 5.1 / 9.1 / 附录 A），只依赖标准库。
任何模块都可以导入它，它不导入项目内其他模块（除同一级的 common 包）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    """主状态（详细设计 6.1）。"""

    INIT = "INIT"
    PORT = "PORT"
    START_COOP = "START_COOP"
    QUEUEING = "QUEUEING"
    LOADING = "LOADING"
    BATTLE_ACTIVE = "BATTLE_ACTIVE"
    BATTLE_FINISHED = "BATTLE_FINISHED"
    DEAD_PENDING_EXIT = "DEAD_PENDING_EXIT"
    EXIT_MENU_OPEN = "EXIT_MENU_OPEN"
    EXIT_CONFIRMING = "EXIT_CONFIRMING"
    EXITING = "EXITING"
    POST_EXIT_INTERFACE = "POST_EXIT_INTERFACE"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"

    @property
    def is_battle(self) -> bool:
        return self in (
            State.BATTLE_ACTIVE,
            State.BATTLE_FINISHED,
            State.DEAD_PENDING_EXIT,
        )

    @property
    def is_stopped(self) -> bool:
        return self in (State.PAUSED, State.STOPPING)


class ActionKind(StrEnum):
    """输入动作类型（详细设计 9.1）。"""

    CLICK_ABS = "click_abs"
    MOVE_ABS = "move_abs"
    MOVE_REL = "move_rel"
    KEY_DOWN = "key_down"
    KEY_TAP = "key_tap"
    KEY_UP = "key_up"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    MOUSE_WHEEL = "mouse_wheel"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class Action:
    """一个原子输入动作。

    ``CLICK_ABS`` / ``KEY_DOWN`` 等“复合动作”由输入线程展开执行，
    队列中相邻动作之间保证串行，不会被其他动作打断（详细设计 7.8 / 9.4）。

    实现补充（评审 P1-3）：
    - ``priority``：队列积压时的丢弃依据。0=普通（行为动作），10=导航点击，
      20=退出/紧急关键动作（参与丢弃时保留）。
    - ``expires_at``：入队时的单调时钟时间戳 + 有效期；输入线程执行前发现已过期
      则丢弃并记录，避免卡顿后补发过期操作（如过期的小地图点击）。
    - ``MOVE_REL`` 的 ``dx/dy`` 是 SendInput 的鼠标计数（mickey），不是屏幕像素；
      ``MOUSE_WHEEL`` 用 ``wheel_delta`` 表示滚轮格数（正=向上）。
    """

    kind: ActionKind
    x: int | None = None
    y: int | None = None
    dx: int | None = None
    dy: int | None = None
    key: str | None = None
    duration_ms: int = 0
    label: str = ""
    wheel_delta: int = 0
    priority: int = 0
    expires_at: float | None = None

    # --- 构造辅助 -----------------------------------------------------
    @classmethod
    def click_abs(cls, x: int, y: int, label: str = "", priority: int = 10) -> Action:
        return cls(ActionKind.CLICK_ABS, x=int(x), y=int(y), label=label, priority=priority)

    @classmethod
    def move_abs(cls, x: int, y: int, label: str = "", priority: int = 10) -> Action:
        """绝对移动到 (x, y)，不按下鼠标（用于先把光标移到滚动/点击区域）。"""
        return cls(ActionKind.MOVE_ABS, x=int(x), y=int(y), label=label, priority=priority)

    @classmethod
    def move_rel(cls, dx: int, dy: int, label: str = "") -> Action:
        return cls(ActionKind.MOVE_REL, dx=int(dx), dy=int(dy), label=label)

    @classmethod
    def key_down(cls, key: str, label: str = "", priority: int = 20) -> Action:
        return cls(ActionKind.KEY_DOWN, key=key, label=label, priority=priority)

    @classmethod
    def tap_key(cls, key: str, label: str = "") -> Action:
        """在当前位置按下并释放，后端作为一个动作执行。"""
        return cls(ActionKind.KEY_TAP, key=key, label=label)

    @classmethod
    def key_up(cls, key: str, label: str = "", priority: int = 20) -> Action:
        return cls(ActionKind.KEY_UP, key=key, label=label, priority=priority)

    @classmethod
    def key_press(cls, key: str, label: str = "") -> tuple[Action, Action]:
        """返回按下/释放两个动作，调用方按顺序提交。"""
        return cls.key_down(key, label), cls.key_up(key, label)

    @classmethod
    def mouse_down(cls, label: str = "") -> Action:
        return cls(ActionKind.MOUSE_DOWN, label=label)

    @classmethod
    def mouse_up(cls, label: str = "") -> Action:
        return cls(ActionKind.MOUSE_UP, label=label)

    @classmethod
    def wheel(cls, delta: int, label: str = "") -> Action:
        return cls(ActionKind.MOUSE_WHEEL, wheel_delta=int(delta), label=label)

    @classmethod
    def wait(cls, duration_ms: int, label: str = "") -> Action:
        return cls(ActionKind.WAIT, duration_ms=int(duration_ms), label=label)

    def with_expiry(self, expires_at: float) -> Action:
        from dataclasses import replace

        return replace(self, expires_at=expires_at)

    # --- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value}
        for name in ("x", "y", "dx", "dy", "key", "duration_ms", "wheel_delta", "priority", "expires_at"):
            value = getattr(self, name)
            if value not in (None, 0):
                data[name] = value
        if self.label:
            data["label"] = self.label
        return data


@dataclass(frozen=True, slots=True)
class Rect:
    """像素矩形，x/y 为左上角，w/h 为宽高，单位为像素。"""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.w < 0 or self.h < 0:
            raise ValueError(f"矩形宽高不能为负: {self!r}")

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def to_tuple(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    @classmethod
    def from_tuple(cls, value: tuple[int, int, int, int]) -> Rect:
        return cls(*value)

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True, slots=True)
class NormalizedRect:
    """归一化矩形，相对窗口客户区，取值 0～1（详细设计 1.4 / 3.2）。"""

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        for name in ("x1", "y1", "x2", "y2"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 超出 0～1: {value!r}")
        if self.x1 >= self.x2 or self.y1 >= self.y2:
            raise ValueError(f"归一化矩形左上角必须小于右下角: {self!r}")

    @property
    def w(self) -> float:
        return self.x2 - self.x1

    @property
    def h(self) -> float:
        return self.y2 - self.y1

    def to_pixels(self, frame_w: int, frame_h: int) -> Rect:
        """换算为窗口内像素矩形；浮点误差向上取整，避免裁掉边缘像素。"""
        x = int(round(self.x1 * frame_w))
        y = int(round(self.y1 * frame_h))
        right = int(round(self.x2 * frame_w))
        bottom = int(round(self.y2 * frame_h))
        return Rect(x, y, max(1, right - x), max(1, bottom - y))

    def to_list(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]

    @classmethod
    def from_list(cls, value: list[float] | tuple[float, ...]) -> NormalizedRect:
        if len(value) != 4:
            raise ValueError(f"归一化矩形需要 4 个数字: {value!r}")
        return cls(float(value[0]), float(value[1]), float(value[2]), float(value[3]))


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """游戏窗口信息（详细设计 5.2 / 9.3）。"""

    hwnd: int
    title: str
    process_name: str
    client_rect: Rect
    foreground: bool = False
    minimized: bool = False
    valid: bool = True

    @property
    def available(self) -> bool:
        return self.valid and not self.minimized and self.client_rect.w > 0 and self.client_rect.h > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "process_name": self.process_name,
            "client_rect": self.client_rect.to_dict(),
            "foreground": self.foreground,
            "minimized": self.minimized,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class MatchResult:
    """单个界面特征的模板匹配结果（详细设计 5.1）。"""

    feature_id: str
    found: bool
    score: float
    center: tuple[int, int] | None = None
    rect: tuple[int, int, int, int] | None = None
    scale: float = 1.0
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "found": self.found,
            "score": round(self.score, 4),
            "center": list(self.center) if self.center else None,
            "rect": list(self.rect) if self.rect else None,
            "scale": round(self.scale, 4),
        }


@dataclass(frozen=True, slots=True)
class MapMatch:
    """地图识别结果（详细设计 5.1 / 5.5）。

    ``affine`` 为底图像素 -> 小地图像素的 2x3 仿射矩阵，按行展开 6 个数；
    ``rotation_deg`` 为识别出的旋转角（顺时针为正），``scale`` 为缩放比例。
    """

    map_id: str
    inliers: int
    score: float
    affine: tuple[float, ...]
    reprojection_error: float
    timestamp: float = 0.0
    mirrored: bool = False
    rotation_deg: float = 0.0
    scale: float = 1.0
    candidates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "inliers": self.inliers,
            "score": round(self.score, 4),
            "reprojection_error": round(self.reprojection_error, 4),
            "mirrored": self.mirrored,
            "rotation_deg": round(self.rotation_deg, 2),
            "scale": round(self.scale, 4),
        }


@dataclass(frozen=True, slots=True)
class ShipPosition:
    """自身舰船位置（详细设计 5.1 / 5.7）。"""

    minimap_px: tuple[float, float]
    world_xy: tuple[float, float] | None
    heading_deg: float | None
    speed_mps: float
    confidence: float
    source: str  # marker / predicted / lost

    def to_dict(self) -> dict[str, Any]:
        return {
            "minimap_px": [round(self.minimap_px[0], 2), round(self.minimap_px[1], 2)],
            "world_xy": [round(v, 2) for v in self.world_xy] if self.world_xy else None,
            "heading_deg": None if self.heading_deg is None else round(self.heading_deg, 2),
            "speed_mps": round(self.speed_mps, 3),
            "confidence": round(self.confidence, 3),
            "source": self.source,
        }


#: 血条证据等级（ISS-002 / 评审 10.2）：
#: - ``alive``：血条区域有效且填充比例达到存活下限；
#: - ``zero``：血条区域有效且填充比例低于空条上限（确认归零）；
#: - ``unknown``：血条区域存在但有效/填充比例无法判定（遮挡、处于空→活死区）；
#: - ``unavailable``：没有可用血条区域/帧/模板，无法判定。
HP_EVIDENCE_LEVELS: tuple[str, ...] = ("alive", "zero", "unknown", "unavailable")


@dataclass(frozen=True, slots=True)
class DeathSignals:
    """死亡信号投票结果（详细设计 5.9 / 评审 10.2）。

    退出授权只认 ``exit_authorized``：己方血量证据有效 + 连续帧确认归零 + 证据在 TTL 内；
    ``banner`` / ``hp_bar_absent`` / ``marker_lost`` / ``spectator_ui`` 仍参与投票与日志，
    但**不能单独授权提前退出**（ISS-002）。
    """

    hp_bar_absent: bool
    hp_zero: bool
    banner: bool
    marker_lost: bool
    spectator_ui: bool
    score: float
    stable_ms: int
    timestamp: float = 0.0
    votes: dict[str, bool] = field(default_factory=dict)
    # --- ISS-002：血条归零证据（评审 10.2）------------------------------
    #: 血条区域填充列比例（0~1）；None 表示没有可用血条区域或帧。
    hp_fill_ratio: float | None = None
    #: 证据等级，取值见 :data:`HP_EVIDENCE_LEVELS`。
    hp_evidence: str = "unavailable"
    #: 是否授权提前退出（只由感知层根据确认归零证据给出）。
    exit_authorized: bool = False
    #: 最近一次“确认归零”证据的时间戳（单调时钟秒；0 表示从未确认）。
    evidence_at: float = 0.0
    #: 当前血量；目前只确认数字零值，非零或未判定时为 None。
    hp_current: int | None = None

    @property
    def dead(self) -> bool:
        # 综合判定由 DeathDetector 负责；此处只提供便捷判断。
        return self.score >= 0.75 and self.stable_ms >= 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "hp_bar_absent": self.hp_bar_absent,
            "hp_zero": self.hp_zero,
            "banner": self.banner,
            "marker_lost": self.marker_lost,
            "spectator_ui": self.spectator_ui,
            "score": round(self.score, 3),
            "stable_ms": self.stable_ms,
            "votes": dict(self.votes),
            "hp_fill_ratio": None if self.hp_fill_ratio is None else round(self.hp_fill_ratio, 4),
            "hp_evidence": self.hp_evidence,
            "exit_authorized": self.exit_authorized,
            "evidence_at": round(self.evidence_at, 3),
            "hp_current": self.hp_current,
        }


@dataclass(frozen=True, slots=True)
class EnemyContact:
    """连续新帧确认的小地图实心敌舰标记，坐标相对小地图内容区。"""

    minimap_px: tuple[float, float]


@dataclass(frozen=True, slots=True)
class CombatObservation:
    timestamp: float
    frame_id: int
    own_px: tuple[float, float] | None = None
    camera_bearing_deg: float | None = None
    enemies: tuple[EnemyContact, ...] = ()
    enemy_candidates: int = 0


@dataclass(frozen=True, slots=True)
class Observation:
    """一帧结构化观察结果（详细设计 5.1）。

    与设计文档的差异：补充 ``window_missing`` / ``capture_error`` 字段，
    用于附录 B.1 主循环中的 ``observation.window_missing`` 判断。
    """

    timestamp: float
    window_rect: tuple[int, int, int, int]
    features: dict[str, MatchResult] = field(default_factory=dict)
    state_guess: str | None = None
    map_match: MapMatch | None = None
    ship: ShipPosition | None = None
    death: DeathSignals | None = None
    window_missing: bool = False
    capture_error: str | None = None
    # 帧序号与截屏时间：行为模块用它判断感知结果是否足够新（防止在过期画面上输入）
    frame_id: int = 0
    frame_timestamp: float = 0.0
    combat: CombatObservation | None = None

    def feature(self, feature_id: str) -> MatchResult | None:
        return self.features.get(feature_id)

    def found(self, feature_id: str) -> bool:
        result = self.features.get(feature_id)
        return bool(result and result.found)

    def score(self, feature_id: str) -> float:
        result = self.features.get(feature_id)
        return result.score if result else 0.0
