"""分析 --smoke 抓到的渲染帧, 用数值判断折叠效果是否真的出现。

本工具是为了在看不到图的情况下也能验证: 单看"PNG 生成成功"并不能说明
着色器真的产生了几何形变 -- 一张摊平的普通桌面截图同样会生成合法的 PNG。

判据 (逆投影模型的空间特征):
  - 靠铰链的底部保持清晰 (梯度大)
  - 远离铰链的顶部被模糊 (梯度小)
  若两者相当, 说明画面根本没被折叠。

用法:
    .venv\\Scripts\\python.exe tools\\check_smoke.py [png路径]
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

DEFAULT = Path(__file__).resolve().parent.parent / "smoke_widget.png"


def gradient_energy(gray):
    """平均梯度幅值 -- 反映锐利程度。"""
    gy, gx = np.gradient(gray)
    return float(np.sqrt(gx * gx + gy * gy).mean())


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print("[FAIL] 找不到 %s" % path)
        return 1

    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    h, w, _ = a.shape
    lum = a @ np.array([0.299, 0.587, 0.114], np.float32)

    print("图像          : %s (%dx%d)" % (path.name, w, h))
    print("整体亮度      : %.1f   std %.1f   min/max %.0f/%.0f"
          % (lum.mean(), lum.std(), lum.min(), lum.max()))

    top = lum[:h // 8]
    bot = lum[-h // 8:]
    et, eb = gradient_energy(top), gradient_energy(bot)
    print("顶部 (远端)   : 亮度 %.1f   梯度 %.2f" % (top.mean(), et))
    print("底部 (近铰链) : 亮度 %.1f   梯度 %.2f" % (bot.mean(), eb))

    failures = []
    if lum.std() < 3.0:
        failures.append("画面几乎是纯色 (std %.2f), 渲染没出内容" % lum.std())
    if lum.max() < 16:
        failures.append("画面全黑, 纹理/着色器没有输出")

    print("-" * 56)
    if eb > et * 1.3:
        print("[OK]   底部明显比顶部锐利 (%.2f vs %.2f) -> 折叠的空间感成立"
              % (eb, et))
    else:
        failures.append("底部没有明显比顶部锐利 (%.2f vs %.2f), "
                        "画面可能没被折叠" % (eb, et))

    if failures:
        for f in failures:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 渲染帧符合逆投影折叠的特征")
    return 0


if __name__ == "__main__":
    sys.exit(main())
