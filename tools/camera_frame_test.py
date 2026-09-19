"""检查摄像头是否真的在出帧 (而不是冻结帧/占位图)。

为什么需要: 静止时 pitch 恒为 0.000、匹配点数完全不变, 这在真实传感器上
不可能 —— 真实摄像头即使静止也有噪点。冻结帧会让测角永远返回 0,
表现为"开合上盖毫无反应"。

用法:
    .venv\\Scripts\\python.exe tools\\camera_frame_test.py
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


def probe(idx, backend="dshow", n=12, gap=0.15):
    try:
        cap, used = open_camera(idx, backend)
    except Exception as exc:  # noqa: BLE001
        print("  index=%d 打不开: %s" % (idx, str(exc).splitlines()[0][:70]))
        return None

    frames = []
    for _ in range(n):
        ok, f = cap.read()
        if ok and f is not None:
            frames.append(f)
        time.sleep(gap)
    cap.release()

    if len(frames) < 3:
        print("  index=%d 只读到 %d 帧" % (idx, len(frames)))
        return None

    diffs = [float(np.abs(frames[i].astype(np.int16)
                          - frames[i + 1].astype(np.int16)).mean())
             for i in range(len(frames) - 1)]
    h, w = frames[0].shape[:2]
    gray = frames[0].mean(axis=2)
    print("  index=%d 后端=%s  %dx%d  帧数=%d" % (idx, used, w, h, len(frames)))
    print("    相邻帧平均差异 : 最小 %.4f  最大 %.4f  均值 %.4f"
          % (min(diffs), max(diffs), sum(diffs) / len(diffs)))
    print("    首帧亮度       : 均值 %.1f  std %.1f" % (gray.mean(), gray.std()))
    identical = max(diffs) < 1e-6
    print("    结论           : %s" % ("**冻结帧** (画面完全不变)" if identical
                                       else "有实时变化, 是真实出帧"))
    return not identical


def main():
    print("逐个 index 检查是否真的在出帧 (各采样 12 帧 / 约 2 秒)")
    print("-" * 68)
    alive = []
    for idx in (0, 1, 2):
        r = probe(idx)
        if r:
            alive.append(idx)
        print()
    print("-" * 68)
    if alive:
        print("[OK] 可用且有实时画面的 index: %s" % alive)
        print("     把 config.json 的 camera_index 设为 %d" % alive[0])
        return 0
    print("[FAIL] 没有任何 index 给出实时画面。")
    print("  若都是冻结帧: 大概率选到了虚拟摄像头 (如手机的\"网络摄像头\"功能)。")
    print("  在 设置 -> 蓝牙和其他设备 -> 摄像头 里看看有几个, 逐个试 index。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
