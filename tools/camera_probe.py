"""摄像头自检 —— 请先跑这一条。会走与主程序完全相同的取流代码。

    .venv\\Scripts\\python.exe tools\\camera_probe.py

不只检查"能不能打开", 还检查"画面是否真的在变" —— 这一点至关重要:
本机 index=0 是一个虚拟摄像头 (摩托罗拉 Smart Connect Camera), 它能打开、
也能读到帧, 但画面是**冻结的占位图**。测角算法拿冻结帧会永远返回 0,
表现为"开合上盖毫无反应", 而单看 isOpened() 完全发现不了。

成功后会存一张 frame_camera_probe.png 供你确认画面。
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from angles.camera import open_camera  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

OUT = Path(__file__).resolve().parent.parent / "frame_camera_probe.png"


def liveness(cap, n=6, gap=0.12):
    """抓 n 帧, 返回 (相邻帧最大平均差, 首帧, 帧尺寸)。

    真实传感器即使静止也有噪点, 平均差必然 > 0; 冻结帧恒为 0。
    """
    frames = []
    for _ in range(n):
        ok, f = cap.read()
        if ok and f is not None:
            frames.append(f)
        time.sleep(gap)
    if len(frames) < 3:
        return None, None, None
    diffs = [float(np.abs(frames[i].astype(np.int16)
                          - frames[i + 1].astype(np.int16)).mean())
             for i in range(len(frames) - 1)]
    return max(diffs), frames[0], frames[0].shape[:2]


def main():
    import cv2

    print("opencv :", cv2.__version__)
    reg = cv2.videoio_registry
    print("摄像头类后端:", [reg.getBackendName(b) for b in reg.getCameraBackends()])
    print("-" * 74)
    print("%-6s %-7s %-11s %-10s %-8s %s"
          % ("index", "后端", "分辨率", "画面变化", "ORB点", "结论"))

    good = []
    for idx in (0, 1, 2):
        for backend in ("dshow", "msmf"):
            try:
                cap, used = open_camera(idx, backend)
            except Exception:  # noqa: BLE001
                continue

            diff, frame, size = liveness(cap)
            if frame is None:
                print("%-6d %-7s %-11s %-10s %-8s %s"
                      % (idx, used, "-", "-", "-", "读不到帧"))
                cap.release()
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            kp = cv2.ORB_create(nfeatures=1200).detect(gray, None)
            live = diff is not None and diff > 1e-6
            verdict = ("可用" if (live and len(kp) > 20)
                       else "冻结帧(虚拟摄像头)" if not live
                       else "纹理不足")
            print("%-6d %-7s %-11s %-10s %-8d %s"
                  % (idx, used, "%dx%d" % (size[1], size[0]),
                     "%.3f" % diff if diff is not None else "-",
                     len(kp), verdict))
            if live and len(kp) > 20:
                good.append((idx, used, frame, len(kp), gray))
            cap.release()

    print("-" * 74)
    if good:
        idx, used, frame, nkp, gray = good[0]
        cv2.imwrite(str(OUT), frame)
        print("[OK] 推荐: camera_index=%d  camera_backend=%s" % (idx, used))
        print("     分辨率 %dx%d   ORB 特征点 %d   亮度均值 %.1f (std %.1f)"
              % (frame.shape[1], frame.shape[0], nkp, gray.mean(), gray.std()))
        print("     已存 %s" % OUT.name)
        if gray.mean() < 40:
            print("     [注意] 画面偏暗, 建议开灯 —— 过暗会让 ORB 匹配不稳、"
                  "自动曝光漂移变大")
        print()
        print("     把上面两个值写进 config.json, 然后:")
        print("       .venv\\Scripts\\python.exe main.py")
        return 0

    print("[FAIL] 没有任何 index/后端组合给出可用画面。请依次检查:")
    print("  1) 设置 -> 隐私和安全性 -> 相机 -> 打开\"让桌面应用访问相机\"")
    print("  2) 关掉可能占用摄像头的程序 (微信/钉钉/相机应用/浏览器视频页)")
    print("  3) 若只有虚拟摄像头可用, 在 设置 -> 蓝牙和其他设备 -> 摄像头 里")
    print("     确认物理摄像头是否启用")
    return 1


if __name__ == "__main__":
    sys.exit(main())
