"""一次性探针: 合成视角下, 转过 theta 后还剩多少可匹配的画面。"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.tracker_test import W, H, make_K, make_scene, rot_x  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

scene = make_scene()
K = make_K()
orb = cv2.ORB_create(nfeatures=1200)

print("%8s %10s %10s %10s" % ("角度", "ORB特征", "非黑占比", "画面高度占用"))
for th in range(0, 61, 5):
    warped = cv2.warpPerspective(scene, rot_x(th, K), (W, H),
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    kp, _ = orb.detectAndCompute(warped, None)
    nonblack = float((warped > 0).mean())
    rows = np.where((warped > 0).any(axis=1))[0]
    span = (rows[-1] - rows[0] + 1) / H if len(rows) else 0.0
    print("%8d %10d %10.3f %10.3f" % (th, len(kp), nonblack, span))
