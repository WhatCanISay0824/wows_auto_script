# 标定工具与模板目录说明（M0 阶段）

本目录结构由 `python -m src.tools.calibrate_profile` / `calibrate_ship` / `capture_marker`
等工具生成或更新，模板图片需要从实机截图截取，**不要把网上图片当作界面模板**。

需要标定并生成的文件：

- `features/*.png`：profile.yaml 中 `features` 引用的界面模板（港口、排队、加载、
  战斗 HUD、死亡提示、退出菜单、确认框、回港弹窗等），至少 22 张；
- `marker/marker.png`：小地图上自身舰船标记，工具会旋转生成 24 个角度模板；
- `ships/<ship_id>/card.png`：港口船只卡片；
- `ships/<ship_id>/loadout/*.png`：空信号旗槽、空经济加成槽。

标定流程见详细设计 12.3 节。模板采集要求：包含周围固定背景、尺寸不小于 8×8、
不要纯色、避免包含动态文字（数字、时间）。
