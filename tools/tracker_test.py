"""验证移植过来的摄像头测角算法 -- 不需要真摄像头。

本机没有任何摄像头设备, 无法用真实硬件端到端验证。但测角算法本体
(ORB 匹配 -> 单应 RANSAC -> 纯旋转 SVD 拟合 -> 滑动关键帧 -> 锚点校正 ->
One Euro 滤波) 正是本次合并"移植"的那部分, 必须证明它真的能算对。

原理: 相机绕铰链旋转是**纯旋转**, 画面变化精确等于
        H = K . Rx(theta) . K^-1
所以可以合成: 拿一张有纹理的静止场景, 按已知 theta 做单应变换, 喂给算法,
看它能否把 theta 反解回来。若符号、K 矩阵或旋转方向写错, 这里会立刻暴露。

测法上必须注意两点, 否则会得出"算法不准"的错误结论:
  1) 必须逐帧小步爬升。一帧跳 5 度 = 150 度/秒, 远超真实开合速度, 而 One Euro
     滤波器的滞后会被误读成测角误差。真实上盖开合本身就是连续小增量。
  2) 必须留驻留帧。滤波器是有惯性的, 精度要在它收敛之后测量。
因此: 每 0.5 度一帧爬到目标角, 再驻留若干帧, 然后测; 跟踪滞后单独测。

用法:
    .venv\\Scripts\\python.exe tools\\tracker_test.py
    .venv\\Scripts\\python.exe tools\\tracker_test.py --max-deg 30
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from angles.camera import OrbTracker, angle_to_level  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

W, H = 640, 480
DT = 1.0 / 30.0


def make_scene():
    """合成一张"有纹理的静止场景" -- ORB 需要足够特征点。"""
    rng = np.random.default_rng(7)
    small = rng.integers(0, 255, (H // 8, W // 8), dtype=np.uint8)
    scene = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
    scene = cv2.GaussianBlur(scene, (0, 0), 1.2)
    for _ in range(45):
        x1, y1 = int(rng.integers(0, W)), int(rng.integers(0, H))
        x2, y2 = x1 + int(rng.integers(20, 140)), y1 + int(rng.integers(20, 140))
        cv2.rectangle(scene, (x1, y1), (min(x2, W - 1), min(y2, H - 1)),
                      int(rng.integers(0, 256)), -1)
    return scene


def make_K():
    f = float(max(H, W))
    return np.array([[f, 0, W / 2.0], [0, f, H / 2.0], [0, 0, 1.0]],
                    dtype=np.float64)


def rot_x(theta_deg, K):
    """相机绕 x 轴转 theta 后, 画面坐标的变换矩阵 K.Rx.K^-1 (纯旋转, 无视差)。"""
    t = np.radians(theta_deg)
    c, s = np.cos(t), np.sin(t)
    R = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    return K @ R @ np.linalg.inv(K)


class Sim:
    """把合成场景当"摄像头"喂给 tracker。"""

    def __init__(self, tracker, scene, K):
        self.tracker = tracker
        self.scene = scene
        self.K = K

    def frame_at(self, theta_deg):
        return cv2.warpPerspective(
            self.scene, rot_x(theta_deg, self.K), (W, H),
            borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    def feed(self, theta_deg):
        p = self.tracker.estimate(self.frame_at(theta_deg), dt=DT)
        return None if p is None else float(np.degrees(p))

    def ramp_to(self, target_deg, from_deg=0.0, step=0.5):
        """逐帧小步爬升, 模拟真实连续开合。step 为负即反向。"""
        out = None
        th = from_deg
        sign = 1.0 if step >= 0 else -1.0
        while (th - target_deg) * sign < -1e-9:
            th += step
            if (th - target_deg) * sign > 0:
                th = target_deg
            r = self.feed(th)
            if r is not None:
                out = r
        return out

    def dwell(self, theta_deg, frames):
        out = None
        for _ in range(frames):
            r = self.feed(theta_deg)
            if r is not None:
                out = r
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-deg", type=float, default=30.0)
    ap.add_argument("--step", type=float, default=5.0, help="精度测试的角度间隔")
    ap.add_argument("--tol", type=float, default=2.0, help="稳态测角容差 (度)")
    ap.add_argument("--lag-tol", type=float, default=6.0,
                    help="连续爬升时的滞后容差 (度)")
    args = ap.parse_args()

    # 合成模型的适用范围: 相机绕 x 轴转 theta 后, 原来的画面按 H 移出视野。
    # 用 tools/fov_probe.py 量过, 640x480 下:
    #     theta=30 度 -> 画面还剩 27.6% 有内容, 1066 个特征点
    #     theta=35 度 -> 只剩 15.7%, 344 个点
    #     theta=40 度 -> 只剩  3.1%, 0 个点 (一条细边, 匹配必然失败)
    # 也就是说 40 度以上测不出来是**合成测试的有限视场**导致的, 不是算法缺陷:
    # 真实场景是连续的三维房间, 每帧只走零点几度, 靠滑动关键帧把总角度串起来。
    if args.max_deg > 35.0:
        print("[注意] --max-deg=%.0f 超出合成模型适用上限 (约 35 度): "
              "画面几乎全部移出视野, 大量特征点消失, 失败不代表算法有问题。"
              % args.max_deg)

    scene = make_scene()
    K = make_K()
    failures = []

    # ---------- 基准帧 ----------
    probe = OrbTracker(with_camera=False)
    kp0, _ = probe.orb.detectAndCompute(probe.clahe.apply(scene), None)
    print("[标定] 合成场景特征点 %d 个" % len(kp0))

    # ---------- 1) 稳态精度: 小步爬到目标角并驻留 ----------
    print("-" * 66)
    print("%10s %12s %10s %10s" % ("真实角度", "稳态测得", "误差", "驻留帧"))
    worst = 0.0
    targets = np.arange(args.step, args.max_deg + 1e-9, args.step)
    for target in targets:
        tr = OrbTracker(with_camera=False)
        tr.set_reference(scene)
        sim = Sim(tr, scene, K)
        sim.ramp_to(target, step=0.5)
        got = sim.dwell(target, 25)
        if got is None:
            print("%10.1f %12s %10s %10d" % (target, "None", "-", 25))
            failures.append("theta=%g 稳态测不出结果" % target)
            continue
        err = got - target
        worst = max(worst, abs(err))
        print("%10.1f %12.2f %10.2f %10d" % (target, got, err, 25))

    print("[1] 稳态测角精度 : 最大误差 %.2f 度 (容差 %.2f)" % (worst, args.tol))
    if worst > args.tol:
        failures.append("稳态测角误差 %.2f 度超出容差" % worst)

    # ---------- 2) 跟踪滞后: 连续爬升时不看稳态 ----------
    # 真实开合是连续的, 这里量的是"边动边测"时落后多少, 属于 One Euro 的固有
    # 特性而不是错误。要求它别太离谱即可。
    tr = OrbTracker(with_camera=False)
    tr.set_reference(scene)
    sim = Sim(tr, scene, K)
    errs = []
    th = 0.0
    while th < args.max_deg - 1e-9:
        th = min(th + 1.0, args.max_deg)
        got = sim.feed(th)
        if got is not None:
            errs.append(th - got)
    if errs:
        # 丢掉起步几帧 (滤波器还没建立速度估计)
        steady = errs[5:] if len(errs) > 5 else errs
        lag = float(np.mean(steady))
        mx = float(np.max(np.abs(steady)))
        print("[2] 跟踪滞后     : 1.0 度/帧连续爬升, 平均滞后 %.2f 度, 最大 %.2f 度"
              % (lag, mx))
        if mx > args.lag_tol:
            failures.append("连续爬升时滞后 %.2f 度超出容差" % mx)
    else:
        failures.append("连续爬升时完全测不出角度")

    # ---------- 3) 往返归零 (锚点校正要解决的漂移问题) ----------
    tr = OrbTracker(with_camera=False)
    tr.set_reference(scene)
    sim = Sim(tr, scene, K)
    sim.ramp_to(args.max_deg, step=0.5)
    sim.dwell(args.max_deg, 10)
    sim.ramp_to(0.0, from_deg=args.max_deg, step=-0.5)
    back = sim.dwell(0.0, 25)
    print("[3] 往返归零     : 0 -> %g -> 0 后测得 %s 度"
          % (args.max_deg, "None" if back is None else "%.2f" % back))
    if back is None or abs(back) > args.tol:
        failures.append("往返后没回到 0 度 (测得 %s), 锚点校正可能失效"
                        % ("None" if back is None else "%.2f" % back))

    # ---------- 4) 单调性 ----------
    tr = OrbTracker(with_camera=False)
    tr.set_reference(scene)
    sim = Sim(tr, scene, K)
    seq = []
    th = 0.0
    while th < args.max_deg - 1e-9:
        th = min(th + 2.0, args.max_deg)
        got = sim.feed(th)
        if got is not None:
            seq.append(got)
    mono = all(seq[i] < seq[i + 1] + 0.5 for i in range(len(seq) - 1))
    print("[4] 单调性       : %s (%d 个采样点)"
          % ("OK" if mono else "FAIL", len(seq)))
    if not mono:
        failures.append("测角结果不单调: %s" % ["%.1f" % v for v in seq])

    # ---------- 5) 角度 -> level 映射 ----------
    print("-" * 66)
    print("[5] pitch -> level 映射 (SCALE=1.1, SIGN=-1, 死区 0.03):")
    for pitch in (-20, 0, 10, 30, 60, 90, 120, 160, 200):
        lvl, fold = angle_to_level(float(pitch), 1.1, -1, 0.03)
        print("      pitch=%+7.1f 度 -> fold=%6.1f  level=%.3f" % (pitch, fold, lvl))

    l0, _ = angle_to_level(0.0, 1.1, -1, 0.03)
    l1, _ = angle_to_level(90.0, 1.1, -1, 0.03)
    l2, _ = angle_to_level(200.0, 1.1, -1, 0.03)
    lneg, _ = angle_to_level(90.0, 1.1, +1, 0.03)
    if abs(l0) > 1e-9:
        failures.append("pitch=0 时 level 不为 0 (%.3f)" % l0)
    if not (0.0 <= l1 <= 1.0):
        failures.append("level 超出 [0,1]: %.3f" % l1)
    if abs(l2 - 1.0) > 1e-6:
        failures.append("大角度没有夹到 level=1 (%.3f)" % l2)
    if lneg >= l1:
        failures.append("SIGN 翻转没有反转方向")
    print("[6] 映射性质     : 0 度->%.3f  90 度->%.3f  200 度->%.3f  "
          "SIGN=+1 时 90 度->%.3f" % (l0, l1, l2, lneg))

    print("=" * 66)
    if failures:
        for f_ in failures:
            print("[FAIL] " + f_)
        return 1
    print("[PASS] 测角算法与映射全部通过 (合成帧验证, 非真实摄像头)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
