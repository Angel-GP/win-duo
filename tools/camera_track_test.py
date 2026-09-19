"""真机摄像头追踪检测 —— 判断你的摆位/场景纹理能不能支撑测角。

tracker_test.py 用合成帧验证算法本身；这个用**真实摄像头**验证现场条件。
静止时 pitch 应该稳在 0 附近、level 应该恒为 0; 若漂移大或匹配点很少,
说明场景纹理不够或光照太差, 开合时会抖。

用法:
    .venv\\Scripts\\python.exe tools\\camera_track_test.py
    .venv\\Scripts\\python.exe tools\\camera_track_test.py --seconds 15
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from angles.camera import CameraAngleSource  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--camera", type=int, default=None, help="覆盖 camera_index")
    ap.add_argument("--backend", default=None, help="覆盖 camera_backend")
    ap.add_argument("--drift-tol", type=float, default=1.5,
                    help="静止时 pitch 标准差容差 (度)")
    ap.add_argument("--min-matches", type=int, default=80,
                    help="可接受的最少匹配点数")
    args = ap.parse_args()

    # 直接读 config.json, 测的就是主程序真正会用的配置
    cfg = json.loads(CFG_PATH.read_text("utf-8-sig"))
    if args.camera is not None:
        cfg["camera_index"] = args.camera
    if args.backend:
        cfg["camera_backend"] = args.backend

    src = CameraAngleSource(cfg)
    print("打开摄像头 (index=%s, backend=%s)..."
          % (cfg.get("camera_index"), cfg.get("camera_backend")))
    src.start()
    if not src.available():
        print("[FAIL] 摄像头打不开, 先跑 tools/camera_probe.py")
        return 1

    # 等自动标定。
    # **时限要给足**: 设备是异步打开的, 而 `auto` 要逐个后端试 + 做冻结帧检测,
    # 本机实测最坏要 ~20 秒 (DSHOW 那个虚拟摄像头打开失败时会卡很久)。
    # 原来写死 8 秒, 在这种时候会误报"未能进入追踪状态"。
    deadline = time.time() + 40.0
    while time.time() < deadline:
        if src.status() in ("追踪中",):
            break
        time.sleep(0.2)
    print("状态: %s" % src.status())
    if src.status() != "追踪中":
        print("[FAIL] 未能进入追踪状态 —— 让摄像头对着有纹理的静止场景, "
              "并保持上盖完全展开后重试")
        src.stop()
        return 1

    print("-" * 66)
    print("采样 %.0f 秒 (保持**静止不动**)..." % args.seconds)
    pitches, levels, matches = [], [], []
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        lvl = src.level()
        d = src.detail or {}
        if lvl is not None and "pitch" in d:
            pitches.append(d["pitch"])
            levels.append(lvl)
            matches.append(d["matches"])
        time.sleep(1.0 / args.hz)

    src.stop()

    if len(pitches) < 10:
        print("[FAIL] 有效样本太少 (%d), 追踪不稳定" % len(pitches))
        return 1

    p = np.asarray(pitches)
    m = np.asarray(matches)
    lv = np.asarray(levels)
    print("-" * 66)
    print("pitch   : 均值 %+7.3f 度   标准差 %.3f 度   范围 %+.2f ~ %+.2f"
          % (p.mean(), p.std(), p.min(), p.max()))
    print("level   : 最大 %.4f   非零样本 %d / %d" % (lv.max(),
                                                      int((lv > 0).sum()), len(lv)))
    print("匹配点  : 均值 %.0f   最少 %d" % (m.mean(), m.min()))

    failures = []
    if p.std() > args.drift_tol:
        failures.append("静止时 pitch 标准差 %.3f 度 > %.2f, 会看到抖动"
                        % (p.std(), args.drift_tol))
    if m.min() < args.min_matches:
        failures.append("最少匹配点 %d < %d, 场景纹理不足"
                        % (m.min(), args.min_matches))
    if lv.max() > 0.02:
        failures.append("静止时 level 已经到 %.3f, 应该接近 0 "
                        "(基准帧可能标定在错误姿态)" % lv.max())

    print("-" * 66)
    if failures:
        for f in failures:
            print("[FAIL] " + f)
        print("""
改善建议:
  - 让摄像头对着有纹理的静止场景 (书架/墙面/房间), 别对着纯白墙、天花板或自己
  - 打开室内灯, 避免逆光和过暗 (自动曝光漂移会引入误差)
  - 标定基准帧时上盖必须**完全展开**
  - 仍然抖动就把 config.json 的 camera_min_cutoff 调小 (更平滑, 代价是更滞后)""")
        return 1

    print("[PASS] 静止时追踪稳定 (pitch 标准差 %.3f 度), 场景纹理够用。" % p.std())
    print("       现在慢慢开合上盖:  .venv\\Scripts\\python.exe main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
