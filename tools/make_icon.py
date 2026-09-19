"""生成 win-duo 的程序图标 (win-duo.ico)。

设计: **两块玻璃面板 + 中间的铰链** —— 直接表达"折叠屏"。
    左面板 = 正对视角, 亮
    右面板 = 略微收窄压暗, 表示朝里折过去了
    中间竖亮线 = 铰链/折痕
    整块放在圆角方形底上, 这样托盘/任务栏里轮廓清楚。

几个要点:
  - **每个尺寸单独重绘, 不是把 256 缩下去。** 16x16 下细线和细节会糊成一团,
    所以小尺寸要主动简化 (去掉高光、加粗铰链)。
  - 先按 4 倍尺寸画再平滑缩回, 边缘才干净。
  - ICO 要塞多档尺寸: Windows 在托盘/任务栏/资源管理器/Alt-Tab 各用一档,
    缺档它会自己硬缩, 小尺寸下很难看。

用法:
    .venv\\Scripts\\python.exe tools\\make_icon.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "win-duo.ico"

#: Windows 图标标准档位
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

#: 每档放大这么多倍来画, 最后平滑缩回 (超采样抗锯齿)
SS = 4

# ---- 配色: 玻璃蓝紫, 浅色和深色任务栏上都能看清 ----
BG_A = (96, 165, 250)        # 左上: 亮天蓝
BG_B = (67, 56, 202)         # 右下: 深靛蓝
BG_EDGE = (30, 41, 130)      # 外描边
PANE_L_TOP = (255, 255, 255, 210)    # 左面板: 亮玻璃
PANE_L_BOT = (219, 234, 254, 170)
PANE_R_TOP = (191, 219, 254, 120)    # 右面板: 压暗 (折过去了)
PANE_R_BOT = (147, 179, 235, 95)
HINGE = (255, 255, 255, 245)


def render_png(size, path):
    """画一张 size×size 的 PNG。"""
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import (QBrush, QColor, QImage, QLinearGradient,
                             QPainterPath, QPen, QPolygonF, QPainter)

    big = size * SS
    img = QImage(big, big, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 小尺寸要简化: 高光在 16px 下只会变成脏点
    tiny = size <= 20
    small = size <= 32

    def px(f):
        return big * f

    # ================= 圆角方形底 =================
    pad = px(0.055)
    rad = px(0.235)
    body = QRectF(pad, pad, big - 2 * pad, big - 2 * pad)

    grad = QLinearGradient(body.topLeft(), body.bottomRight())
    grad.setColorAt(0.0, QColor(*BG_A))
    grad.setColorAt(1.0, QColor(*BG_B))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(body, rad, rad)

    # 外描边: 浅色背景上也能看出边界
    ew = max(1.0, px(0.017))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(*BG_EDGE, 80), ew))
    inset = pad + ew / 2
    p.drawRoundedRect(QRectF(inset, inset, big - 2 * inset, big - 2 * inset),
                      rad - ew / 2, rad - ew / 2)

    # ================= 两块玻璃面板 =================
    # 左面板: 从左边到中线; 右面板: 从中线到右边, 但上下都收窄一点、
    # 整体再小一圈 —— 这点"收窄"就是"朝里折过去"的视觉线索。
    gap = px(0.028)                     # 两块之间的缝, 铰链画在缝里
    fold_x = big * 0.5

    lx0, lx1 = px(0.185), fold_x - gap / 2
    ly0, ly1 = px(0.315), px(0.685)
    rx0, rx1 = fold_x + gap / 2, px(0.815)
    ry0, ry1 = px(0.375), px(0.625)     # 右面板上下各收 6%, 表示折进去了
    rr = px(0.055)                      # 面板圆角

    def pane_gradient(rect, top, bot):
        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, QColor(*top))
        g.setColorAt(1.0, QColor(*bot))
        return g

    # 右面板先画 (在底层), 左面板盖在上面 —— 折过去的那块应该被压住
    r_rect = QRectF(rx0, ry0, rx1 - rx0, ry1 - ry0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(pane_gradient(r_rect, PANE_R_TOP, PANE_R_BOT)))
    p.drawRoundedRect(r_rect, rr, rr)

    l_rect = QRectF(lx0, ly0, lx1 - lx0, ly1 - ly0)
    p.setBrush(QBrush(pane_gradient(l_rect, PANE_L_TOP, PANE_L_BOT)))
    p.drawRoundedRect(l_rect, rr, rr)

    # ================= 铰链: 中间那道亮竖线 =================
    # **小尺寸下必须加粗** —— 16x16 时按比例算出来的线宽不足 1 像素, 缩回后
    # 亮线和不亮的面板糊在一起, 中列亮度会和两侧一样 (实测 147 vs 148),
    # 等于白画。所以给一个"至少 1.2 逻辑像素"的下限。
    hw = max(px(0.052), SS * (2.4 if tiny else 1.5))
    hx = fold_x
    if tiny:
        # 16/20px: 只留中间一小段, 短而粗, 保证"中间有个亮点"
        hy0, hy1 = px(0.37), px(0.63)
    else:
        hy0, hy1 = px(0.295), px(0.705)
    p.setPen(QPen(QColor(*HINGE), hw,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(hx, hy0), QPointF(hx, hy1))

    # ================= 玻璃高光 (小尺寸直接跳过) =================
    if not small:
        # 左面板上一条斜向反光, 用三角形做个柔和的面
        sheen = QPolygonF([
            QPointF(lx0 + (lx1 - lx0) * 0.10, ly1),
            QPointF(lx0 + (lx1 - lx0) * 0.52, ly0),
            QPointF(lx0 + (lx1 - lx0) * 0.74, ly0),
            QPointF(lx0 + (lx1 - lx0) * 0.30, ly1),
        ])
        sg = QLinearGradient(QPointF(lx0, ly0), QPointF(lx1, ly1))
        sg.setColorAt(0.0, QColor(255, 255, 255, 0))
        sg.setColorAt(0.5, QColor(255, 255, 255, 70))
        sg.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(sg))
        clip = QPainterPath()
        clip.addRoundedRect(l_rect, rr, rr)
        p.save()
        p.setClipPath(clip)
        p.drawPolygon(sheen)
        p.restore()

        # 顶边一道细亮线: 玻璃的厚度感
        p.setPen(QPen(QColor(255, 255, 255, 130), max(1.0, px(0.012)),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(lx0 + rr * 0.7, ly0 + px(0.012)),
                   QPointF(lx1 - rr * 0.4, ly0 + px(0.012)))

    p.end()

    out = img.scaled(size, size,
                     Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    if not out.save(str(path), "PNG"):
        raise RuntimeError("写不出 PNG: %s" % path)


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication, QIcon

    app = QGuiApplication([sys.argv[0]])   # noqa: F841

    tmp = Path(tempfile.mkdtemp(prefix="windo-icon-"))
    frames = []
    for size in SIZES:
        path = tmp / ("icon-%d.png" % size)
        render_png(size, path)
        frames.append((size, path))
        print("  画好 %3d x %-3d" % (size, size))

    try:
        from PIL import Image
    except ImportError:
        print("[!] 缺 Pillow:  .venv\\Scripts\\python.exe -m pip install Pillow")
        return 1

    imgs = [Image.open(str(p)).convert("RGBA") for _s, p in frames]
    imgs[-1].save(str(OUT), format="ICO",
                  sizes=[(s, s) for s, _p in frames],
                  append_images=imgs[:-1])
    print("\n已写出: %s" % OUT)

    with Image.open(str(OUT)) as ico:
        got = sorted(ico.ico.sizes())
        print("  内含尺寸: %s" % (got,))
        expect = sorted((s, s) for s in SIZES)
        missing = [s for s in expect if s not in got]
        if missing:
            print("  [!] 缺档位: %s" % (missing,))
            return 1
        ico.size = (16, 16)
        px16 = ico.convert("RGBA").load()
        print("  16x16 角落 alpha=%d (期望 0)  居中 alpha=%d (期望 255)"
              % (px16[0, 0][3], px16[8, 8][3]))
        if px16[0, 0][3] != 0 or px16[8, 8][3] != 255:
            print("  [!] 16x16 不对劲")
            return 1
        print("  [OK] 图标正常")

    icon = QIcon(str(OUT))
    ok = not icon.isNull() and len(icon.availableSizes()) >= 4
    print("  QIcon 可加载: %s, 档位 %d 个" % (ok, len(icon.availableSizes())))

    # 出一张多尺寸预览图, 方便肉眼确认小尺寸下还看得清
    prev = ROOT / ".icon_preview.png"
    sizes_show = [16, 20, 24, 32, 48, 64, 128, 256]
    H = 300
    W = sum(sizes_show) + 12 * (len(sizes_show) + 1)
    canvas = Image.new("RGB", (W, H), (240, 240, 244))
    x = 12
    with Image.open(str(OUT)) as ic:
        for s in sizes_show:
            ic.size = (s, s)
            im = ic.convert("RGBA")
            canvas.paste(im, (x, 22), im)
            x += s + 12
    canvas.save(str(prev))
    print("  预览: %s" % prev)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
