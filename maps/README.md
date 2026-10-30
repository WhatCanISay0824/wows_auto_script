# 地图包目录说明（详细设计第 4 章）

当前为空。M0 阶段按以下步骤生成第一个地图包：

1. 从 https://wowsdb.com/zh-CN/database/maps 下载目标地图底图（核对分辨率、
   边框、图例与站点使用条款，待验证项 V28），保存为 `maps/<map_id>/background.png`；
2. 用 `python -m src.tools.calibrate_map --map-dir maps/<map_id>` 打开标定工具，
   标出 image_bounds、填写 world_bounds / size_meters、选择底图方向；
3. 在底图上添加出生点（世界坐标 + 朝向）并绘制航线；
4. 保存后运行 `python -m src.tools.validate` 校验地图包。

地图包结构：

```
maps/<map_id>/
├── background.png   # 外部地图底图（来自地图站）
├── map.yaml         # 地图元数据与图像坐标标定
├── spawns.yaml      # 出生点
├── routes.yaml      # 航线
└── features.npz     # 可选，ORB 特征缓存（缺失时启动阶段生成）
```

注意：
- `map_id` 使用英文小写与下划线，必须与目录名一致；
- `image_orientation` 表示底图相对世界坐标的方向（normal / rot90 / rot180 / rot270 /
  mirror_h / mirror_v / mirror_rot90 / mirror_rot270），标定工具提供方向预览；
- 出生点与航点一律使用世界坐标（米），不要写屏幕/图像像素坐标；
- 航线需要人工确认落在水域内并避开岛屿，程序不做地形判断。
