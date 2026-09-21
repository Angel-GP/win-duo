"""键盘控制 -- 合并了两个项目的操作方式, 并兼作 manual 角度源。

这个线程统一收所有快捷键 (所以渲染层和角度源都不用自己读键盘), 同时它本身
就是 manual 角度源: 上下键直接给浓度。

按键:
    ↑ / w        浓度 +3%
    ↓ / s        浓度 -3%
    → / d        拉满 100%
    ← / a        清零 0%
    r            切换 跟随传感器 <-> 手动覆盖
    m            轮换角度源 camera -> serial -> manual
    c            标定摄像头基准帧 (上盖完全展开时按)
    + / =        camera_scale +0.1        - / _   camera_scale -0.1
    f            翻转 camera_sign
    v            切换出界处理 black <-> backdrop
    x            开关摄像头特征匹配调试窗口
    Esc / q      退出

注意: 控制台窗口必须先点一下获得焦点, msvcrt.getwch() 才收得到键。
"""
import threading

import wdlog
from .base import AngleSource


def _has_console():
    """托盘模式常用 pythonw.exe 启动, 那时没有控制台窗口, msvcrt 无从读键。"""
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return False


class KeyControl(AngleSource):
    name = "manual"

    def __init__(self, cfg=None, auto=True):
        super().__init__()
        cfg = cfg or {}
        self._lock = threading.Lock()
        self._level = 0.0
        self._auto = bool(auto)
        self._last_key = ""
        self._cmds = []
        self.quit_flag = False
        self._started = False
        self._enabled = False        # 只在"键盘手动"这个角度源下才收功能键
        self.detail = {}

    # ---------- 开关 ----------
    def set_enabled(self, on):
        """功能键开关。由 SourceHub 在切角度源时设置 —— 只有键盘模式才响应。"""
        on = bool(on)
        with self._lock:
            changed = on != self._enabled
            self._enabled = on
        if changed:
            wdlog.log.debug("功能键%s" % ("已启用 (键盘模式)" if on else "已停用"), tag="keys")

    # ---------- 生命周期 ----------
    def start(self):
        if self._started:
            return
        self._started = True
        if not _has_console():
            wdlog.log.debug("没有控制台 (托盘/pythonw 启动), 控制台按键已停用 —— "
                            "键盘模式请用全局热键 (设置窗口里有列表)", tag="keys")
            return
        threading.Thread(target=self._run, name="keyboard", daemon=True).start()

    # ---------- AngleSource ----------
    def level(self):
        with self._lock:
            return self._level

    def status(self):
        with self._lock:
            return "手动" if not self._auto else "跟随传感器"

    def is_auto(self):
        with self._lock:
            return self._auto

    def set_auto(self, on):
        with self._lock:
            self._auto = bool(on)

    def set_level(self, v):
        with self._lock:
            self._level = max(0.0, min(1.0, float(v)))

    # ---------- 命令队列 ----------
    def pop_commands(self):
        with self._lock:
            cmds, self._cmds = self._cmds, []
            return cmds

    def _push(self, cmd):
        with self._lock:
            self._cmds.append(cmd)

    # ---------- 主循环 ----------
    def _run(self):
        try:
            import msvcrt
        except ImportError:
            wdlog.log.warn("非 Windows 平台, 键盘控制不可用", tag="keys")
            return

        while not self.quit_flag:
            ch = msvcrt.getwch()

            # 退出键永远有效: 否则在没有托盘的场合会被困住
            if ch == "\x1b":
                self.quit_flag = True
                break
            if ch and ch.lower() == "q":
                self.quit_flag = True
                break

            # 其余功能键只在键盘手动模式下响应。用 msvcrt 读键是阻塞的,
            # 所以这里只是"读了就丢", 不会有额外开销。
            with self._lock:
                enabled = self._enabled
            if not enabled:
                continue

            if ch in ("\xe0", "\x00"):
                k = msvcrt.getwch()
                if k == "H":
                    self._nudge(+0.03, "↑")
                elif k == "P":
                    self._nudge(-0.03, "↓")
                elif k == "M":
                    self._set(1.0, "→")
                elif k == "K":
                    self._set(0.0, "←")
                continue

            low = ch.lower()
            if low == "w":
                self._nudge(+0.03, "w")
            elif low == "s":
                self._nudge(-0.03, "s")
            elif low == "d":
                self._set(1.0, "d")
            elif low == "a":
                self._set(0.0, "a")
            elif low == "r":
                with self._lock:
                    self._auto = not self._auto
                    self._last_key = "跟随传感器" if self._auto else "手动覆盖"
                    wdlog.log.debug("键盘: %s" % self._last_key, tag="keys")
            elif low == "m":
                self._push("cycle_source")
            elif low == "c":
                self._push("calibrate")
            elif low == "v":
                self._push("toggle_outside")
            elif low == "x":
                self._push("toggle_debug")
            elif low == "b":
                self._push("pick_backdrop")
            elif low == "n":
                self._push("reload_backdrop")
            elif low == "f":
                self._push("flip_sign")
            elif ch in ("+", "="):
                self._push("scale_up")
            elif ch in ("-", "_"):
                self._push("scale_down")

    def _nudge(self, delta, key):
        with self._lock:
            self._auto = False          # 任何调节键都切回手动覆盖
            self._level = max(0.0, min(1.0, self._level + delta))
            self._last_key = key

    def _set(self, v, key):
        with self._lock:
            self._auto = False
            self._level = v
            self._last_key = key

    # ---------- 退出 ----------
    def stop(self):
        self.quit_flag = True
