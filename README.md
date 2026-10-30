# wows_script

《战舰世界》联合作战（PvE）模式的本地自动化研究脚本：通过**截屏 + 模拟键鼠**实现
自动开局、识别地图与出生点、按预设航线自动航行、随机视角/开火/消耗品输入、死亡后
自动退出并进入下一场。不读取游戏内存、不修改客户端文件、不抓取网络数据。

> ⚠️ **风险提示**：自动化脚本违反游戏官方第三方软件政策，处罚对象是账号。
> 本项目仅用于本地自动化技术研究，请使用可弃小号、限制场次与时长、不要公开传播；
> 随机化输入只能降低客户端层面的挂机判定风险，**不能保证**规避服务器端识别。
> 后果由使用者自行承担。详见详细设计第 7、17 章。

## 实现依据

- 详细设计（权威）：`design/战舰世界联合作战挂机脚本详细设计文档.docx` v0.4，同目录有 PDF 预览；
- 初步设计：`design/战舰世界联合作战挂机脚本设计文档.docx` v0.1；
- **实现补充与裁决**：`design/详细设计评审与实现补充.md` —— 两份独立评审发现的 P0/P1
  问题及实现口径（退出流程、死亡判定、配置字段、ORB 尺度、GDI/INPUT 细节等），
  与详细设计冲突时以该文件为准；
- 工作约定：`CLAUDE.md`。

## 当前状态

| 模块 | 状态 |
| --- | --- |
| 公共契约（类型/坐标/时钟/窗口） | ✅ 已实现并单测 |
| 配置加载与校验、地图包、SQLite 日志、截图管理、`validate` 工具 | ✅ M0 已实现并单测 |
| 账号档案加载、分账号统计、会话间轮换 | ✅ 已实现（用户扩展需求，见下文） |
| GDI 截屏、模板匹配、特征匹配 | ✅ M0 已实现并单测 |
| 界面模板标定（实机截图） | ✅ 已从 16 张实机截图提取 17 个界面模板 + 自身标记 + 两艘船卡（列宁格勒/铁公爵）；`validate` 0 error，`--dry-run` 退出码 0 |
| ORB 地图识别、舰船定位、死亡投票 | ✅ M0 已实现并单测（合成图验证） |
| SendInput 输入、输入线程、安全热键 | ✅ M0 已实现并单测 |
| 步骤序列、会话控制、导航、随机行为 | ✅ M0 已实现并单测 |
| 感知引擎、状态机主转移、主循环 | ✅ 已实现并单测（假数据集成） |
| 简化导航 autopilot（不识别地图/不标路线） | ✅ 已实现并单测（对称敌区目标） |
| 标定/录制回放/环境检查/统计工具 | ✅ M0-b 已实现；`check_env --offline` 实测 PASS=5/SKIP=11/FAIL=0（退出码 3） |
| 实机验证（详细设计第 16 章 38 项） | ⏳ 游戏安装后进行 |

**未完成实机验证前，不宣称任何实机验收通过。** 当前仓库中的 `profiles/cn360/` 是
待标定骨架（模板图片、船只卡片、地图包均未生成），`python -m src.tools.validate`
会报告“模板文件不存在”等错误，这是预期的。

## 环境要求

- Windows 11（脚本依赖 Win32 GDI/SendInput，其他平台不可用）；
- Python 3.14.x（开发环境 3.14.5）；
- `numpy>=2.5`、`opencv-python>=5.0`、`PyYAML>=6.0`（见 `requirements.txt`，开发另需 pytest、ruff）；
- 游戏以 **2560×1600 无边框窗口**运行、UI 缩放固定、小地图设置固定（整图 + 北向）、
  游戏窗口保持前台；系统不锁屏、不休眠。

```powershell
# 安装依赖（假设已安装 Python 3.14）
python -m pip install -r requirements.txt
python -m pip install pytest ruff
```

> **注意（本机）**：有的 Windows 环境里 `python` 是应用商店占位符，执行无输出或提示找不到；
> 本项目统一用 `py -3.14`，或仓库根目录的快捷脚本：
>
> ```powershell
> .\wows.cmd --dry-run
> .\wows.cmd --ship-name 铁公爵
> .\wows-tool.cmd validate --root .
> .\wows-tool.cmd calibrate_ship --root . --ship-id iron_duke --display-name 铁公爵 --screenshot "screenshots\铁公爵港口.png"
> ```
>
> 等价完整命令为 `py -3.14 -m src.main ...`、`py -3.14 -m src.tools.<工具> ...`。
> 如果中文输出乱码，先在 PowerShell/cmd 执行 `chcp 65001`（快捷脚本已自动切换代码页）。

## 导航模式：简化 autopilot（当前默认）

使用者确认的简化方案：**不做地图识别、不标定航线**。

- 开局在游戏小地图上点一个目标点，让游戏自带自动驾驶把船开过去；之后脚本**不碰 WASD**，
  也不识别是哪张地图；
- 目标点 = 我方出生点关于地图中心的**对称区域**，且**横坐标固定到中线**：
  `target = (0.5, 1 - own_y)`（归一化到小地图内容区）。`own_y` 取开局时小地图上自身舰船
  标记的位置，因此只需要在 `profiles/<client>/` 标定小地图区域与自身标记模板；
- 脚本按 60～120 秒的随机间隔重发同一航点，避免自动航行到点后停船或卡住不恢复；
- 若自身标记识别不到（模板缺失/掉帧），回退到小地图中心并记录警告；
  某张地图上船持续卡住时，可在 `config.yaml` 调整 `navigation.autopilot_target=center` 或
  `autopilot_offset` 微调目标点。

配置（`config.yaml`）：

```yaml
navigation:
  mode: autopilot             # autopilot（默认，无需地图包）| waypoints（旧模式，需地图标定）
  autopilot_target: symmetric # 对称敌区（默认）| center（小地图中心）
  autopilot_reclick_s: [60, 120]
  autopilot_offset: [0.0, 0.0]
```

原“地图包 + 出生点 + 航线 + ORB 地图识别”的 waypoints 模式代码保留在
`src/navigation/navigator.py`、`src/perception/map_identify.py`、`maps/` 中，配置
`navigation.mode: waypoints` 才启用；autopilot 模式下 `maps/` 可以为空、`validate` 不再要求地图包。

## 账号选择与轮换（用户扩展需求）

详细设计原范围是“单机单账号、不做账号切换/代登录”。应使用者要求，增加了**本地账号档案 +
每账号独立配置 + 会话间轮换**，安全边界不变：

- `accounts.yaml` 只保存本地别名（alias）与配置覆盖，**禁止保存真实账号、密码、令牌、验证码**；
  加载器发现凭据字段会直接报错拒绝；
- 不自动登录、不代登录：真实登录与切号由使用者在游戏客户端手动完成；
- 同一时间只运行一个账号、一个游戏窗口；
- 会话/场次/每日上限按别名隔离统计（数据库 `account_alias` 列）。

```powershell
python -m src.main --account alt1     # 只运行指定账号档案
python -m src.main --rotate           # 按 accounts.yaml 轮换启用账号
python -m src.main --list-ships       # 列出客户端配置下的船只档案（ship_id 与显示名）
python -m src.main --ship-name 列宁格勒  # 按船名选择固定船只（脚本用鼠标在港口底部轮播条里找并选中）
```

> 每次托管的船可以不同：用 `--ship-name` 输入船名即可，脚本会在底部船只轮播条里滚轮查找并点击选中。
> 每艘**新船**首次使用前需要做一次约 10 秒的标定（生成该船卡片）。当前已有两艘可直接用：
> `profiles/cn360/ships/leningrad/`（列宁格勒）、`profiles/cn360/ships/iron_duke/`（铁公爵）。
> 以新增“某新船”为例：
>
> ```powershell
> # 1) 在港口截图（底部轮播条里能看到该船卡片，最好选中它）
> # 2) 生成新船档案（GUI 里确认显示名，框选卡片：只框船体剪影+船名，避开 +50%/等级徽章与选中边框）
> .\wows-tool.cmd calibrate_ship --root . --ship-id <新船id> --display-name <船名> --screenshot "screenshots\<截图>.png"
> # 3) 之后启动时直接输入船名，脚本自动找船并选中
> .\wows.cmd --ship-name <船名>
> ```
>
> 可选：把该船战斗画面左下角血条截图裁成 `ships/<新船id>/hp_bar.png`（`hp_bar_template: hp_bar.png`），
> 用于“血条消失”死亡信号；不提供也能跑，死亡判定改用死亡提示/结算界面兜底。

轮换流程：当前账号达到 `max_battles_per_session`（或每日上限/允许时段）后，脚本停止会话并在
控制台提示手动切换账号；完成客户端切号、回到港口后按回车，脚本以新账号的客户端配置/船只/
场次上限开始新会话。详见 `design/详细设计评审与实现补充.md` 第 8 节。

## 目录结构

```
config.yaml                全局运行配置（默认值对应 2560×1600 国服）
profiles/cn360/            客户端配置：窗口、界面特征、按键、退出步骤、船只
maps/<map_id>/             地图包：底图、元数据、出生点、航线（当前为空，待标定）
src/common/                公共类型、坐标变换、时钟、Win32 窗口工具
src/config/                配置/地图包加载与校验
src/perception/            截屏、模板/ORB 识别、舰船定位、死亡判定
src/execution/             SendInput、输入线程、安全热键
src/state_machine/         状态机、步骤序列、会话控制
src/navigation/            航线选择与导航
src/behavior/              随机行为调度
src/storage/               SQLite 日志、文本日志、截图管理
src/tools/                 validate / check_env / 标定 / 录制回放 / 统计
tests/                     单元测试（离线，不需要游戏）
data/ logs/ screenshots/   运行产物（不入库）
docs/plans/                实现计划
design/                    设计文档与评审补充
```

## 常用命令

```powershell
# 校验配置、客户端配置、地图包（当前仓库已 0 error，仅剩有意保留的 warning）
.\wows-tool.cmd validate --root .

# 运行单元测试（离线，不需要游戏）
py -3.14 -m pytest tests -q

# 代码静态检查
py -3.14 -m ruff check src tests

# 运行脚本（需要游戏已安装并完成标定）
.\wows.cmd --ship-name <船只显示名> --max-battles 1 --log-level DEBUG

# 环境检查 / 标定 / 录制回放（M0-b 完成后可用）
.\wows-tool.cmd check_env --profile cn360
.\wows-tool.cmd calibrate_map --map-dir maps/<map_id>
.\wows-tool.cmd calibrate_profile --profile cn360
```

运行说明：
- 启动时脚本会自动把**游戏窗口切到前台**；运行中切走会按安全策略暂停，需点回游戏后按 **F11** 恢复；
- **F12 紧急停止**（释放所有按键/鼠标），任何异常先按 F12；
- 启动后日志会依次出现 `INIT -> PORT`、装备页信号旗校验、船只卡片校验、开局排队等记录；
- `--max-battles 1` 表示跑 1 场后自动停止；日志里的 `max_battles: 20` 是配置里的会话安全上限，不冲突。

## 运行前提与边界

- 单机、单账号、单窗口、固定一艘船；
- **装备校验只要求信号旗数量为 0**；经济加成卡保持原样不动（不主动使用、不摘除，原有就让
  它自然耗尽）；装备界面左侧配件/升级不读取、不点击、不修改；
- **不售卖、不出售、不消耗任何船只或装备**；
- 船只列表用鼠标滚轮滚动；死亡后提前退出时港口出现的“原战局战报”弹窗不处理（忽略/ESC）；
- 截图只用于当前场次诊断：临时截图战斗结束后删除，异常诊断截图按配额保留；
- 日志/截图/数据库只保存在本机，不含账号、密码、令牌；
- 完整风险说明见详细设计第 7、17 章，实现补充文档第 6 节列出了每项实机假设
  “不成立时的降级/停止条件”。
