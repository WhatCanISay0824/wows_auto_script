"""Tkinter GUI（M1：运行/性能/日志/设置）。

本包只负责控件、布局与中文文案：

- 通过 :class:`src.gui.app.ServiceProtocol` 与运行服务解耦，只导入
  ``src.runtime.messages`` 中的只读数据类型；
- 不导入 ``winput`` / ``ctypes`` / ``InputExecutor`` / ``StateMachine``，
  不发送游戏键鼠输入，不写 ``config.yaml``；
- 所有 Tk 调用都发生在主线程（``after()`` 轮询，无额外线程）。

入口：``py -3.14 -m src.gui.main``。
"""
