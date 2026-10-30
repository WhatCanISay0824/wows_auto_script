# M0/M1 首期实现计划（wows_script）

> **执行方式：** Agent Teams 并行实现。Lead 维护公共契约与集成，各模块负责人按任务写作用域（write scope）实现并自测。
> 任务板：`team_task_*`。计划中每一项都有独立可运行的 pytest 交付物。

**Goal：** 依据 `design/战舰世界联合作战挂机脚本详细设计文档.docx` v0.4，搭建可离线运行/测试的项目骨架：公共类型与坐标、配置与地图包加载校验、GDI 截屏、模板/ORB 感知、SendInput 输入、状态机与步骤执行、导航、随机行为、SQLite 日志、校验工具，并配套单元测试。

**Architecture：** 单向数据流：截屏 → 感知 Observation → 状态机决策 → 动作队列 → SendInput；导航与行为只产生 Action，不直接调用键鼠；存储线程只消费事件队列。模块依赖见 `src/` 各包与 `design/详细设计评审与实现补充.md`。

**Tech Stack：** Windows 11、Python 3.14.5、opencv-python 5.0.0.93、NumPy 2.5.1、PyYAML 6.0.3、ctypes/SendInput、sqlite3、tkinter、pytest 9.1.1、ruff 0.15.21。

## Global Constraints

- 只支持 Windows；代码在 `src/`，测试在 `tests/`，运行入口 `python -m src.main --profile cn360`，工具入口 `python -m src.tools.<name>`。
- 不引入 pyautogui / pydirectinput / OCR / 深度学习依赖；不读取游戏内存、不注入、不改客户端。
- 输入使用 ctypes 调 SendInput；截屏使用 ctypes 调 GDI BitBlt；界面识别只用模板匹配；地图识别只用 ORB + 局部仿射。
- 界面参数、按键、退出步骤、地图数据全部配置化，代码不写死客户端专有常量；配置唯一来源 `config.yaml` + `profiles/<client>/` + `maps/<map_id>/`。
- 时间：计时用单调时钟（`src/common/clock.py`），持久化用带时区 ISO-8601。
- 日志/截图/数据库只存本地；不写账号、密码、令牌；窗口标题不整体入库。
- 所有单元测试不得真的发送键鼠事件、不得要求游戏窗口存在；Win32 调用必须可注入 fake 或只在纯函数层测试。
- 公共契约（`src/common/**`、`src/config/models.py`、`config.yaml`、`profiles/cn360/**`、`tests/conftest.py`、`tests/helpers.py`）由 Lead 维护，模块负责人不要修改；确需扩展先报告 Lead。
- 每次提交前：`python -m pytest tests -q` 与 `python -m ruff check src tests` 通过（至少自己负责的测试通过）。

## 公共契约（已落地，负责人只读）

```
src/common/types.py      State, Action, ActionKind, Rect, NormalizedRect, WindowInfo,
                         MatchResult, MapMatch, ShipPosition, DeathSignals, Observation
src/common/coords.py     ImageOrientation, WorldImageTransform, BattleCalibration,
                         CalibrationMode, 仿射工具, expected_minimap_scale
src/common/clock.py      Clock/SystemClock/FakeClock, Sleeper/SystemSleeper/FakeSleeper, iso 时间
src/common/errors.py     ValidationIssue/Severity/ConfigError/CaptureUnavailable/InputUnavailable/
                         MapMatchError/RouteUnavailable/StepFailure/SafetyStop/StopRequest
src/common/window.py     set_dpi_awareness, WindowLocator, client_rect_screen, virtual_screen_rect
src/config/models.py     FeatureDef/StepDef/ClientProfile/KeyMap/ShipProfile/MapPack/
                         GlobalConfig/AppConfig 及全部配置分组
config.yaml              全局默认配置（字段与 models 一一对应）
profiles/cn360/**        客户端配置骨架（模板待实机标定）
```

关键接口（各任务按此实现/消费）：

```python
# 截屏（T3 实现，T4/T5 消费）
@dataclass(frozen=True)
class Frame:
    image: np.ndarray            # BGR, HxWx3, uint8
    timestamp: float
    window_rect: Rect            # 客户区屏幕矩形
class GdiCapture:
    def capture(self) -> Frame: ...      # 失败抛 CaptureUnavailable

# 模板匹配（T3）
class TemplateMatcher:
    def load_profile(self, profile: ClientProfile) -> None
    def match(self, feature_id: str, frame: Frame) -> MatchResult
    def match_many(self, feature_ids: Sequence[str], frame: Frame) -> dict[str, MatchResult]

# 地图识别（T4）
class MapIdentifier:
    def __init__(self, maps: Mapping[str, MapPack], cfg: PerceptionConfig): ...
    def identify(self, minimap_image: np.ndarray, expected_scale: float | None = None,
                 timestamp: float = 0.0) -> MapMatch | None

# 舰船定位（T4）：返回小地图像素坐标与朝向，世界坐标由 T4 engine 或调用方换算
class ShipLocator:
    def __init__(self, profile: ClientProfile, cfg: PerceptionConfig): ...
    def locate(self, minimap_image: np.ndarray, timestamp: float) -> ShipPosition | None
    def reset(self) -> None

# 死亡投票（T4）
class DeathDetector:
    def __init__(self, cfg: DeathConfig, battle_started_at: float): ...
    def update(self, votes: Mapping[str, bool], timestamp: float) -> DeathSignals
    def reset(self) -> None

# 感知引擎（Lead 集成）
class PerceptionEngine:
    def observe(self, state: State | None = None, now: float | None = None) -> Observation
    def match_feature(self, feature_id: str, now: float | None = None) -> MatchResult
    def detect_state(self, now: float | None = None) -> State | None
    def identify_map(self, now: float | None = None) -> MapMatch | None
    def locate_own_ship(self, now: float | None = None) -> ShipPosition | None
    def check_death(self, now: float | None = None) -> DeathSignals
    def set_calibration(self, calibration: BattleCalibration | None) -> None

# 输入（T2）
class InputBackend(Protocol):
    def send(self, actions: Sequence[Action]) -> None: ...
    def release_all(self) -> None: ...
class InputExecutor:
    def submit(self, actions: Sequence[Action]) -> None
    def release_all(self) -> None
    def stop(self, drain: bool = True) -> None

# 存储（T1）
class Database:
    def begin_session(self, profile_id: str, config_hash: str) -> int
    def end_session(self, session_id: int, stop_reason: str, total_battles: int) -> None
    def begin_battle(self, session_id: int, map_id: str | None) -> int
    def mark_queued(self, battle_id: int) -> None              # v2 补充：周转指标
    def mark_death(self, battle_id: int) -> None
    def mark_exit_requested(self, battle_id: int) -> None
    def mark_returned_to_port(self, battle_id: int) -> None    # v2 补充
    def save_random_profile(self, battle_id: int, profile: dict) -> int  # v2 补充
    def log_action(self, battle_id: int | None, action_type: str, detail: dict) -> None
    def log_state(self, session_id: int, battle_id: int | None, state: str,
                  confidence: float | None, detail: dict) -> None
    def end_battle(self, battle_id: int, result: str, exit_method: str,
                   exit_error: str | None = None) -> None
    def count_battles_on(self, day: date) -> int
```

## 任务拆分（写作用域互不重叠）

### Task 1 `config-storage` — 配置加载/校验、SQLite 存储、文本日志、截图管理、validate 工具

文件（新建）：`src/config/loader.py`、`src/config/validate.py`、`src/storage/database.py`、
`src/storage/event_log.py`、`src/storage/screenshots.py`、`src/tools/validate.py`、
`tests/test_config_loader.py`、`tests/test_config_validate.py`、`tests/test_mapdata.py`、
`tests/test_database.py`、`tests/test_screenshots.py`、`tests/test_event_log.py`、
`tests/fixtures/configs/**`

验收：
- 加载 `config.yaml` + `profiles/cn360`（模板缺失应得到 errors 而不是崩溃）；
- `load_app(root, profile_id)` 返回 `AppConfig`（含 config_hash、sources）；
- 地图包加载/校验覆盖详细设计 4.7 全部规则（错误/警告分级）；
- SQLite 表结构按详细设计 11.2 + 实现补充 v2（sessions.total_battles、battles.queued_at、
  battle_random_profiles.battle_id UNIQUE、去掉 battles.random_profile_id）；
- 事件线程批量写、失败降级 `logs/db_fallback.log`、可停止并 drain；
- 截图：临时战斗目录/异常诊断留存（配额清理）/会话启动清理遗留目录；
- `python -m src.tools.validate --root .` 打印问题清单，有 error 时退出码 2。

### Task 2 `execution` — SendInput 后端、输入线程、安全热键与看门狗

文件（新建）：`src/execution/winput.py`、`src/execution/queue.py`、`src/execution/safety.py`、
`tests/test_winput.py`、`tests/test_input_queue.py`、`tests/test_safety.py`

验收：
- INPUT/MOUSEINPUT/KEYBDINPUT 结构体与 64 位布局正确；虚拟屏幕归一化公式有测试；
- 按键名→虚拟键码/扫描码表覆盖 profile/keys.yaml 使用的名称与 f11/f12/escape/鼠标键；
- 绝对点击、相对移动、按键、鼠标按下/释放、wait 全部展开为 SendInput 调用；
- `SetCursorPos` 备选后端（配置 `input.backend=setcursor`）；
- 输入线程：串行、原子动作不被打断、最小间隔 150ms ±20% 抖动、队列上限丢弃最旧非关键动作、
  stop 清空并 release_all；提供 `submit(actions)`；
- 安全：F12 紧急停止、F11 恢复、心跳超时检测（GetAsyncKeyState 通过可注入 KeyStateSource 假实现测试）。

### Task 3 `perception-core` — GDI 截屏、模板匹配、特征匹配、状态特征表

文件（新建）：`src/perception/capture.py`、`src/perception/templates.py`、`src/perception/features.py`、
`src/perception/state_features.py`、`tests/test_capture_geometry.py`、`tests/test_templates.py`、
`tests/test_features.py`

验收：
- 截屏：客户区矩形→位图→numpy BGR，持久 DC 复用、尺寸变化重建、耗时统计、黑屏/空图检测、
  失败抛 CaptureUnavailable；纯几何/位图转换函数可离线测试（不依赖游戏窗口）；
- 模板：加载校验（存在、≥8×8、非纯色）、多尺度灰度预缓存、归一化相关系数匹配、
  归一化区域→像素裁剪、返回 MatchResult（center/rect/score/scale）；
- 特征匹配：按 profile.features 管理、`match`/`match_many`、支持运行时注册特征
  （ship_card、loadout_*，模板由调用方提供路径）、线程内复用；
- 状态特征表：默认表覆盖详细设计 5.4 的 PORT/QUEUEING/LOADING/BATTLE_ACTIVE/BATTLE_FINISHED/
  EXIT_MENU_OPEN/EXIT_CONFIRMING/POST_EXIT_INTERFACE，可被 profile.state_features 覆盖。

### Task 4 `perception-geometry` — 地图识别（ORB+镜像+仿射）、舰船定位、死亡投票

文件（新建）：`src/perception/map_identify.py`、`src/perception/ship_locate.py`、`src/perception/death.py`、
`tests/test_map_identify.py`、`tests/test_ship_locate.py`、`tests/test_death.py`、`tests/test_engine_contract.py`

验收：
- ORB：底图原始+水平镜像描述子；features.npz 缓存（底图 mtime/size 失效重建）；
  KNN+Lowe 0.75、estimateAffinePartial2D(RANSAC 3px)、内点≥15、重投影中位数≤2.5px、
  缩放/旋转接受条件（±50%、±10° 到 90° 整数倍）、多图候选领先 30% 告警；
- 镜像匹配时把仿射矩阵换算回原始底图坐标（`mirror_x_matrix`），并给出 mirrored/rotation/scale；
- 合成图测试：对 `tests.helpers.sample_background` 做已知相似变换后应识别成功；
- 舰船定位：24 角度模板 + HSV 颜色约束 + 时序预测/跳变重置 + lost 状态；
- 死亡投票：权重/阈值/稳定窗口可配，强信号 1.5s 直判，矛盾清零，开局 5s 内不判定；
- `test_engine_contract.py` 只验证 class 接口签名与依赖可被 fake 替换（engine 由 Lead 实现）。

### Task 5 `runtime` — 状态机辅助（步骤序列、会话控制）、导航、随机行为

文件（新建）：`src/state_machine/steps.py`、`src/state_machine/session.py`、
`src/navigation/route.py`、`src/navigation/navigator.py`、`src/behavior/random_profile.py`、
`src/behavior/scheduler.py`、`tests/test_steps.py`、`tests/test_session.py`、
`tests/test_route_select.py`、`tests/test_navigator.py`、`tests/test_behavior.py`

验收：
- 步骤执行器：key/click_feature/wait_feature/wait_ms/scroll，超时/重试/optional，
  失败抛 StepFailure；时间与等待通过注入 Clock/Sleeper，测试不 sleep；
- 会话控制：会话时长/场次/每日上限/允许时段/between_battle 随机等待/停止原因；
- 航线选择：按权重 + 本场 seed 可复现；weight=0 不选；无航线抛 RouteUnavailable；
- 导航：7.3~7.6 的点击/移动确认/到达/越点/卡住三级恢复/位置丢失降级全部实现，
  输出 Action 列表 + 事件回调（nav_*）；使用 FakeClock/合成 Observation 测试；
- 行为：本场随机档案、两个计时器 + 看门狗、正态采样+钳位、攻击二选一、消耗品上限后改开火、
  动作频率上限、战斗开始 10s 静默；输出 Action；seed 可复现。

### Task 6 `tools`（第二阶段，待 T1/T3/T4 完成后）— 标定、录制回放、环境检查、统计

文件：`src/tools/check_env.py`、`calibrate_map.py`、`calibrate_profile.py`、`calibrate_ship.py`、
`capture_marker.py`、`record.py`、`replay.py`、`stats.py` + tests。

### Task 7 Lead — 感知引擎、状态机主转移、主循环、集成测试、评审补充文档、README

文件：`src/perception/engine.py`、`src/state_machine/machine.py`、`src/main.py`、
`tests/test_engine.py`、`tests/test_state_machine.py`、`tests/test_integration_offline.py`、
`design/详细设计评审与实现补充.md`、`README.md`。

## 验证命令

```powershell
$py = "py -3.14"
& $py -m pytest tests -q
& $py -m ruff check src tests
& $py -m src.tools.validate --root .
& $py -m src.tools.check_env --help
& $py -m src.main --help
```

## 里程碑与提交

1. M0-a：T1/T2/T3/T4/T5 完成 + 测试通过（并行）。
2. M0-b：Lead 集成 engine/machine/main + 工具 T6 + 全量测试。
3. 提交由 Lead 统一执行（teammate 不运行 git 命令），每个里程碑一次 commit。
