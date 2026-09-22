"""系统级全局热键。

为什么必须有: 玻璃层是**全屏置顶**且**穿透输入**的窗口。一旦它停在高浓度上:
  - 右键托盘弹出的菜单会被玻璃层盖住 —— 菜单其实弹出来了也能点, 但用户看不见,
    感觉就是"点托盘没反应";
  - 托盘模式用 pythonw 启动, 没有控制台, Esc 也没得按。
于是屏幕就"回不去了"。热键不依赖焦点、不依赖托盘、不依赖控制台, 是最后一条后路。

实现注意: **不要用 QWidget + 重写 nativeEvent 的方式**。那样在 `winId()` 触发
原生窗口创建时, Qt 会回调 `nativeEvent`, 实测直接以
`0xC000041D`(STATUS_FATAL_USER_CALLBACK_EXCEPTION) 崩掉整个进程, 连异常都抓不到。
改用 `QAbstractNativeEventFilter`: RegisterHotKey 时 hwnd 传 NULL, WM_HOTKEY 会
投递到线程消息队列, Qt 的事件派发器会把它交给 native event filter。
"""
import ctypes
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

import wdlog

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

#: 不能靠 ord() 得到的键
_NAMED_KEYS = {
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "tab": 0x09,
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}


def parse_hotkey(spec):
    """把 'ctrl+alt+shift+esc' 解析成 (modifiers, vk)。解析不了返回 (None, None)。"""
    if not spec:
        return None, None
    mods, vk = 0, None
    for part in str(spec).lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super"):
            mods |= MOD_WIN
        elif part in _NAMED_KEYS:
            vk = _NAMED_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            n = int(part[1:])
            if 1 <= n <= 24:
                vk = 0x70 + n - 1
    if vk is None or mods == 0:
        return None, None
    return mods, vk


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, _event_type, message):  # noqa: N802
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self._owner.dispatch(int(msg.wParam))
        except Exception:  # noqa: BLE001  绝不能从回调里抛出去
            pass
        return False, 0


class HotkeyManager(QObject):
    """注册/注销系统级热键。触发时发 triggered(name)。"""

    triggered = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._user32 = ctypes.windll.user32
        # **显式声明 argtypes/restype。** 不声明时 ctypes 按默认 int 传参,
        # 64 位下句柄/指针可能被截断 —— 项目里对 SetProcessWorkingSetSize
        # 正是因此踩过坑 (见 controller 的注释), 这里保持同样严谨。当前参数
        # 恰好是小整数与 NULL 所以能跑, 但那是巧合, 不该依赖。
        self._user32.RegisterHotKey.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        self._user32.RegisterHotKey.restype = ctypes.c_bool
        self._user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = ctypes.c_bool
        self._names = {}          # hotkey id -> name
        self._specs = {}          # name -> spec
        self._next_id = 0xD00D
        self._filter = _HotkeyFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)

    def register(self, spec, name):
        mods, vk = parse_hotkey(spec)
        if mods is None:
            wdlog.log.warn("%r 解析失败" % spec, tag="hotkey")
            return False
        hid = self._next_id
        # hwnd=NULL: WM_HOTKEY 投递到线程消息队列, 由 native event filter 接
        ok = self._user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk)
        if not ok:
            # 失败就不占 id —— 原来无条件自增, 反复失败会把 id 空间一直推高
            return False
        self._next_id += 1
        self._names[hid] = name
        self._specs[name] = spec
        return True

    def has(self, name):
        return name in self._specs

    def unregister(self, name):
        for hid, nm in list(self._names.items()):
            if nm != name:
                continue
            try:
                self._user32.UnregisterHotKey(None, hid)
            except Exception:  # noqa: BLE001
                pass
            del self._names[hid]
        self._specs.pop(name, None)

    def dispatch(self, hid):
        name = self._names.get(hid)
        if name:
            self.triggered.emit(name)

    def release(self):
        for hid in list(self._names):
            try:
                self._user32.UnregisterHotKey(None, hid)
            except Exception:  # noqa: BLE001
                pass
        self._names.clear()
        self._specs.clear()
        try:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
        except Exception:  # noqa: BLE001
            pass
