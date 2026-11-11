# 无敌人期间的平滑视角动作实施方案

## 目标和范围

用户要求无敌人时五秒内随机轻微移动视角，有敌人时沿用当前交战行为。当前定向模式只调用 CombatController，无目标时保持不动。实现新增的无目标视角动作，不推断游戏的挂机判定规则，也不把离线动作测试视为规避检测证明。

保留现有全部未提交修改和用户船包。本轮不提交、不推送、不删除文件，不触发真实游戏键鼠。Codex 决定设计、边界和验收，通过 Broker 的 deepseek-flash 路由执行限定范围的编码和测试，Codex 独立复核。仅允许派发本方案列出的源码、测试和非敏感配置，不读取运行日志、账号档案、凭据或整段对话。

## 方案选择

复用旧随机视角会继承 3～15 秒间隔、长时间凝视和整组动作入队，不符合五秒内开始及遇敌取消的要求。采用独立的 IdleCameraController，每轮只生成当前所需的一小步，不生成 WAIT 队列。遇敌后尚未生成的剩余运动直接取消。

当前交战每分钟额度是 40 次，平滑移动拆成多步后会耗尽该额度。无目标视角单独限流，默认每分钟最多 180 个移动输入，不计入交战的 40 次额度；原有交战额度、开火和转向逻辑保持不变。达到额度、门禁关闭、画面不可用或主循环停顿时，允许停止视角动作，不能为满足五秒而突破这些约束。

## 数据与配置

- `CombatObservation` 在末尾增加 `enemy_candidates: int = 0`，保持原有位置参数兼容。`MinimapCombatObserver` 返回当前帧通过形状筛选的候选数量，未完成两帧确认的候选也立即阻止无目标视角动作。确认敌人、镜头估计和 CombatController 实现均不改动。
- BehaviorConfig 增加四项，loader 必须完整加载，validate 检查有限值、有序区间和范围：`idle_camera_interval_s=(3.0,4.5)`，范围 [2.0,4.5]；`idle_camera_move_px=(24,48)`，正整数区间且上限不超过 80；`idle_camera_duration_s=(0.5,0.7)`，范围 [0.3,1.0]；`idle_camera_actions_per_minute_max=180`，正整数。项目 config.yaml 写出相同值并说明单位为鼠标计数。

## 控制与衔接

新文件 `src/behavior/idle_camera.py` 的 IdleCameraController 接口：构造接收 BehaviorConfig；`reset(seed=0)` 重置独立随机流；`cancel()` 清除计时和运动但不重抽随机种子；`eligible(frame,now)->bool` 判断可靠的无敌人画面；`tick(frame,now)->tuple[list[Action],dict]` 每次最多返回一个 MOVE_REL。

eligible 要求 combat 时间在 [now-max_input_frame_age_s,now]，己方位置存在且数值有限，enemies 为空、enemy_candidates 为零。无画面或无己方标记不是“没有敌人”。无目标运动不依赖绝对镜头方向，因此镜头扇区缺失不阻止该分支；有敌人时仍使用原有镜头方向门槛。前台、弹窗、开图、死亡、结算、暂停和停止继续由现有调度门禁及状态机阻止输入。

首次获得可靠无敌人画面时，按均匀分布抽取下一次运动间隔。每次运动的开始时刻之间间隔 3～4.5 秒，不从运动结束时再加间隔；正常 10 Hz 调度下五秒内开始有非零移动。每次抽取左右方向、总幅度和持续时间，垂直分量为零，避免改变炮口俯仰。用 smoothstep `3*t*t-2*t*t*t` 计算累计位移，每轮仅发送本轮累计值相对已发值的差；持续时间结束时补齐余量。

等待期间的慢循环保留既定开始时间。相邻控制器 tick 间隔超过 0.3 秒时，只取消正在执行的剩余运动，保留下一次开始时间，不补发大步。每个 MOVE_REL 的有效期取 `min(frame.timestamp+max_input_frame_age_s, now+0.15)`。不生成阻塞等待或批量未来动作。当前补充方案见 `2026-11-03-behavior-start-and-switch.md`。

项目输入线程的最小动作间隔为 150 毫秒，并有 20% 抖动；直接按 10 Hz 生成移动会在输入队列中积压并过期。无目标控制器开始时发送方向一致的 1 个鼠标计数，随后每隔至少 0.2 秒才发一小步，按真实经过时间计算累计曲线并扣除已发位移，单步最多 24 个计数。该节奏与当前输入配置相容，不修改输入线程或有敌人时的动作限流。若用户主动加大输入间隔，短期动作可因过期而丢弃，不能补发长时间积压。

BehaviorScheduler 持有独立控制器和无目标输入时间队列。start、stop、resume、非战斗上下文及输入门禁关闭时清除无目标运动；敌方候选、确认敌人或不可靠画面也取消运动。现有 CombatController 与有目标分支保持原行为。无目标运动只在可靠无敌人且门禁允许时接管空闲返回分支；开局 quiet 时间内也允许该分支，quiet 对敌方交战的约束保持不变。导航外部活动不重置无目标计时。记录每次视角运动的幅度、持续时间和限流原因，避免每一小步重复输出日志。

## 文件职责和验收

编码范围：`src/common/types.py`、`src/perception/combat.py`、`src/behavior/idle_camera.py`、`src/behavior/scheduler.py`、`src/config/models.py`、`src/config/loader.py`、`src/config/validate.py`、`config.yaml`；测试范围为 `tests/test_idle_camera.py`、`tests/test_combat.py`、`tests/test_behavior.py`、`tests/test_config_loader.py`、`tests/test_config_validate.py`。不得改动 `src/behavior/combat.py`、状态机、执行后端、客户端资源、全局或项目规则、Git 历史和用户船包。

- [x] 用时间推进测试证明首次及连续三分钟内的运动开始间隔不超过五秒，间隔有变化；检查每次最多一个动作、水平累计幅度有界、速度先缓后快再缓、有效期不超过 0.15 秒、不生成 WAIT。
- [x] 覆盖敌人和候选在运动中出现后取消；无镜头仍允许无目标移动，无位置、缺失/过期/未来画面、失焦、弹窗、非战斗、暂停停止和恢复不沿用旧运动；主循环中断不补发大步。
- [x] 验证开局 quiet 内无敌人会移动、有敌人仍不交战；独立额度生效、不会消耗交战额度，继续通过已有定向转向、开火、新帧反馈及截图回放测试。
- [x] 配置加载与非法范围测试、相关测试、全套 pytest、Ruff、四套配置校验、git diff --check；核对 CombatController 与用户船包散列未改变。
- [x] Codex 审核补丁与验证证据，更新 README、CLAUDE.md、实现补充和本方案的实际验收记录。实机平滑程度、实际发炮和游戏系统判定明确列为未验证项。

## 实际验收记录

- Broker 路由为 `deepseek-flash`，provider 为 `deepseek`，model 为 `deepseek-flash`，权限为本工作区写入；派发仅包含任务契约、方案及授权源码/测试路径。Flash 在明确范围内完成增量，未改动交战控制器、状态机、执行后端和用户船包。
- Flash 的测试环境在创建 tmp_path 时遇到 Windows 访问权限错误。原始输出及说明保存在 `pytest_work/idle_camera_1103/worker-report.md`；没有改权限、删除目录或切换 provider。该报告不作为完整通过证据。
- Codex 独立执行相关六组测试：161 项通过，耗时 8.75 秒。全套测试：1303 项通过、1 项因缺少旧样本跳过，耗时 49.51 秒。Ruff、四套客户端配置校验和 `git diff --check` 通过；国服 4 个既有警告，直营服各 5 个既有警告。
- 三分钟控制器测试验证首次非零动作在五秒内、起动间隔有变化、累计位移和缓入缓出曲线相符、单步间隔至少 0.2 秒。生产 InputExecutor 加替身后端的时序测试中，三步实际执行、零步过期，生成时间与输入队列时钟一致。
- Codex 补充生产感知到调度及 FakeSendInput 的 12 秒回放：基于真实小地图移除红橙像素构造无敌人场景，不是真实采集的连续无敌人画面。10 Hz 决策期间产生 25 张模拟截图，首次移动为 4.6 秒，两次运动起动约为 4.4 秒和 8.9 秒，共收到 6 个相对水平移动事件，无绝对移动或开火。结果见 `pytest_work/idle_camera_1103/codex-image-replay.json`。
- `src/behavior/combat.py` SHA256 保持 `2587F033C7077359B2FBDC1547750E0205497A637B5E72FA0D8F3D75F230582F`；用户船包三个文件的散列均与派发前一致。没有暂存、提交或推送。
- 尚未验证真实游戏中的平滑程度、各地图下的识别稳定性及系统挂机判定。五秒内开始的结论适用于门禁开放、画面可靠、额度充足和正常调度；暂停、开图、感知失败及主循环停顿期间不保证该节奏。
