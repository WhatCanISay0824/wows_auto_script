# wows_auto_script

《战舰世界》联合作战（PvE）模式的本地自动化研究脚本：通过**截屏 + 模拟键鼠**实现
自动港口选船（确认已选中）→ 加入战斗 → 排队 → 进战斗 → 小地图点击启用游戏自带
自动驾驶（不碰 WASD）→ 随机开火/消耗品 → 结算页优先“继续战斗！”直接下一局（无该按钮则回港）
→ 下一场。不再校验信号旗/加成卡。
不读取游戏内存、不修改客户端文件、不抓取网络数据，不使用 OCR。

- **项目综述（先看这个）**：[`docs/项目综述.md`](docs/项目综述.md) —— 功能、架构、当前状态、限制与路线图
- 仓库：<https://github.com/WhatCanISay0824/wows_auto_script>

> ⚠️ **风险提示**：自动化脚本违反游戏官方第三方软件政策，处罚对象是账号。
> 本项目仅用于本地自动化技术研究，请使用可弃小号、限制场次与时长、不要公开传播；
> 随机化输入只能降低客户端层面的挂机判定风险，**不能保证**规避服务器端识别。
> 后果由使用者自行承担。详见详细设计第 7、17 章。

## 实现依据

- 详细设计（权威）：`design/战舰世界联合作战挂机脚本详细设计文档.docx` v0.4，同目录有 PDF 预览；
- 初步设计：`design/战舰世界联合作战挂机脚本设计文档.docx` v0.1；
- **实现补充与裁决**：`design/详细设计评审与实现补充.md` —— 两份独立评审发现的 P0/P1
  问题及实现口径（退出流程、死亡判定、配置字段、ORB 尺度、GDI/INPUT 细节等），
  与详细设计冲突时以该文件为准；**第 10 节为 2026-10-31 ISS-001～010 修复口径**；
- 实机问题与修复记录：`docs/实机问题记录.md`（分析、修复实施记录、实机待验证项）；
- **GUI 详细设计（设计稿，未实施）**：`design/GUI界面设计文档.md` —— 运行服务契约、三页界面线框、
  船卡导入、标定工具集成、M0～M3 实施切片与验收清单；
- 实机标定记录：`docs/截图分类与提取结果.md`；
- 工作约定：`CLAUDE.md`。

## 当前状态

| 模块 | 状态 |
| --- | --- |
| 公共契约（类型/坐标/时钟/窗口） | ✅ 已实现并单测 |
| 配置加载与校验、地图包、SQLite 日志、截图管理、`validate` 工具 | ✅ M0 已实现并单测 |
| 账号档案加载、分账号统计、会话间轮换 | ✅ 已实现（用户扩展需求，见下文） |
| GDI 截屏、模板匹配、特征匹配 | ✅ M0 已实现并单测 |
| 界面模板标定（实机截图） | ✅ 已从 16 张实机截图提取 18 个界面模板（含 autopilot_indicator） + 自身标记 + 两艘船卡/血条/选中态；`validate` 0 error，`--dry-run` 退出码 0 |
| ORB 地图识别、舰船定位、死亡判定 | ✅ 已实现并单测；ISS-002 改为颜色无关血条归零证据门禁 |
| SendInput 输入、输入线程、安全热键 | ✅ 已实现并单测；ISS-004/005/009 增加统一输入门禁与独立切断 |
| 步骤序列、会话控制、导航、随机行为 | ✅ 已实现并单测；ISS-001 autopilot 改为 M 全屏地图点选 |
| 感知引擎、状态机主转移、主循环 | ✅ 已实现并单测（假数据集成）；ISS-002/006/007/010 已接入 |
| 单实例、异常清理、场次上下文 | ✅ 离线实现并单测（ISS-003/008/010） |
| 标定/录制回放/环境检查/统计工具 | ✅ M0-b 已实现；`check_env --offline` 实测 PASS=5/SKIP=11/FAIL=0（退出码 3） |
| GUI 详细设计 | ✅ 设计稿完成（`design/GUI界面设计文档.md`），未实施 |
| 实机验证（详细设计第 16 章 38 项 + ISS 修复回归） | ⏳ 待实机回归 |

**当前处于实机联调阶段（S2）**：离线测试 **754 项全过**、`validate` 0 error / 4 warning、
`main --dry-run` 退出码 0；实机已验证港口识别、按船名选船（铁公爵，selected.png/右侧船名证据）、
加入战斗/排队、自动进入战斗后的战斗 HUD 识别、F12/F11 安全键。2026-10-31 已按
[`docs/实机问题记录.md`](docs/实机问题记录.md) 逐项修复 ISS-001～ISS-010（**离线实现完成，
实机回归未完成**）：autopilot 改为 M 全屏地图点选、血条归零证据门禁、单实例锁、输入门禁、
暂停恢复、选船确认、异常清理、场次关联；GUI 详细设计已完成。实机回归项详见
[`docs/项目综述.md`](docs/项目综述.md) 第 4 节。

**实机问题与修复记录**：
[`docs/实机问题记录.md`](docs/实机问题记录.md) —— 问题分析、修复实施记录、实机待验证清单。

## 克隆

```powershell
git clone https://github.com/WhatCanISay0824/wows_auto_script.git
cd wows_auto_script
```

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

- 开局**按 `M` 打开全屏战术地图，在地图上点选一个目标点，再按 `M` 关闭地图**，让游戏自带
  自动驾驶把船开过去；之后脚本**不碰 WASD**，也不识别是哪张地图（2026-10-30 实机确认：
  直接在战斗画面右下角小地图上点击不会设置航向，因此旧路径仅作失败降级保留）；
- 目标点 = 我方出生点关于地图中心的**对称区域**，且**横坐标固定到中线**：
  `target = (0.5, 1 - own_y)`（归一化到地图内容区）。`own_y` 取开局时小地图上自身舰船
  标记的位置，因此需要标定小地图区域、自身标记模板与全屏地图内容区矩形；
- 点选后用自身标记位移或船速验证航向是否生效；失败会重试，仍失败降级为旧小地图路径并记录
  warning；地图打开到验证结束前会暂停随机行为，避免误触其它战斗输入；
- 脚本按 60～120 秒的随机间隔重发同一航点，避免自动航行到点后停船或卡住不恢复；
- 若自身标记识别不到（模板缺失/掉帧），回退到地图中心并记录警告；
  某张地图上船持续卡住时，可在 `config.yaml` 调整 `navigation.autopilot_target=center` 或
  `autopilot_offset` 微调目标点。

配置（`config.yaml`）：

```yaml
navigation:
  mode: autopilot             # autopilot（默认，无需地图包）| waypoints（旧模式，需地图标定）
  surface: full_map           # full_map（默认：M 开图点选）| minimap（旧交互，仅降级/兼容）
  full_map_rect: [0.2376, 0.0698, 0.7622, 0.8752]  # 全屏地图内容区（实机截图 211242/211253 标定）
  full_map_open_wait_s: 1.0   # 按 M 后等待地图界面就绪
  full_map_close_wait_s: 0.4  # 点选后等待点击生效再按 M 关图
  full_map_verify_s: 8.0      # 关图后验证航向（位移/船速）窗口
  full_map_retry: 2           # 验证失败重试次数，仍失败降级 minimap
  autopilot_target: symmetric # 对称敌区（默认）| center（地图中心）
  autopilot_reclick_s: [60, 120]
  autopilot_offset: [0.0, 0.0]
```

> **待实机验证**：M 开/关地图时序、全屏地图是否整图居中显示（截图为透视梯形，按外接矩形换算
> 有约 2% 纵向残差）、点选后是否真的设置航向。未验证前不把 ISS-001 记为“已解决”。

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
> 每艘**新船**首次使用前需要做三类标定：①船卡模板 `card.png`（在轮播条找船）；
> ②选中态模板 `selected.png`（卡片“已选中”高亮状态）；③右侧详情面板船名模板 `selected_name.png`
> （指挥官上方的当前选中船名，与卡片背景/高亮无关，比②更稳）。②③任一命中即确认“已选中该船”，
> 不能把“卡片可见”当作已选中（ISS-007；2026-10-31 实机补充：selected.png 曾因背景变化只到 0.815）。
> 当前已有两艘可直接用：`profiles/cn360/ships/leningrad/`（列宁格勒）、
> `profiles/cn360/ships/iron_duke/`（铁公爵）。
> 以新增“某新船”为例：
>
> ```powershell
> # 1) 在港口截图（底部轮播条里能看到该船卡片，并把它选中）
> # 2) 生成新船档案（GUI 里确认显示名，框选卡片：只框船体剪影+船名，避开 +50%/等级徽章与选中边框）
> .\wows-tool.cmd calibrate_ship --root . --ship-id <新船id> --display-name <船名> --screenshot "screenshots\<截图>.png"
> # 2b) 点“框选已选中状态的卡片（selected.png）”，在选中该船的同一张截图里框选一次
> # 2c) 点“框选右侧详情面板船名（selected_name.png）”，框选右侧面板指挥官上方的船名文字
> # 3) 之后启动时直接输入船名，脚本自动找船、点击并确认已选中
> .\wows.cmd --ship-name <船名>
> ```
>
> 模板要求：`selected.png` 必须包含该船的**身份特征（船名/船体）与选中高亮/边框**，不能只截通用边框；
> `selected_name.png` 要包含船名文字与固定背景，换船后文字不同即可区分（建议再选中另一艘船验证不命中）。
> 已有两艘船的三类模板均已由实机截图补齐；新船只框 `card.png` 也能保存
> （空 `loadout_slots` 同样允许保存），但缺少两类“已选中”证据时运行会在选船阶段保守暂停并提示补标定。
>
> 可选：把该船战斗画面左下角血条截图裁成 `ships/<新船id>/hp_bar.png`（`hp_bar_template: hp_bar.png`），
> 用于血条区域有效性校验；不提供时无法确认“血条归零”，死亡判定只会等待死亡提示/结算界面兜底。

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

## GUI（设计稿，未实施）

命令行脚本将升级为 Windows 本地控制台（运行页 / 日志页 / 标定与设置页）。详细设计见
[`design/GUI界面设计文档.md`](design/GUI界面设计文档.md)：包括从 `src/main.py` 抽取
`src/runtime/service.py` 的契约、Tkinter+ttk 技术选型、线程/消息模型、船名输入与首次船卡导入、
标定工具集成、账号轮换等待态、M0～M3 实施切片与验收清单。当前**只有设计，尚未实现**；
`ISS-001～010` 修复是 GUI 的硬前置，未修复/未验证前文档中已用 `CapabilityFlags` 标注“不承诺”。

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
- **F12 紧急停止**（释放所有按键/鼠标），任何异常先按 F12；F12 与心跳超时会在安全监控线程内
  立即关闭输入门禁并请求输入线程释放键鼠，不等待主循环下一次同步；
- **单实例**：同一 `data/` 目录只允许一个脚本实例运行（Windows 命名互斥量 + `data/wows_script.lock`）；
  已有实例时直接退出并打印持有者 PID，退出码 **3**，不会启动游戏控制；命令行与 GUI 共用同一把锁。
  确认无实例但提示占用时，检查任务管理器并结束残留进程（**结束进程树**）：
  `taskkill /PID <pid> /T /F`——不要只结束 `py` 启动器父进程，否则 Python 子进程会残留并继续控制游戏；
- 启动后日志会依次出现 `INIT -> PORT`、已选中船校验、开局排队等记录；**2026-10-31 起不再做装备页/信号旗/加成卡校验**；
- **截屏方式**：默认 `perception.capture_method: auto`。无边框全屏且游戏在前台时优先用桌面 DC 截屏，
  避免部分客户端“窗口 BitBlt/PrintWindow 返回冻结旧帧”导致页面切换/战斗画面识别不到
  （2026-10-31 实机问题 ISS-011）；桌面截屏失败会自动回退旧路径。远程桌面/特殊环境可改为
  `screen`（强制桌面截屏）或 `window`（旧行为）；
- `--max-battles 1` 表示跑 1 场后自动停止；日志里的 `max_battles: 20` 是配置里的会话安全上限，不冲突。

## 运行前提与边界

- 单机、单账号、单窗口、固定一艘船；
- **不再校验信号旗/经济加成卡**（2026-10-31 用户确认取消）；装备界面不读取、不点击、不修改；
- **不售卖、不出售、不消耗任何船只或装备**；
- 船只列表用鼠标滚轮滚动；死亡后提前退出时港口出现的“原战局战报”弹窗不处理（忽略/ESC）；
- 截图只用于当前场次诊断：临时截图战斗结束后删除，异常诊断截图按配额保留；
- 日志/截图/数据库只保存在本机，不含账号、密码、令牌；
- 完整风险说明见详细设计第 7、17 章，实现补充文档第 6 节列出了每项实机假设
  “不成立时的降级/停止条件”。
