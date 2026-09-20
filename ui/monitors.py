"""显示器枚举与选择。

选择结果同时记 name 和 index:
  - name 是首选依据 (换显示器顺序后 index 会错位, name 不会)
  - index 作为兜底 (极少数情况下 QScreen.name() 为空)
"""
from PyQt6.QtWidgets import QApplication


def screens():
    app = QApplication.instance()
    return list(app.screens()) if app is not None else []


def primary():
    app = QApplication.instance()
    return app.primaryScreen() if app is not None else None


def label(screen, index=None):
    g = screen.geometry()
    parts = [screen.name() or ("显示器 %d" % (index if index is not None else 0)),
             "%dx%d" % (g.width(), g.height())]
    if screen is primary():
        parts.append("主屏")
    try:
        ratio = screen.devicePixelRatio()
        if abs(ratio - 1.0) > 1e-6:
            parts.append("缩放 %.2gx" % ratio)
    except Exception:  # noqa: BLE001
        pass
    return "  ".join(parts)


def resolve(cfg):
    """按 config 里的 screen_name / screen_index 找出要用哪块屏。"""
    alls = screens()
    if not alls:
        return None
    want = cfg.get("screen_name")
    if want:
        for s in alls:
            if s.name() == want:
                return s
    try:
        idx = int(cfg.get("screen_index", 0))
    except (TypeError, ValueError):
        idx = 0
    if 0 <= idx < len(alls):
        return alls[idx]
    return primary()


def index_of(screen):
    for i, s in enumerate(screens()):
        if s is screen:
            return i
    return 0


def region_for(screen):
    """该屏幕的截屏区域 (物理像素)。

    mss 的 `grab(region)` 要求四个字段**同一坐标系** (都是物理像素), 而
    `QScreen.geometry()` 给的是**逻辑坐标**。原来只把 width/height 乘了 dpr,
    left/top 仍是逻辑值 —— 主屏在原点 (0,0) 时看不出来, 但**带缩放的副屏**
    (left/top 非零) 上截屏区域会整体偏移。这里连 left/top 一起换算。
    (DXGI 路径按 output 抓整屏、不看 region, 所以只有退回 mss 时才暴露。)
    """
    g = screen.geometry()
    dpr = screen.devicePixelRatio()
    return {"left": int(g.x() * dpr), "top": int(g.y() * dpr),
            "width": max(1, int(g.width() * dpr)),
            "height": max(1, int(g.height() * dpr))}
