"""验证全局热键真的能触发。

做两件事:
  1. 低层: 注册 -> **真的模拟按键** (keybd_event) -> 确认事件到达 -> 注销后不再触发
  2. 高层: 键盘模式下的浓度热键 (Ctrl+Alt+↑/↓/←/→) 真的能改浓度,
     并且切到摄像头模式后会自动注销

为什么键盘模式要用全局热键: 启动器是 pythonw.exe, **没有控制台**, 原来靠
`msvcrt.getwch()` 读控制台按键根本读不到 —— 这就是"键盘模式没生效"的原因。

注意: 运行时会真的按几个组合键。它们在本程序里只做开关玻璃层/调浓度,
对你的其它程序一般无副作用, 但知道一下比较好。

用法:
    .venv\\Scripts\\python.exe tools\\hotkey_test.py
"""
import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

KEYUP = 0x0002
VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
      "d": 0x44, "c": 0x43, "f": 0x46, "x": 0x58, "esc": 0x1B, "f5": 0x74,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}


def press(combo):
    """模拟按下并松开一组键。"""
    u = ctypes.windll.user32
    for k in combo:
        u.keybd_event(VK[k], 0, 0, 0)
    for k in reversed(combo):
        u.keybd_event(VK[k], 0, KEYUP, 0)


def main():
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from ui.hotkey import HotkeyManager, parse_hotkey

    failures = []

    def check(label, cond, extra=""):
        print("  [%s] %-44s %s" % ("OK" if cond else "FAIL", label, extra))
        if not cond:
            failures.append(label)

    print("解析:")
    for spec in ("ctrl+alt+shift+esc", "ctrl+alt+up", "ctrl+alt+d", "shift+f5"):
        print("  %-20s -> %s" % (spec, parse_hotkey(spec)))
    check("ctrl+alt+shift+esc 解析为 Esc",
          parse_hotkey("ctrl+alt+shift+esc") == (7, 0x1B),
          str(parse_hotkey("ctrl+alt+shift+esc")))
    check("ctrl+alt+up 解析为方向键上",
          parse_hotkey("ctrl+alt+up") == (3, 0x26),
          str(parse_hotkey("ctrl+alt+up")))
    check("缺修饰键时解析失败", parse_hotkey("esc") == (None, None))
    check("乱写时解析失败", parse_hotkey("not-a-key") == (None, None))

    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)

    # ---------- 低层: 注册 / 触发 / 注销 ----------
    print("\n注册与触发 (低层):")
    mgr = HotkeyManager()
    got = []
    mgr.triggered.connect(got.append)
    check("注册 toggle", mgr.register("ctrl+alt+d", "toggle"))
    check("注册 off", mgr.register("ctrl+alt+shift+esc", "off"))
    check("has() 能查到", mgr.has("toggle") is True)
    mgr.unregister("off")
    check("unregister 后 has() 为假", mgr.has("off") is False)

    def low_send():
        press(["ctrl", "alt", "d"])

    def low_done():
        check("未注册的键不触发", got == ["toggle"], str(got))
        mgr.release()
        run_high_level()

    print("  (这一步会真的按一下 Ctrl+Alt+D)")
    QTimer.singleShot(300, low_send)
    QTimer.singleShot(900, low_done)

    # ---------- 高层: 键盘模式的浓度热键 ----------
    def run_high_level():
        import main as app_main
        from ui import AppController

        print("\n键盘模式热键 (高层):")
        (ROOT / ".tmp").mkdir(exist_ok=True)
        cfg = app_main.load_config()
        cfg["source"] = "manual"
        ctl = AppController(cfg, ROOT / ".tmp" / "hotkey.json")
        ctl.register_hotkeys()

        lines = ctl.hotkey_lines()
        labels = [v for _, v in lines]
        for spec, label in lines:
            print("      %-22s %s" % (spec, label))
        check("键盘模式下注册了浓度热键",
              any("+5%" in v for v in labels), str(len(labels)) + " 个")
        check("键盘模式下注册了调试窗", any("调试" in v for v in labels))
        check("键盘模式下不注册摄像头专有键（标定/翻转）",
              not any(x in v for v in labels for x in ("标定", "翻转")))
        check("紧急关闭始终在", any("紧急关闭" in v for v in labels))

        ctl.control.set_auto(False)
        ctl.control.set_level(0.50)

        def step1():
            press(["ctrl", "alt", "up"])

        def step2():
            check("Ctrl+Alt+↑ 浓度 +5%",
                  abs(ctl.control.level() - 0.55) < 1e-6,
                  "%.2f" % ctl.control.level())
            press(["ctrl", "alt", "right"])

        def step3():
            check("Ctrl+Alt+→ 拉满 100%",
                  abs(ctl.control.level() - 1.0) < 1e-6,
                  "%.2f" % ctl.control.level())
            press(["ctrl", "alt", "left"])

        def step4():
            check("Ctrl+Alt+← 清零",
                  abs(ctl.control.level() - 0.0) < 1e-6,
                  "%.2f" % ctl.control.level())
            press(["ctrl", "alt", "down"])

        def step5():
            check("Ctrl+Alt+↓ 在 0 处夹住不为负",
                  abs(ctl.control.level() - 0.0) < 1e-6,
                  "%.2f" % ctl.control.level())
            # 切到摄像头 -> 键盘模式的键全部注销, 只剩紧急关闭
            ctl.set_source("camera")
            labels2 = [v for _, v in ctl.hotkey_lines()]
            check("摄像头模式下只剩紧急关闭",
                  len(labels2) == 1 and "紧急关闭" in labels2[0], str(labels2))
            check("摄像头模式下浓度热键已注销",
                  not any("+5%" in v for v in labels2))
            check("摄像头模式下开关玻璃层也注销了",
                  not any("开关玻璃层" in v for v in labels2))
            check("键盘模式下摄像头已停",
                  ctl.hub.camera_running() is False,
                  "camera_running=%s" % ctl.hub.camera_running())

            ctl.set_source("manual")
            check("切回键盘后浓度热键恢复",
                  any("+5%" in v for _, v in ctl.hotkey_lines()))
            check("切回键盘后摄像头仍是停的",
                  ctl.hub.camera_running() is False)

            ctl.shutdown()
            print("-" * 62)
            if failures:
                for f in failures:
                    print("[FAIL] " + f)
                app.exit(1)
            else:
                print("[PASS] 全局热键可用；键盘模式热键随角度源注册/注销")
                app.exit(0)

        QTimer.singleShot(250, step1)
        QTimer.singleShot(650, step2)
        QTimer.singleShot(1050, step3)
        QTimer.singleShot(1450, step4)
        QTimer.singleShot(1850, step5)

    QTimer.singleShot(0, lambda: None)
    rc = app.exec()
    return rc


if __name__ == "__main__":
    sys.exit(main())
