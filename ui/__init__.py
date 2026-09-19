"""Qt 界面层: 托盘、设置窗口、控制器、开机自启、显示器枚举。

main.py 只负责装配这里的东西; 具体逻辑都在 controller 里。
界面控件统一从 widgets.py 取 (Fluent / 回退自动切换)。
"""
from .controller import AppController
from .panel import SettingsPanel
from .tray import DuoTray
from .widgets import FLUENT, apply_theme, make_icon

__all__ = ["AppController", "SettingsPanel", "DuoTray", "make_icon",
           "apply_theme", "FLUENT"]
