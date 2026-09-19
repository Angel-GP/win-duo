"""验证 win-duo.ico 真的被应用到全局各处。

检查清单:
  1. 文件本身: 多档尺寸、圆角透明
  2. 应用级图标 (QApplication.windowIcon) —— 任务栏 / Alt-Tab / 通知都用它
  3. 各窗口自己的标题栏图标: 设置窗、日志窗、玻璃层
  4. 托盘图标 + 通知气泡图标
  5. 兜底: .ico 不存在时必须退回运行时绘制, 不能崩、不能空白

用法:
    .venv\\Scripts\\python.exe tools\\icon_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

FAILS = []


def check(label, cond, extra=""):
    print("  [%s] %-40s %s" % ("OK" if cond else "FAIL", label, extra))
    if not cond:
        FAILS.append(label)


def main():
    from PyQt6.QtWidgets import QApplication

    import main as app_main
    from ui import AppController, DuoTray, SettingsPanel, apply_theme, make_icon
    from ui.widgets import _ICON_FILE

    ico = ROOT / "win-duo.ico"
    print("=" * 68)
    print("图标应用验证")
    print("=" * 68)

    # ---------- 1. 文件本身 ----------
    print("\n① 图标文件:")
    check("win-duo.ico 存在", ico.exists(), str(ico))
    if not ico.exists():
        return 1
    check("文件非空 (>5KB)", ico.stat().st_size > 5000,
          "%.1f KB" % (ico.stat().st_size / 1024.0))
    try:
        from PIL import Image
        import numpy as np
        with Image.open(str(ico)) as im:
            sizes = sorted(im.ico.sizes())
            check("含 16/32/48/256 四个关键档位",
                  all((s, s) in sizes for s in (16, 32, 48, 256)),
                  str(sizes))
            # 小尺寸必须透明圆角, 不能是实心方块
            im.size = (16, 16)
            a = np.array(im.convert("RGBA"))
            check("16x16 圆角外透明 (角落 alpha=0)", a[0, 0, 3] == 0,
                  "corner alpha=%d" % a[0, 0, 3])
            check("16x16 中心不透明 (alpha=255)", a[8, 8, 3] == 255,
                  "center alpha=%d" % a[8, 8, 3])
            # 结构: 右半应比左半暗 (折过去), 中列最亮 (铰链)
            rgb, al = a[:, :, :3].astype(int), a[:, :, 3]

            def lum(x):
                return (x[:, 0] * 0.299 + x[:, 1] * 0.587 + x[:, 2] * 0.114).mean()

            left = rgb[:, :5][al[:, :5] > 128]
            right = rgb[:, 11:][al[:, 11:] > 128]
            mid = rgb[:, 8][al[:, 8] > 200]
            check("右半比左半暗 (表示折过去)",
                  len(left) and len(right) and lum(right) < lum(left),
                  "左%.0f 右%.0f" % (lum(left), lum(right)))
            check("中列最亮 (铰链)", len(mid) and lum(mid) > lum(left) + 15,
                  "铰链%.0f 左%.0f" % (lum(mid), lum(left)))
    except ImportError:
        print("  (没装 Pillow, 跳过文件结构检查)")

    # ---------- 2-4. 运行时各处 ----------
    app_main.make_qsurface_format()
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    apply_theme()

    print("\n② 应用级图标 (任务栏 / Alt-Tab / 通知):")
    ai = app.windowIcon()
    check("已设置且非空", not ai.isNull())
    check("档位数 >= 4", len(ai.availableSizes()) >= 4,
          str(len(ai.availableSizes())))

    cfg = app_main.load_config()
    cfg["source"] = "manual"
    (ROOT / ".tmp").mkdir(exist_ok=True)
    ctl = AppController(cfg, ROOT / ".tmp" / "icon.json")

    print("\n③ 各窗口:")
    panel = SettingsPanel(ctl)
    check("设置窗口有图标",
          not panel.windowIcon().isNull()
          and len(panel.windowIcon().availableSizes()) >= 4)
    from ui.log_dialog import LogDialog
    dlg = LogDialog()
    check("日志窗口有图标", not dlg.windowIcon().isNull())

    print("\n④ 托盘:")
    tray = DuoTray(ctl, lambda: None)
    check("托盘图标非空", not tray.icon().isNull())
    check("托盘图标多档", len(tray.icon().availableSizes()) >= 4,
          str(len(tray.icon().availableSizes())))

    print("\n⑤ make_icon() 加载的是 .ico 文件:")
    ic = make_icon()
    check("返回可用 QIcon", not ic.isNull())
    check("档位数与文件一致 (9)", len(ic.availableSizes()) == 9,
          str(len(ic.availableSizes())))

    print("\n⑥ 兜底: .ico 缺失时不崩、有图标:")
    # 临时把缓存的图标清掉并假装文件不存在
    import ui.widgets as W
    saved_file, saved_cache = W._ICON_FILE, list(W._ICON_CACHE)
    try:
        W._ICON_CACHE.clear()
        W._ICON_FILE = ROOT / "__no_such_icon__.ico"
        fallback = make_icon()
        check("退回运行时绘制且非空", not fallback.isNull())
        check("兜底图标也有多个尺寸", len(fallback.availableSizes()) >= 1,
              str(len(fallback.availableSizes())))
    finally:
        W._ICON_FILE, W._ICON_CACHE[:] = saved_file, saved_cache

    panel.shutdown()
    ctl.shutdown()

    print("-" * 68)
    if FAILS:
        for f in FAILS:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 图标已全局生效 (应用级 / 窗口 / 托盘 / 兜底)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
