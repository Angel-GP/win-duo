"""系统托盘图标与菜单。

程序默认就活在这里 —— 不开玻璃层、不占摄像头, 只在托盘留一个图标。
"""
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import wdlog
from angles.hub import LABELS

from . import autostart
from .widgets import make_icon

SOURCE_ORDER = ("camera", "serial", "manual")


class DuoTray(QSystemTrayIcon):
    def __init__(self, controller, on_open_panel, parent=None):
        super().__init__(make_icon(), parent)
        self.controller = controller
        self.on_open_panel = on_open_panel
        self.setToolTip("win-duo 悬浮玻璃")
        self._build_menu()
        controller.glassChanged.connect(self.sync)
        controller.notified.connect(self.notify)
        self.activated.connect(self._on_activated)
        self.sync(controller.glass_on)

    # ------------------------------------------------------------ 菜单
    def _build_menu(self):
        menu = QMenu()

        self.act_toggle = QAction("开启玻璃层", self)
        self.act_toggle.triggered.connect(self.controller.toggle_glass)
        menu.addAction(self.act_toggle)

        menu.addSeparator()
        act_panel = QAction("设置...", self)
        act_panel.triggered.connect(lambda: self.on_open_panel())
        menu.addAction(act_panel)

        # 角度源
        src_menu = menu.addMenu("角度源")
        self.src_group = QActionGroup(self)
        self.src_group.setExclusive(True)
        self.src_actions = {}
        for name in SOURCE_ORDER:
            act = QAction(LABELS.get(name, name), self)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked, n=name: self._pick_source(n))
            self.src_group.addAction(act)
            src_menu.addAction(act)
            self.src_actions[name] = act

        menu.addSeparator()
        self.act_auto = QAction("开机自启", self)
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(autostart.is_enabled())
        self.act_auto.triggered.connect(self._toggle_autostart)
        if not autostart.available():
            self.act_auto.setEnabled(False)
        menu.addAction(self.act_auto)

        menu.addSeparator()
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        # 菜单是普通窗口, 会被全屏置顶的玻璃层盖住 —— 弹出期间先把玻璃层收起来,
        # 否则用户看不见菜单, 只能盲点, 感觉像"点托盘没反应"。
        menu.aboutToShow.connect(lambda: self.controller.suppress_glass(True))
        menu.aboutToHide.connect(lambda: self.controller.suppress_glass(False))

        self.setContextMenu(menu)

    # ------------------------------------------------------------ 交互
    def _pick_source(self, name):
        wdlog.log.debug("用户操作: 托盘切换角度源 -> %s" % name, tag="tray")
        self.controller.set_source(name)
        self.sync(self.controller.glass_on)

    def _toggle_autostart(self, checked):
        wdlog.log.debug("用户操作: 托盘切换开机自启 -> %s" % checked, tag="tray")
        try:
            autostart.set_enabled(checked)
        except Exception as exc:  # noqa: BLE001
            self.notify("设置开机自启失败: %s" % exc)
        # 以注册表真实状态为准, 别信复选框
        self.act_auto.setChecked(autostart.is_enabled())
        self.notify("开机自启已%s" % ("开启" if autostart.is_enabled() else "关闭"))

    def _on_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            wdlog.log.debug("用户操作: 托盘图标激活 (打开设置)", tag="tray")
            self.on_open_panel()

    def _quit(self):
        wdlog.log.debug("用户操作: 托盘退出", tag="tray")
        self.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------ 状态同步
    def sync(self, glass_on):
        wdlog.log.debug("托盘状态同步: 玻璃层 %s" % ("开" if glass_on else "关"), tag="tray")
        self.act_toggle.setText("关闭玻璃层" if glass_on else "开启玻璃层")
        self.act_auto.setChecked(autostart.is_enabled())
        active = self.controller.hub.active_name()
        for name, act in self.src_actions.items():
            act.setChecked(name == active)
        self.setToolTip("win-duo 悬浮玻璃 —— %s"
                        % ("玻璃层开启中" if glass_on else "待机 (在托盘打开)"))

    def notify(self, message, msec=4000):
        try:
            self.showMessage("win-duo", message, make_icon(), msec)
        except Exception:  # noqa: BLE001
            wdlog.log.debug("托盘气泡: %s" % message, tag="tray")
