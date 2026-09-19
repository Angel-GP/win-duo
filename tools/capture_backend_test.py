"""验证截屏后端: 速度、通道顺序、以及在"桌面变化"时真的能拿到帧。

为什么需要这个测试:
  1. 重截频率的上限就是 1/单帧耗时。mss 走 GDI BitBlt (本机 ~29ms -> 34Hz),
     DXGI 走 GPU 复制 (~0.2ms), 但**必须在真机上量出来**, 不能靠文档。
  2. DXGI 返回的数组是 RGBA, mss 是 BGRA。搞反了红蓝互换, 肉眼很难发现
     (要看图才发现), 所以这里用通道均值比对来卡住。
  3. DXGI **只在桌面真的变了的时候才给新帧**, 没变化返回 None。这是它的正常
     行为而不是故障 —— 但必须验证"变化时确实给帧", 否则玻璃层会永远冻住。
     这里用一个自己抖动的窗口来制造变化。

用法:
    .venv\\Scripts\\python.exe tools\\capture_backend_test.py
"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

FAILS = []


def check(label, cond, extra=""):
    print("  [%s] %-46s %s" % ("OK" if cond else "FAIL", label, extra))
    if not cond:
        FAILS.append(label)


def pump_until_new(worker, app=None, timeout=1.5):
    """踢一次并等**帧号真的推进**。

    别用 `done.wait()` 当同步点: `done` 是持久标记, 不清就立刻返回; 清了也有
    竞态 —— worker 可能正忙着处理上一次请求, `kick()` 被 `busy` 挡掉, 而我们
    等到的却是上一次的 done。之前那个"40/40 帧"就是这么来的假通过。
    直接盯帧号最稳。

    `app` 是必须的: 等待期间要**持续跑 Qt 事件循环**, 否则用来制造画面变化的
    那个测试窗口根本不会重绘 —— 桌面没变, DXGI 自然不给新帧, 于是死等超时。
    (踩过一次: 只 sleep 不 processEvents, 结果 0/40 帧。)
    """
    with worker.lock:
        before = worker.frame[3] if worker.frame else -1
    worker.kick()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        with worker.lock:
            if worker.frame and worker.frame[3] != before:
                return True
        time.sleep(0.004)
    return False


def main():
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QWidget

    from render.capture import CaptureWorker, frame_bgr
    from ui import monitors

    app = QApplication([sys.argv[0]])
    screen = app.primaryScreen()
    region = monitors.region_for(screen)
    W, H = region["width"], region["height"]
    print("=" * 70)
    print("截屏后端验证  %dx%d  显示器刷新率 %.0f Hz" % (W, H, screen.refreshRate()))
    print("=" * 70)

    # ---------- 1. 后端选择与速度 ----------
    print("\n后端与速度:")
    dxgi = CaptureWorker(region, size=(W, H), backend="auto",
                         display_hz=screen.refreshRate())
    dxgi._open()
    check("自动选择到了 DXGI 后端", dxgi.backend in ("bettercam", "dxcam"),
          dxgi.backend)

    # 直接量抓屏本身 —— 别用线程 + kick 去量, 那量到的是 wait 超时
    import mss as _mss
    sct = _mss.MSS()
    sct.grab(region)
    t0 = time.perf_counter()
    for _ in range(20):
        sct.grab(region)
    mss_ms = (time.perf_counter() - t0) / 20 * 1000.0

    # DXGI 静止时不返回帧; 但 grab() 本身的耗时照样能测
    for _ in range(5):
        dxgi._dxgi.grab()
    t0 = time.perf_counter()
    for _ in range(200):
        dxgi._dxgi.grab()
    dxgi_ms = (time.perf_counter() - t0) / 200 * 1000.0

    print("      mss      %7.2f ms/帧 -> 上限 %5.0f Hz" % (mss_ms, 1000.0 / mss_ms))
    print("      %-8s %7.2f ms/帧 -> 上限 %5.0f Hz (显示器 %.0f Hz)"
          % (dxgi.backend, dxgi_ms, min(1000.0 / max(dxgi_ms, 1e-6),
                                        screen.refreshRate()), screen.refreshRate()))
    check("DXGI 比 mss 快 (>=3 倍)", dxgi_ms * 3 <= mss_ms,
          "%.2fms vs %.2fms (%.0f 倍)" % (dxgi_ms, mss_ms, mss_ms / max(dxgi_ms, 1e-6)))

    # worker 层: 真跑一遍, 确认 max_hz 反映的是 DXGI 而不是兜底帧
    dxgi.start()
    dxgi.kick()
    dxgi.done.wait(3)
    check("worker 能拿到帧 (静止桌面靠 mss 种子帧)", dxgi.latest() is not None)
    # 上限是**固定的显示器刷新率**, 不是实测耗时算出来的动态值
    check("max_hz 是固定的显示器刷新率",
          abs(dxgi.max_hz() - max(60.0, screen.refreshRate())) < 1e-6,
          "%.0f Hz (屏 %.0f Hz)" % (dxgi.max_hz(), screen.refreshRate()))
    check("上限远高于 mss 能做到的（这才是提升上限的意义）",
          dxgi.max_hz() > 1000.0 / mss_ms * 2,
          "%.0f Hz vs mss 只能 %.0f Hz" % (dxgi.max_hz(), 1000.0 / mss_ms))

    # ---------- 2. 桌面变化时 DXGI 必须给帧 ----------
    print("\n桌面变化时 DXGI 必须给新帧:")
    win = QWidget()
    win.setWindowFlags(Qt.WindowType.FramelessWindowHint
                       | Qt.WindowType.WindowStaysOnTopHint)
    win.setGeometry(0, 0, 400, 300)
    win.show()
    got = 0
    dxgi_frame = None
    last_seq = -1
    for i in range(40):
        win.setStyleSheet("background: %s;" % ("#ff0000" if i % 2 else "#00ff00"))
        app.processEvents()
        time.sleep(0.02)
        if pump_until_new(dxgi, app):
            got += 1
    check("抖动窗口时 DXGI 给出了新帧", got >= 5, "%d 帧" % got)

    # ---------- 3. 通道顺序: 用纯色窗口做判据 ----------
    # 这是**决定性**的判据, 不依赖壁纸颜色: 画一个纯蓝窗口, 抓下来看
    # frame_bgr 给的三通道里蓝色分量是不是真的落在 B 上。
    # (之前拿"通道均值"当判据不可靠: 壁纸接近无彩色时 R/B 互换只让均值动 2.0,
    #  那种反例给的是假的安全感。)
    print("\n通道顺序 (用纯色窗口判定):")
    PURE_BLUE = "#0000ff"
    win.setStyleSheet("background: %s;" % PURE_BLUE)
    app.processEvents()
    blue_frame = None
    for _ in range(40):
        win.setStyleSheet("background: %s;" % ("#0010ff" if _ % 2 else PURE_BLUE))
        app.processEvents()
        time.sleep(0.02)
        if pump_until_new(dxgi, app):
            blue_frame = dxgi.latest()
            break
    win.close()
    app.processEvents()
    check("拿到了纯蓝窗口那一帧", blue_frame is not None)
    if blue_frame is not None:
        bgr = frame_bgr(blue_frame)
        # 窗口在左上角 400x300, 取中间一小块避开边框
        patch = bgr[100:200, 100:300].reshape(-1, 3).astype(np.int32).mean(axis=0)
        print("      纯蓝窗口区域 BGR 均值 = B=%.0f G=%.0f R=%.0f"
              % (patch[0], patch[1], patch[2]))
        check("蓝色落在 B 通道（没有红蓝互换）",
              patch[0] > 200 and patch[2] < 60,
              "B=%.0f R=%.0f" % (patch[0], patch[2]))

    # 合成像素做确定性反例 —— 别拿真实桌面当反例: 壁纸接近无彩色时 R/B 互换
    # 只让通道均值动 2.0, 根本卡不住错误, 那种"反例"是假的安全感。
    print("\n通道转换单测 (合成像素):")
    px = np.zeros((2, 2, 4), dtype=np.uint8)
    px[:, :, 0] = 200      # R
    px[:, :, 1] = 50       # G
    px[:, :, 2] = 10       # B
    px[:, :, 3] = 255
    got_rgba = frame_bgr((px, 2, 2, 0, "RGBA")).reshape(-1, 3)[0]
    print("      输入 RGBA=(200,50,10) -> frame_bgr 得 BGR=%s" % list(got_rgba))
    check("RGBA 输入被正确转成 BGR",
          list(got_rgba) == [10, 50, 200], str(list(got_rgba)))
    got_bad = frame_bgr((px, 2, 2, 0, "BGRA")).reshape(-1, 3)[0]
    check("反例: 标成 BGRA 时通道确实错位",
          list(got_bad) == [200, 50, 10], str(list(got_bad)))
    check("反例与正确结果的差异足够大",
          abs(int(got_bad[0]) - int(got_rgba[0])) > 100,
          "B 差 %d" % abs(int(got_bad[0]) - int(got_rgba[0])))

    # ---------- 4. 帧号必须持续推进（不再有"内容没变就不推进"的过滤） ----------
    # **这条是回归防线。** 曾经有个全图平均差的静止检测 (阈值 1.5), 而日常操作
    # (打字/滚动/小窗口) 摊到 400 万像素上平均差只有 0.2~0.3 —— 永远够不到阈值,
    # 帧号永不推进, 玻璃层停在第一帧, 用户报"直接变成静态的了, 帧率似乎为 0"。
    # 现在的规矩: 后端给了帧就推进, 不做内容比较。
    # 注意 worker 线程在跑时**不能**直接调 _dxgi.grab() —— 并发
    # AcquireNextFrame 会直接 DXGI_ERROR_INVALID_CALL。一律走 kick 等帧号。
    print("\n帧号必须持续推进 (哪怕桌面只有小变化):")
    time.sleep(0.4)
    # **测量期间必须真的制造画面变化。** 之前只 kick 不抖窗口, 桌面恰好静止时
    # 帧号当然不推进 (那是**正确行为**), 却会被判成 FAIL —— 我因此误报过一次。
    # 这里用一个 400x300 的小窗口来回变色, 模拟"用户只有小幅操作"。
    tw = QWidget()
    tw.setWindowFlags(Qt.WindowType.FramelessWindowHint
                      | Qt.WindowType.WindowStaysOnTopHint)
    tw.setGeometry(300, 300, 400, 300)
    tw.show()
    app.processEvents()
    time.sleep(0.3)

    seq0 = dxgi.latest()[3]
    t0 = time.perf_counter()
    i = 0
    while time.perf_counter() - t0 < 1.5:
        tw.setStyleSheet("background: %s;"
                         % ("#ff0000" if i % 2 else "#00ff00"))
        app.processEvents()
        dxgi.kick()
        i += 1
        time.sleep(0.01)
    wall = time.perf_counter() - t0
    seq1 = dxgi.latest()[3]
    tw.close()
    app.processEvents()
    rate = (seq1 - seq0) / wall
    print("      1.5s 内帧号 %d -> %d = %.0f/s" % (seq0, seq1, rate))
    check("帧号在持续推进 (>= 10/s)", rate >= 10.0, "%.0f/s" % rate)

    # ---------- 5. 实际速率: 上限不能只是纸面数字 ----------
    print("\n实际速率 (上限是纸面值, 这里看真跑到了多少):")
    win2 = QWidget()
    win2.setWindowFlags(Qt.WindowType.FramelessWindowHint
                        | Qt.WindowType.WindowStaysOnTopHint)
    win2.setGeometry(200, 200, 800, 600)
    win2.show()
    app.processEvents()
    seq_a = dxgi.latest()[3]
    t0 = time.perf_counter()
    n = 0
    end = time.time() + 2.0
    while time.time() < end:
        # 以 ~120Hz 抖窗口; worker 自己尽力跑 (它忙的时候 kick 会被跳过)
        win2.setStyleSheet("background: %s;" % ("#f0f" if n % 2 else "#0ff"))
        app.processEvents()
        dxgi.kick()
        n += 1
        time.sleep(0.008)
    wall = time.perf_counter() - t0
    seq_b = dxgi.latest()[3]
    win2.close()
    app.processEvents()
    real = (seq_b - seq_a) / wall
    print("      桌面持续变化时: 拿到 %.0f 帧/秒 (屏 %.0f Hz)"
          % (real, screen.refreshRate()))
    # 阈值定宽一点: 这里测的是"worker 尽力跑能到多少", 结果随系统负载在
    # 40~140 之间浮动 (实测踩过 55/s 被卡)。只要明显高于 mss 的 37Hz 上限,
    # 就足以证明后端换对了。
    check("持续变化时实际帧率 >= 40/s (mss 上限只有 37)", real >= 40.0,
          "%.0f/s" % real)
    check("实际帧率不超过显示器刷新率", real <= screen.refreshRate() * 1.15,
          "%.0f/s vs 屏 %.0f Hz" % (real, screen.refreshRate()))

    dxgi.stop()
    print("-" * 70)
    if FAILS:
        for f in FAILS:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 截屏后端: DXGI 可用、通道正确、变化时给帧、上限提到显示器刷新率")
    return 0


if __name__ == "__main__":
    sys.exit(main())
