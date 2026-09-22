"""控件兼容层。

优先用 qfluentwidgets (Fluent Design, Windows 11 观感); 没装就退回普通 Qt
控件 + 一份手写 QSS。这样界面代码只写一遍, 也不会因为少一个依赖就打不开。

强制走回退路径 (测试用):  设环境变量 WIN_DUO_NO_FLUENT=1
"""
import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QRegularExpression, pyqtSignal
from PyQt6.QtGui import QIcon, QRegularExpressionValidator
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                             QLineEdit as _QLineEdit, QPlainTextEdit, QPushButton,
                             QVBoxLayout, QWidget)

# 项目根要在 sys.path 上 (打包后 paths.py 被收进同一个包)
if __package__:
    from paths import resource_file as _resource_file
else:  # pragma: no cover - 直接用脚本跑这个文件时
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from paths import resource_file as _resource_file

_FORCE_FALLBACK = os.environ.get("WIN_DUO_NO_FLUENT", "") not in ("", "0", "false")

ACCENT = "#4f7cff"

FLUENT = False
if not _FORCE_FALLBACK:
    try:
        # qfluentwidgets 在 import 时**无条件** `print(ALERT)` 打一行
        # "QFluentWidgets Pro is now released..." 的广告 (见其 common/config.py,
        # 没有关闭开关)。首次 import 时把 stdout 临时挡掉, 吞掉这条广告 ——
        # 只挡 stdout, stderr 不动, 真报错照样能看到。
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget,
                                        FluentIcon, LineEdit, PlainTextEdit,
                                        PrimaryPushButton, PushButton,
                                        SegmentedWidget, StrongBodyLabel,
                                        SubtitleLabel, Theme, ToolButton,
                                        TransparentPushButton, setTheme,
                                        setThemeColor)
            from qfluentwidgets import SwitchButton as _FluentSwitch
            from qfluentwidgets import ComboBox as _FluentCombo

        class ComboBox(_FluentCombo):
            """Fluent 的 addItem 签名是 (text, icon, userData), Qt 是 (text, userData)。
            这里统一成 Qt 那套 —— 否则 userData 会被当成 icon 吃掉,
            currentData() 全是 None, 下拉选择静默失效。"""

            def addItem(self, text, userData=None, icon=None):
                return super().addItem(text, icon, userData)

        class SwitchButton(_FluentSwitch):
            """Fluent 的开关只发 checkedChanged, 普通 QCheckBox 发 toggled。
            统一成 checkedChanged, 界面代码就不用管用的是哪一套。"""

        FLUENT = True
    except Exception as _exc:  # noqa: BLE001
        print("[ui] 未启用 Fluent 组件 (%s), 退回普通 Qt" % _exc)

if not FLUENT:
    class Theme:                       # noqa: N801
        LIGHT = DARK = AUTO = 0

    def setTheme(*_a, **_k):
        pass

    def setThemeColor(*_a, **_k):
        pass

    class _AnyIcon:
        def __getattr__(self, _name):
            return QIcon()

    FluentIcon = _AnyIcon()

    class CardWidget(QFrame):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setObjectName("winDuoCard")

    class SwitchButton(QCheckBox):
        """回退版开关: 用复选框, 但对外仍提供 checkedChanged / setOnText。"""

        checkedChanged = pyqtSignal(bool)

        def __init__(self, parent=None):
            super().__init__(parent)
            self.toggled.connect(self.checkedChanged)

        def setOnText(self, _text):      # noqa: N802  (对齐 qfluentwidgets)
            pass

        def setOffText(self, _text):     # noqa: N802
            pass

    class ComboBox(QComboBox):
        pass

    LineEdit = _QLineEdit
    PlainTextEdit = QPlainTextEdit
    PushButton = QPushButton
    PrimaryPushButton = QPushButton
    TransparentPushButton = QPushButton
    ToolButton = QPushButton
    SubtitleLabel = QLabel
    StrongBodyLabel = QLabel
    BodyLabel = QLabel
    CaptionLabel = QLabel


class SegmentBar(QWidget):
    """分段选择条 —— 统一 Fluent 与回退两套 API。

    Fluent 的 SegmentedWidget 用 routeKey, QComboBox 用 index, 这里包一层,
    对外只有 add(text, key) / set_current(key) / current() / changed 信号。
    """

    changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys = []
        self._loading = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        if FLUENT:
            self._inner = SegmentedWidget(self)
            self._inner.currentItemChanged.connect(self._on_fluent)
        else:
            self._inner = ComboBox(self)
            self._inner.currentIndexChanged.connect(self._on_combo)
        lay.addWidget(self._inner)

    def add(self, text, key, icon=None):
        self._keys.append(key)
        if FLUENT:
            self._inner.addItem(key, text, onClick=lambda: None, icon=icon)
        else:
            self._inner.addItem(text, key)

    def set_current(self, key):
        if key not in self._keys:
            return
        self._loading = True
        try:
            if FLUENT:
                self._inner.setCurrentItem(key)
            else:
                self._inner.setCurrentIndex(self._keys.index(key))
        finally:
            self._loading = False

    def current(self):
        if FLUENT:
            cur = self._inner.currentRouteKey()
            return cur if cur in self._keys else (self._keys[0] if self._keys else None)
        idx = self._inner.currentIndex()
        return self._inner.itemData(idx)

    def _on_fluent(self, key):
        if not self._loading:
            self.changed.emit(key)

    def _on_combo(self, idx):
        if not self._loading:
            self.changed.emit(self._inner.itemData(idx))


def apply_theme():
    """全局主题 + 应用级图标。

    图标在这里一并设掉: `QApplication.setWindowIcon` 管的是**应用级**图标,
    任务栏、Alt-Tab、以及 Windows 的通知气泡都用它。各个窗口自己的
    `setWindowIcon` 只管自己那个窗口的标题栏, 两者都要设。
    """
    # 应用级图标 (与主题无关, 所以放在分支之前)
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(make_icon())
            # Windows 任务栏按"应用用户模型 ID"归组, 不设的话它会把 Python
            # 解释器自己的图标显示出来, 而不是我们的。
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "win-duo.floating-glass.1")
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        print("[ui] 设置应用图标失败: %s" % exc)

    if FLUENT:
        setTheme(Theme.AUTO)
        setThemeColor("#4f7cff")
        return True
    app = None
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
    except Exception:  # noqa: BLE001
        pass
    if app is not None:
        app.setStyleSheet(FALLBACK_QSS)
    return False


FALLBACK_QSS = """
QWidget { font-family: "Microsoft YaHei UI","Segoe UI",sans-serif; font-size: 13px; }
QFrame#winDuoCard {
    background: palette(base);
    border: 1px solid rgba(128,128,128,0.28);
    border-radius: 10px;
}
QLabel#hint { color: rgba(128,128,128,1); }
QLabel#title { font-size: 17px; font-weight: 600; }
QLabel#value { color: rgba(128,128,128,1); min-width: 56px; }
QPushButton {
    padding: 6px 14px; border-radius: 6px;
    border: 1px solid rgba(128,128,128,0.35);
    background: palette(button);
}
QPushButton:hover { border-color: %(accent)s; }
QPushButton:checked { background: %(accent)s; color: white; border-color: %(accent)s; }
QComboBox, QLineEdit {
    padding: 5px 9px; border-radius: 6px;
    border: 1px solid rgba(128,128,128,0.35);
}
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: rgba(128,128,128,0.3); }
QSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 14px; margin: -6px 0; border-radius: 7px; background: %(accent)s;
}
""" % {"accent": ACCENT}


def card_layout(card, margins=(18, 14, 18, 14), spacing=10):
    """给卡片装一个纵向布局并返回它。"""
    lay = QVBoxLayout(card)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return lay


#: 只允许"数字 + 一个小数点"的键入内容。**故意不限制数值范围** ——
#: QDoubleValidator 会在打字途中就拒掉超范围的中间态 (比如范围 30~89 时
#: 想输 50, 第一个字符 "5" 就被拒), 根本没法用。范围在提交时再夹。
_NUM_RE = QRegularExpression(r"^\d{0,4}(\.\d{0,3})?$")
#: 允许负号的版本。**只有下限 < 0 的字段才用它** (比如"重绘上限"的 -1 = 不限速):
#: 原来的正则没有 `-`, 于是输入框里根本敲不出负号 —— 用户想填 -1 却填不了,
#: 只能看着默认值, 以为"这个框坏了"。
_NUM_RE_NEG = QRegularExpression(r"^-?\d{0,4}(\.\d{0,3})?$")


class NumberField(QWidget):
    """纯键盘输入的数值框 —— 没有上下箭头, 也不能拖, 只能打字。

    回车或失焦时提交, 提交时按 [lo, hi] 夹紧并回写规范化后的文本。
    值真的变了才发 changed, 所以程序化 setValue 不会引起无谓的写盘。
    """

    changed = pyqtSignal(float)

    def __init__(self, lo, hi, decimals=0, unit="", width=96,
                 show_range=False, parent=None):
        super().__init__(parent)
        self._lo, self._hi = float(lo), float(hi)
        self._dec = int(decimals)
        self._unit = unit
        self._value = float(lo)

        self.edit = LineEdit(self)
        # 下限为负的字段允许输入负号 (见 _NUM_RE_NEG 的说明)
        re_src = _NUM_RE_NEG if self._lo < 0 else _NUM_RE
        self.edit.setValidator(QRegularExpressionValidator(re_src, self))
        self.edit.setFixedWidth(width)
        self.edit.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
        self.edit.editingFinished.connect(self._commit)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.edit)

        # 单位 + 允许范围。范围必须写出来 —— 输入框本身看不出能填多少,
        # 超范围会被静默夹紧, 不提示的话用户只会觉得"填了没用"。
        tail = self._range_text() if show_range else unit
        if tail:
            lab = CaptionLabel(tail)
            lab.setObjectName("hint")
            lay.addWidget(lab)
        # 末尾留伸缩: 否则标签会被拉伸到整行最右边, 跟输入框脱开老远
        # (实测 "%" 被拉到 238px 宽, 看起来完全不像它的单位)
        lay.addStretch(1)

    def _range_text(self):
        def fmt(v):
            return "%g" % v
        text = "%s~%s" % (fmt(self._lo), fmt(self._hi))
        return ("%s %s" % (text, self._unit)).strip()

    # ---------- 取值 / 赋值 ----------
    def value(self):
        return self._value

    def setValue(self, v, emit=True):          # noqa: N802  (跟着 Qt 的命名)
        v = self._clamp(v)
        self._value = v
        self.edit.setText(self._fmt(v))
        if emit:
            self.changed.emit(v)

    def set_range(self, lo, hi):
        """改允许范围 (比如"重截频率"的上限要用实测出来的截屏物理上限)。"""
        self._lo, self._hi = float(lo), float(hi)
        for lab in self.findChildren(CaptionLabel):
            if lab.objectName() == "hint":
                lab.setText(self._range_text())
        self.setValue(self._value, emit=False)

    def setEnabled(self, on):                  # noqa: N802
        super().setEnabled(on)
        self.edit.setEnabled(on)

    # ---------- 内部 ----------
    def _clamp(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = self._lo
        return max(self._lo, min(self._hi, v))

    def _fmt(self, v):
        if self._dec <= 0:
            return "%d" % int(round(v))
        return ("%.*f" % (self._dec, v)).rstrip("0").rstrip(".")

    def _commit(self):
        raw = self.edit.text().strip()
        # 只有一个负号 / 空 / 只有小数点 = 打字中间态, 保留原值 (别当成 0)
        if raw in ("", ".", "-"):
            self.edit.setText(self._fmt(self._value))
            return
        try:
            v = float(raw)
        except ValueError:
            v = self._value
        v = self._clamp(v)
        self.edit.setText(self._fmt(v))        # 顺手把 "007"/"3.50" 规范化
        if abs(v - self._value) > 1e-9:
            self._value = v
            self.changed.emit(v)


def row(*widgets, spacing=10, stretch_last=False):
    """横向排一行, 返回 (容器, 布局)。"""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for i, w in enumerate(widgets):
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w, 1 if (stretch_last and i == len(widgets) - 1) else 0)
    return box, lay


#: 程序图标文件 (内含 9 档尺寸)。
#: **只读资源** —— 打包后它在 PyInstaller 的解包目录里, 所以走 resource_file。
_ICON_FILE = _resource_file("win-duo.ico")

#: 加载过一次就缓存 —— 托盘菜单、通知、每个窗口都会要它, 没必要反复读盘。
_ICON_CACHE = []


def make_icon(size=64):
    """程序图标。

    **优先用 win-duo.ico** (含 16~256 共 9 档)。
    多分辨率的好处: Windows 在托盘、任务栏、资源管理器、Alt-Tab 各取所需,
    不用它自己硬缩 —— 硬缩出来的小图标又糊又脏。
    文件不在 (比如刚 clone 还没生成) 就**退回运行时绘制**, 保证界面不会因为
    少一个文件就崩或者显示空白图标。
    """
    if _ICON_CACHE:
        return _ICON_CACHE[0]
    if _ICON_FILE.exists():
        icon = QIcon(str(_ICON_FILE))
        if not icon.isNull():
            _ICON_CACHE.append(icon)
            return icon
        print("[ui] 图标文件读不出来, 退回运行时绘制: %s" % _ICON_FILE)
    icon = _draw_icon(size)
    _ICON_CACHE.append(icon)
    return icon


def _draw_icon(size=64):
    """运行时绘制的兜底图标 (与 .ico 同款设计: 两块玻璃面板 + 中间铰链)。"""
    from PyQt6.QtCore import QPointF, QRectF
    from PyQt6.QtGui import (QBrush, QColor, QLinearGradient, QPainter,
                             QPen, QPixmap)

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    tiny = size <= 20
    small = size <= 32

    # 圆角方底
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor(96, 165, 250))
    grad.setColorAt(1.0, QColor(67, 56, 202))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(QRectF(size * 0.055, size * 0.055,
                             size * 0.89, size * 0.89),
                      size * 0.235, size * 0.235)

    # 右面板 (暗, 折过去了)
    rg = QLinearGradient(QPointF(size * 0.55, size * 0.35),
                         QPointF(size * 0.82, size * 0.63))
    rg.setColorAt(0.0, QColor(191, 219, 254, 120))
    rg.setColorAt(1.0, QColor(147, 179, 235, 95))
    p.setBrush(QBrush(rg))
    p.drawRoundedRect(QRectF(size * 0.545, size * 0.375,
                             size * 0.27, size * 0.25),
                      size * 0.055, size * 0.055)

    # 左面板 (亮)
    lg = QLinearGradient(QPointF(size * 0.18, size * 0.31),
                         QPointF(size * 0.47, size * 0.69))
    lg.setColorAt(0.0, QColor(255, 255, 255, 210))
    lg.setColorAt(1.0, QColor(219, 234, 254, 170))
    p.setBrush(QBrush(lg))
    p.drawRoundedRect(QRectF(size * 0.185, size * 0.315,
                             size * 0.287, size * 0.37),
                      size * 0.055, size * 0.055)

    # 铰链
    hw = max(size * 0.052, 3.0 if tiny else 2.0)
    hy0, hy1 = (size * 0.37, size * 0.63) if tiny else (size * 0.295, size * 0.705)
    p.setPen(QPen(QColor(255, 255, 255, 245), hw,
                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(size * 0.5, hy0), QPointF(size * 0.5, hy1))

    # 高光 (小尺寸跳过, 否则只会变成脏点)
    if not small:
        p.setPen(QPen(QColor(255, 255, 255, 130), max(1.0, size * 0.012),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(size * 0.23, size * 0.33),
                   QPointF(size * 0.42, size * 0.33))
    p.end()
    return QIcon(pm)
