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
| 配置加载与校验、地图包、SQLite 日志、截图管理、`validate` 工具 | 🚧 M0 实现中 |
| GDI 截屏、模板匹配、特征匹配 | 🚧 M0 实现中 |
| ORB 地图识别、舰船定位、死亡投票 | 🚧 M0 实现中 |
| SendInput 输入、输入线程、安全热键 | 🚧 M0 实现中 |
| 步骤序列、会话控制、导航、随机行为 | 🚧 M0 实现中 |
| 感知引擎、状态机主转移、主循环 | ⏳ 集成中 |
| 标定/录制回放/环境检查/统计工具 | ⏳ M0-b |
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
# 校验配置、客户端配置、地图包（当前仓库因待标定会报 error，退出码 2）
python -m src.tools.validate --root .

# 运行单元测试（离线，不需要游戏）
python -m pytest tests -q

# 代码静态检查
python -m ruff check src tests

# 运行脚本（需要游戏已安装并完成标定）
python -m src.main --profile cn360 --ship <ship_id>

# 环境检查 / 标定 / 录制回放（M0-b 完成后可用）
python -m src.tools.check_env --profile cn360
python -m src.tools.calibrate_map --map-dir maps/<map_id>
python -m src.tools.calibrate_profile --profile cn360
```

## 运行前提与边界

- 单机、单账号、单窗口、固定一艘船；不使用信号旗与经济加成卡（每场开始前校验）；
- 截图只用于当前场次诊断：临时截图战斗结束后删除，异常诊断截图按配额保留；
- 日志/截图/数据库只保存在本机，不含账号、密码、令牌；
- 完整风险说明见详细设计第 7、17 章，实现补充文档第 6 节列出了每项实机假设
  “不成立时的降级/停止条件”。
