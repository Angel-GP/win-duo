"""验证摄像头调试窗能正常打开、也**能正常关上**。

这是针对一个真实 bug 的回归测试: OpenCV 的 HighGUI 是"消息靠 waitKey 驱动"的,
destroyWindow() 只是把销毁请求排进队列, 不接着调 waitKey 就永远不会被处理 ——
窗口会一直留在屏幕上, 表现为"打开就关不上"。

顺带验证窗口标题是纯 ASCII: OpenCV 在 Windows 上按本地代码页解释标题,
中文会变成乱码。

用法:
    .venv\\Scripts\\python.exe tools\\debug_window_test.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def visible(cv2, name):
    """窗口是否还显示着。不存在时 OpenCV 会抛异常, 这里统一成 False。"""
    try:
        return cv2.getWindowProperty(name, cv2.WND_PROP_VISIBLE) >= 1
    except Exception:  # noqa: BLE001
        return False


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    import cv2
    from PyQt6.QtWidgets import QApplication

    import main as app_main
    from render.overlay import DEBUG_WINDOW
    from ui import AppController

    failures = []

    def check(label, cond, extra=""):
        print("  [%s] %-42s %s" % ("OK" if cond else "FAIL", label, extra))
        if not cond:
            failures.append(label)

    print("调试窗标题: %r" % DEBUG_WINDOW)
    check("标题是纯 ASCII (中文会乱码)", DEBUG_WINDOW.isascii())

    app_main.make_qsurface_format()
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)

    cfg = app_main.load_config()
    cfg["source"] = "camera"
    # **关掉低内存模式。** 本测试拷的是用户真实的 config.json, 而用户可能开着
    # 低内存模式 —— 那种模式下"浓度 0 的窗口"会被周期检查释放 (阈值只有几秒),
    # 而这里要等摄像头最多 40 秒, 期间 overlay 就变成 None 了, 后面全部踩空。
    # 本测试测的是调试窗, 不是内存策略, 所以显式关掉。
    cfg["low_memory_mode"] = False
    (ROOT / ".tmp").mkdir(exist_ok=True)
    ctl = AppController(cfg, ROOT / ".tmp" / "dbg.json")
    ctl.start_glass()

    # 等摄像头出帧。
    # **不能只 sleep 固定秒数**: 设备是异步打开的, 而 `auto` 要逐个后端试 +
    # 做冻结帧检测, 本机实测最坏要 20 秒 (DSHOW 那个虚拟摄像头打开失败时
    # 会卡很久)。所以要轮询"真的就绪了"再继续。
    print("\n等摄像头就绪 (异步打开, 最坏可能 20 秒)...")
    t0 = time.time()
    while time.time() - t0 < 40.0:
        app.processEvents()
        cam = ctl.hub.get("camera")
        if cam is not None and cam.status() not in ("正在打开…", "") \
                and cam.status() is not None:
            break
        time.sleep(0.05)
    print("  摄像头状态: %s  (等了 %.1fs)"
          % (ctl.hub.get("camera").status(), time.time() - t0))
    pump(app, 0.8)

    print("\n打开调试窗:")
    ctl.toggle_debug_window()
    pump(app, 1.5)
    check("调试窗已出现", visible(cv2, DEBUG_WINDOW),
          "visible=%s" % visible(cv2, DEBUG_WINDOW))

    print("\n再点一次同一个按钮 (以前这里关不掉):")
    ctl.toggle_debug_window()
    pump(app, 1.0)
    still = visible(cv2, DEBUG_WINDOW)
    check("调试窗已关闭", not still, "visible=%s" % still)

    print("\n再开一次, 然后用 X 关掉再点按钮:")
    ctl.toggle_debug_window()
    pump(app, 1.2)
    check("第二次也能正常打开", visible(cv2, DEBUG_WINDOW))
    try:
        cv2.destroyWindow(DEBUG_WINDOW)     # 模拟用户点 X
        cv2.waitKey(1)
    except Exception:  # noqa: BLE001
        pass
    pump(app, 1.2)
    check("用户手动关掉后状态被同步", not visible(cv2, DEBUG_WINDOW))
    check("覆盖层状态也跟着复位",
          ctl.overlay._dbg_window_open is False,
          "open=%s" % ctl.overlay._dbg_window_open)

    ctl.shutdown()
    # 关闭玻璃层时也必须把调试窗一起收掉
    check("关闭玻璃层后调试窗不残留", not visible(cv2, DEBUG_WINDOW))

    print("-" * 62)
    if failures:
        for f in failures:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 调试窗能开、能关、能同步, 中文乱码问题已消除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
