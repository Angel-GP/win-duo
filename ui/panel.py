"""设置窗口。

设计:
  - **常用的一眼可见, 其余收进「高级设置」**。
  - **数值一律用输入框直接填** (不用拖滑块), 浓度按百分比填。
  - 原来控制台快捷键能做的事, 这里都有对应按钮; 控制台功能键现在只在
    「键盘」这个角度源下才响应。
  - 改动即时生效并自动落盘, 所以没有"保存"按钮 —— 托盘程序里忘点保存
    丢配置太容易发生。

界面组件走 ui/widgets.py 的兼容层: 装了 qfluentwidgets 就是 Fluent Design,
没装就退回普通 Qt + 一份 QSS。
"""
from pathlib import Path
import time

from PyQt6.QtCore import (QEvent, QEasingCurve, QPropertyAnimation, QRectF,
                          Qt, QThread, QTimer, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import QColor, QPainter, QPen, QTransform
from PyQt6.QtWidgets import (QApplication, QDialog, QFileDialog, QPushButton,
                             QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from angles.hub import LABELS

from . import autostart, monitors
from .log_dialog import LogDialog
from .widgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox, LineEdit,
                      NumberField, PrimaryPushButton, SegmentBar, StrongBodyLabel,
                      SwitchButton, TransparentPushButton, card_layout, make_icon, row)

SOURCE_ORDER = ("camera", "serial", "manual")
#: **主窗「角度源」里列出来的源。**
#: 不含 `manual` —— 键盘手动不是日常使用的角度源, 而是"没接摄像头/陀螺仪时
#: 的调试手段", 所以它的入口收进「高级设置 -> 调试」里的「键盘模式」按钮,
#: 主窗只留真正常用的摄像头 / ESP32。
PANEL_SOURCES = ("camera", "serial")
OUTSIDE = (("black", "纯黑 (原版)"), ("backdrop", "背景图兜底 (无黑场)"))

#: 退出时还没结束、又不能在原生调用里被打断的线程寄存处。
#: 见 `SettingsPanel.shutdown`: 留着引用 → Qt 不析构 → 不 abort;
#: 它们都是 daemon 线程, 进程退出时由系统回收。
_ORPHANED = []


class _CurPageStack(QStackedWidget):
    """`QStackedWidget`, 但 **sizeHint 只按"当前页"算**。

    Qt 原版的 `sizeHint()` 取的是**所有子页里最大**的那个 —— 于是内容矮的页
    (「启动与标定」只有 ~130px) 会被最高那页 (~300px) 撑到同样高, 底部留一大
    片空白。

    原来为了消掉这个空白用了 `setFixedHeight(当前页sizeHint)`, 但那是**硬**
    约束: 一旦标签因为换行 / DPI / 字体需要更高, 页面被钉死在旧高度 ->
    **控件互相挤压、遮挡**。

    这里改成只覆盖 sizeHint (软约束): 高度跟着当前页走, 空间不够时 Qt 仍会
    把弹窗撑高, 不会再压扁内容。
    """

    def sizeHint(self):                      # noqa: N802
        cur = self.currentWidget()
        if cur is None:
            return super().sizeHint()
        s = cur.sizeHint()
        # 宽度也按当前页 (免得窄页被宽页撑出大片右边空白)
        return s

    def minimumSizeHint(self):               # noqa: N802
        cur = self.currentWidget()
        if cur is None:
            return super().minimumSizeHint()
        return cur.minimumSizeHint()

#: 数值输入项: (config 键, 标签, 最小, 最大, 步进, 单位, 小数位)
INPUTS = (
    ("camera_scale", "灵敏度", 0.1, 3.0, 0.1, "", 2),
    ("max_tilt_deg", "最大转角", 30, 89, 1, "°", 0),
    ("eye_dist_h", "眼距", 0.5, 6.0, 0.1, "×屏高", 1),
    ("blur_spread", "模糊强度", 0.0, 1.5, 0.01, "", 2),
    # 重截频率: **-1 = 不设限** (默认) —— 自动用当前选中显示器的刷新率。
    # 上限由 _refresh_refresh_max() 动态设成那块屏的刷新率 (本机 165),
    # 120 只是面板显示前的保守占位。
    ("refresh_hz", "重截频率", -1, 120, 1, "Hz", 0),
    # 重绘上限: **-1 = 不限制** (默认; 每 tick 都画 ≈62.5 FPS, 动画最顺)。
    # 上限 **62 而不是 120** —— tick 固定 16ms, 重绘的物理上限就是 1000/16
    # ≈ 62.5/s, 填更大也不会更快 (120 是假的可用值)。
    ("render_fps", "重绘上限", -1, 62, 1, "帧/秒", 0),
)


class _RotatingScreenButton(TransparentPushButton):
    """一个带**旋转动画**的小屏幕图标按钮: 点一下把铰链方向反转。

    用途: 反着用笔记本时 (屏幕朝下 / 摄像头倒装), 铰链相对画面就跑到了**上边**,
    这时折叠动画的方向是反的。点这个按钮把铰链换到另一边修正过来。

    为什么做成旋转图标而不是普通按钮: "铰链在哪一边"是**空间关系**, 用文字
    ("底边/顶边")要读, 而一个**转过去的屏幕图标**能一眼看懂。点击时图标平滑
    转 180°, 用户立刻明白"铰链换边了"。

    ⚠️ **必须继承 TransparentPushButton (而不是裸 QPushButton)** —— 面板上另外
    两个按钮 (标定/反转开合) 用的都是它。继承裸 QPushButton 会丢掉 Fluent 的
    透明按钮样式, 三个按钮**材质不一致**, 一眼就看得出这一个"不是一伙的"。
    """

    #: 图标占的宽度 (左侧)。文字从它后面开始, 见 paintEvent。
    _ICON_W = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        # 文字**不加前导空格** —— 缩进由 paintEvent 里自己排 (空格会因为居中
        # 而和图标错位, 看起来就是"文字偏右")。
        self.setText("反转铰链方向")
        self.setToolTip("反着用笔记本时点这里: 把铰链从屏幕底边换到顶边")
        self._angle = 0.0
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(320)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def sizeHint(self):                            # noqa: N802
        # 在"文字宽度"基础上多留出图标的位置, 否则图标会压到文字上。
        s = super().sizeHint()
        return s.__class__(s.width() + self._ICON_W, s.height())

    # 动画驱动这个属性 (0 -> 180)
    def _get_angle(self):
        return self._angle

    def _set_angle(self, v):
        self._angle = float(v)
        self.update()

    angle = pyqtProperty(float, _get_angle, _set_angle)

    def set_flipped(self, flipped, animate=False):
        """设成"铰链在上边/在下边"状态。`animate=True` 时平滑转过去。"""
        target = 180.0 if flipped else 0.0
        if animate and abs(self._angle - target) > 1:
            self._anim.stop()
            self._anim.setStartValue(self._angle)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._set_angle(target)

    def paintEvent(self, ev):                      # noqa: N802
        """画按钮: **左侧旋转的小屏幕图标 + 紧跟其后的文字**。

        不用 QPushButton 自带的文字绘制, 因为它的对齐 (居中) 会和图标打架 ——
        实测就是"图标在左、文字居中", 看着像文字**偏右**、和图标分了家。
        这里自己排: 图标在最左, 文字**紧接图标**开始, 两者是一个整体。
        """
        # 先把文字设为空, 让父类只画**背景/边框** (保留 Fluent 的透明材质),
        # 文字由下面自己画 —— 否则会出现"两份文字"。
        real = self.text()
        if real:
            self.setText("")
            try:
                super().paintEvent(ev)
            finally:
                self.setText(real)
        else:
            super().paintEvent(ev)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        col = self.palette().color(self.foregroundRole())
        h = self.height()
        # 图标: 靠左, 垂直居中
        side = max(8, min(h - 12, 16))
        x = 8.0
        y = (h - side * 0.78) / 2.0 - side * 0.11
        # 绕图标中心旋转 (动画角度)
        p.save()
        p.translate(x + side / 2.0, y + side * 0.39)
        p.rotate(self._angle)
        p.translate(-(x + side / 2.0), -(y + side * 0.39))
        rect = QRectF(x, y, side, side * 0.78)
        pen = QPen(col)
        pen.setWidthF(1.3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 1.5, 1.5)
        # 铰链那条粗线: **加粗的一边就是铰链所在边** —— 旋转后一眼看出它换到
        # 了上边还是下边。
        pen2 = QPen(col)
        pen2.setWidthF(2.4)
        p.setPen(pen2)
        p.drawLine(int(rect.left()), int(rect.top()),
                   int(rect.right()), int(rect.top()))
        p.restore()
        # 文字: 紧接图标右侧, 垂直居中
        if real:
            p.setPen(col)
            p.setFont(self.font())
            tx = x + side + 5.0
            ty = (h + p.fontMetrics().ascent() - p.fontMetrics().descent()) / 2.0
            p.drawText(QRectF(tx, 0, max(0.0, self.width() - tx - 4), h),
                       int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter), real)


class CameraScanThread(QThread):
    """后台扫描摄像头 —— 打开设备要 1~2 秒, 不能卡住界面。

    **必须有总时间预算。** 程序退出时如果这个线程还跑在原生 cv2/MediaFoundation
    调用里, 进程拆除会直接 **ACCESS_VIOLATION (0xC0000005)** —— 偶发, 4 次里
    中 2 次, 而且不是 Python 异常、抓不到。所以:
      - 扫描总时长封顶 `budget_sec`, 保证它一定会在可等待的时间内结束;
      - `stop()` 在每个 index 之间生效 (open_camera 内部阻塞, 打断不了);
      - 退出时 `wait()` 的时限要大于这个预算。
    """

    scanned = pyqtSignal(list)

    #: 扫描总预算 (秒)。给 3 个 index 各留一次 `open_camera` 的机会 ——
    #: 它是同步阻塞的, 中途打断不了, 本机实测最坏一次要 ~20 秒
    #: (DSHOW 那个虚拟摄像头打开失败时会卡很久)。
    BUDGET_SEC = 20.0

    def __init__(self, parent=None, budget_sec=None):
        super().__init__(parent)
        self._stop = False
        self.budget_sec = float(budget_sec if budget_sec is not None
                                else self.BUDGET_SEC)

    def stop(self):
        self._stop = True

    def run(self):
        found = []
        deadline = time.time() + self.budget_sec
        try:
            from angles.camera import open_camera
        except Exception as exc:  # noqa: BLE001
            print("[scan] 无法导入取流模块: %s" % exc)
            self.scanned.emit(found)
            return
        for idx in range(3):
            if self._stop or time.time() > deadline:
                break
            try:
                cap, backend = open_camera(idx, "auto")
            except Exception:  # noqa: BLE001
                continue
            try:
                ok, frame = cap.read()
                h, w = frame.shape[:2] if frame is not None else (0, 0)
                found.append({"index": idx, "backend": backend,
                              "width": w, "height": h})
            finally:
                cap.release()
        self.scanned.emit(found)


class SettingsPanel(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.cfg = controller.cfg
        self._loading = False
        self._scan = None
        self._scan_result = None
        self._inputs = {}

        self.setWindowTitle("win-duo 设置")
        self.setWindowIcon(make_icon())
        # 可自由缩放 / 最大化: 只给一个最小尺寸, 不锁死宽高。
        # 控件都用"标签列 + 可拉伸控件"的自适应布局, 拉宽会自然铺开。
        # 初始高度先给个小的, 首次 show 时由 _fit_height 贴合真实内容
        # (避免写死高度导致底部一大片空白)。
        self.setMinimumSize(440, 300)
        self.resize(470, 300)
        self._fitted = False
        self._build()
        # 顺序很重要: 先建控件 -> 用真实配置填充 -> **最后**才接信号。
        # 反过来的话, 填充时每个 setValue/setChecked 都会触发一次"用户改动",
        # 把还没填好的控件值当成用户输入写回配置 (已经因此写坏过 config.json)。
        self.refresh_all()
        self._wire()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(500)

    def _wire(self):
        """把会改配置的信号接上。只在控件被真实配置填好之后调用一次。"""
        self.sw_glass.checkedChanged.connect(self._on_glass_switch)
        self.cmb_screen.currentIndexChanged.connect(self._on_screen)
        self.num_level.changed.connect(self._on_level)
        self.seg_source.changed.connect(self._on_source)
        self.cmb_camera.currentIndexChanged.connect(self._on_camera)
        self.cmb_outside.currentIndexChanged.connect(self._on_effect)
        self.edt_port.editingFinished.connect(self._on_serial)
        self.sw_autocal.checkedChanged.connect(self._on_autocal)
        self.sw_autoglass.checkedChanged.connect(self._on_autoglass)
        self.sw_autostart.checkedChanged.connect(self._on_autostart)
        self.sw_lowmem.checkedChanged.connect(self._on_lowmem)
        self.cmb_capture_backend.currentIndexChanged.connect(
            self._on_capture_backend)
        for box in self._inputs.values():
            box.changed.connect(self._on_effect)

    # ================================================================ 构建
    def _build(self):
        # 主窗只放常用卡片 + 一个"高级设置..."按钮, 保持紧凑、高度固定。
        # 高级设置(那一大堆参数)挪进独立弹窗 `self._adv_dialog`, 点按钮才开 ——
        # 主窗永远不会被撑长。
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_glass_card())
        root.addWidget(self._build_source_card())
        root.addWidget(self._build_advanced_card())
        root.addWidget(self._build_status())
        # **末尾留伸缩**: 否则窗口变高时, 多出来的空间会全塞给"最后那个控件"
        # —— 也就是底部状态行, 把它从一行高 (17px) 拉到 60+px, 白占一大块。
        # 加了 stretch 之后多余空间落到底部空白, 状态行只占它需要的一行。
        root.addStretch(1)

    # ---------------------------------------------------------- 悬浮玻璃
    def _build_glass_card(self):
        card = CardWidget()
        lay = card_layout(card)

        self.lbl_glass_state = CaptionLabel("待机")
        self.sw_glass = SwitchButton()
        self.sw_glass.setOnText("开")
        self.sw_glass.setOffText("关")
        lay.addWidget(row(StrongBodyLabel("悬浮玻璃"), None,
                          self.lbl_glass_state, self.sw_glass, spacing=8)[0])

        self.cmb_screen = ComboBox()
        lay.addWidget(self._labeled("显示器", self.cmb_screen))

        # 浓度: 纯键盘输入百分比, 没有箭头、没有滑块、也没有快捷按钮
        self.num_level = NumberField(0, 100, 0, "%", width=84)
        lay.addWidget(self._labeled("浓度", self.num_level))
        return card

    # ---------------------------------------------------------- 角度源
    def _build_source_card(self):
        card = CardWidget()
        # 存起来: 切源后要强制它重排 (见 _relayout_source_card)
        self._source_card = card
        lay = card_layout(card)

        self.seg_source = SegmentBar()
        # 只列 PANEL_SOURCES (摄像头 / ESP32) —— 键盘手动的入口在调试页。
        for name in PANEL_SOURCES:
            self.seg_source.add(LABELS.get(name, name), name)
        lay.addWidget(row(StrongBodyLabel("角度源"), None, self.seg_source,
                          spacing=8)[0])

        # ── 按角度源切换的"源设置"行 ────────────────────────────────
        # 摄像头模式显示「摄像头 + 扫描」, ESP32 模式显示「串口」——
        # 两者都放在**同一位置**, 用 QStackedWidget 按当前源切换显示。
        # 这样主窗一眼就能看到"当前源该怎么设", 不用去高级设置里翻。
        self.stack_src_setting = QStackedWidget()

        # (a) 摄像头: 选择设备 + 扫描
        self.cmb_camera = ComboBox()
        self.btn_scan = TransparentPushButton("扫描")
        self.btn_scan.clicked.connect(self.start_scan)
        cam_box, _ = row(self.cmb_camera, self.btn_scan, spacing=6,
                         stretch_last=False)
        self.stack_src_setting.addWidget(self._labeled("摄像头", cam_box))

        # (b) ESP32: 串口名 (原来在「高级设置 -> 启动与标定」里, 挪到这里)
        self.edt_port = LineEdit()
        self.edt_port.setPlaceholderText("COM3")
        port_box, _ = row(self.edt_port, spacing=6)
        self.stack_src_setting.addWidget(self._labeled("ESP32 串口", port_box))

        lay.addWidget(self.stack_src_setting)

        # 摄像头相关的快捷操作 (标定基准帧 / 翻转方向) —— 只在摄像头模式有意义。
        # **要藏就藏这一整行 (外层 _labeled 容器)**: 只藏内层的话外层还占着
        # 一行高, ESP32 模式下就会留下一块 43px 的空白 (实测)。
        self.btn_calib = TransparentPushButton("标定基准帧")
        self.btn_calib.clicked.connect(self._quick_calibrate)
        self.btn_flip = TransparentPushButton("反转开合方向")
        self.btn_flip.clicked.connect(self._quick_flip)
        # 「反转铰链方向」: 点一下把铰链从屏幕底边换到顶边 (给"反着用笔记本"
        # 的场景), 再点一下换回来。按钮上画一个**小屏幕图标并做 180° 旋转
        # 动画** —— 一眼就看出"这次会把铰链转到哪一边"。
        self.btn_hinge = _RotatingScreenButton()
        self.btn_hinge.clicked.connect(self._toggle_hinge)
        # 三个按钮**等间距**排一行, 末尾留伸缩把整组推向左。
        #
        # ⚠️ **这一行不要用 `_labeled("", ...)`** —— 那会给它套一个 78px 的
        # **空标签** (+10px 间距), 于是:
        #   1. 按钮组被推到 x=88 -> 看着"太靠右" (用户反馈);
        #   2. 整行 sizeHint 变成 78+10+312=400+, 而卡片可用宽只有 402 ->
        #      **右侧被裁**, "反转铰链方向"末尾几个字被切掉。
        # 这里是"没有标签的一行", 直接放按钮组即可 (左边跟卡片内边距对齐,
        # 反而和上面各行的**控件列**对齐不上 —— 但那本来也不是标签行)。
        ops_inner, ops_lay = row(self.btn_calib, self.btn_flip, self.btn_hinge,
                                 spacing=8)
        ops_lay.addStretch(1)
        self.row_cam_ops = ops_inner          # 显隐用 (见 _refresh_sources)
        lay.addWidget(ops_inner)

        # 当前模式的快捷键提示。
        # **只在摄像头 / ESP32 模式显示** —— 那两模式下热键是"兜底手段"
        # (紧急关闭、标定), 用户需要知道按什么。键盘模式那一大串浓度/调试键
        # 不在这里 (会把主窗撑长), 统一在「高级设置 -> 调试」的键盘模式块里。
        self.lbl_keys = CaptionLabel("")
        self._auto_height_label(self.lbl_keys)
        self.lbl_keys.setObjectName("hint")
        lay.addWidget(self.lbl_keys)

        self.lbl_scan = CaptionLabel("")
        self._auto_height_label(self.lbl_scan)
        lay.addWidget(self.lbl_scan)
        return card

    # ---------------------------------------------------------- 高级设置
    def _build_advanced_card(self):
        """主窗里只放一个入口按钮; 真正的高级设置在独立弹窗里 (见下)。"""
        card = CardWidget()
        lay = card_layout(card, spacing=6)
        self.btn_adv = TransparentPushButton("高级设置...")
        self.btn_adv.clicked.connect(self._open_advanced)
        lay.addWidget(self.btn_adv)
        # 弹窗内容此刻就建好 (控件要在 refresh_all / _wire 之前存在), 但不显示。
        self._build_advanced_dialog()
        return card

    def _build_advanced_dialog(self):
        # 独立弹窗: 分三个标签页, 一次只显示一页 —— 高度天然受控, 不会撑长主窗。
        self._adv_dialog = QDialog(self)
        self._adv_dialog.setWindowTitle("win-duo 高级设置")
        self._adv_dialog.setWindowIcon(make_icon())
        # 可缩放 / 可最大化 (只给最小尺寸, 不锁宽)。
        # **高度最小值要给小**: 之前是 320, 而「启动与标定」页内容只要 ~234px,
        # 于是那一页必然留 ~86px 空白 (setMinimumSize 是硬下限, 缩不下去)。
        # 取 200: 仍能容下最矮那页, 又把空白压掉。
        self._adv_dialog.setMinimumSize(400, 200)
        self._adv_dialog.resize(430, 400)
        # Qt 的 QDialog 默认旗标**不含**最大化/最小化按钮, 要显式加上才能最大化。
        self._adv_dialog.setWindowFlags(
            (self._adv_dialog.windowFlags()
             | Qt.WindowType.WindowMinimizeButtonHint
             | Qt.WindowType.WindowMaximizeButtonHint)
            & ~Qt.WindowType.WindowContextHelpButtonHint)
        body = QVBoxLayout(self._adv_dialog)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(9)

        self.tab_adv = SegmentBar()
        self.tab_adv.add("效果参数", "effect")
        self.tab_adv.add("启动与标定", "launch")
        self.tab_adv.add("调试", "debug")
        # **垂直方向固定**: 标签栏只占一行高。不设的话它会被布局拉伸 ——
        # 实测弹窗里它被拉到 153px (占了弹窗一半), 三个按钮变得又高又空,
        # 看起来就是"UI 异常"。(stack 用 setFixedHeight 后多余空间会全给它。)
        self.tab_adv.setSizePolicy(QSizePolicy.Policy.Preferred,
                                   QSizePolicy.Policy.Fixed)
        body.addWidget(self.tab_adv)

        # 用 _CurPageStack: sizeHint 只按当前页算 (否则矮页被最高页撑出空白)
        self.stack_adv = _CurPageStack()

        # 页面 1: 效果参数 (出界模式、转角、眼距、模糊、重截频率、背景图)
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.setContentsMargins(0, 4, 0, 0)
        l1.setSpacing(8)

        self.cmb_outside = ComboBox()
        for value, text in OUTSIDE:
            self.cmb_outside.addItem(text, value)
        l1.addWidget(self._labeled("出界处理", self.cmb_outside))

        for key, text, lo, hi, step, unit, dec in INPUTS:
            box = NumberField(lo, hi, dec, unit, show_range=True)
            box.setValue(float(self.cfg.get(key, lo)), emit=False)
            self._inputs[key] = box
            l1.addWidget(self._labeled(text, box))

        self.edt_backdrop = LineEdit()
        self.edt_backdrop.setReadOnly(True)
        btn_pick = TransparentPushButton("选择")
        btn_pick.clicked.connect(self._pick_backdrop)
        btn_reset = TransparentPushButton("默认")
        btn_reset.clicked.connect(self._clear_backdrop)
        l1.addWidget(self._labeled("背景图", row(self.edt_backdrop, btn_pick,
                                                btn_reset, spacing=6)[0]))
        self.stack_adv.addWidget(p1)

        # 页面 2: 启动与标定 (串口、开机自启、开玻璃自动标定、开程序自动开玻璃)
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.setContentsMargins(0, 4, 0, 0)
        l2.setSpacing(8)

        # 全部走 _labeled: 标签右对齐在统一的 78px 列, 控件紧跟其后 ——
        # 和「效果参数」「调试」两页一致, 标签列上下对齐, 不再一会儿靠左
        # 一会儿靠右。
        self.sw_autocal = SwitchButton()
        self.sw_autocal.setOnText("开")
        self.sw_autocal.setOffText("关")
        l2.addWidget(self._labeled("自动标定", self.sw_autocal))

        self.sw_autoglass = SwitchButton()
        self.sw_autoglass.setOnText("开")
        self.sw_autoglass.setOffText("关")
        l2.addWidget(self._labeled("自动启动", self.sw_autoglass))

        self.sw_autostart = SwitchButton()
        self.sw_autostart.setOnText("开")
        self.sw_autostart.setOffText("关")
        l2.addWidget(self._labeled("开机自启", self.sw_autostart))
        # 注: 「ESP32 串口」输入框已挪到**主窗**「角度源」下面 —— 那里按当前源
        # 显示对应设置 (摄像头选择 / 串口), 比藏在高级设置里直观得多。
        self.stack_adv.addWidget(p2)

        # 页面 3: 调试 (匹配调试窗、查看日志)
        p3 = QWidget()
        l3 = QVBoxLayout(p3)
        l3.setContentsMargins(0, 4, 0, 0)
        l3.setSpacing(10)

        self.btn_debug = TransparentPushButton("打开匹配调试窗")
        self.btn_debug.clicked.connect(self._quick_debug)
        self.btn_log = PrimaryPushButton("查看运行日志...")
        self.btn_log.clicked.connect(self._open_log_dialog)
        l3.addWidget(self._labeled("特征匹配", self.btn_debug))
        l3.addWidget(self._labeled("运行日志", self.btn_log))

        # 低内存模式: 玻璃层不可见够久就把 GL 窗口销毁, 把显存/交换链还回去。
        # 用 _labeled 和其它项一样左对齐标签 (label_w 和其它行一致, 否则这一行
        # 的"低内存模式"会比上面的"特征匹配/运行日志"更靠左, 看起来是错的)。
        self.sw_lowmem = SwitchButton()
        self.sw_lowmem.setOnText("开")
        self.sw_lowmem.setOffText("关")
        l3.addWidget(self._labeled("低内存模式", self.sw_lowmem))

        # ── 采集后端 ─────────────────────────────────────────────────
        # 三种抓屏方式性能差很多 (实测: wgc ~0.5ms/帧 < dxgi ~1.1ms <<
        # mss ~27ms), 所以默认 auto 让系统挑最优; 想强制某个后端 (排查问题时
        # 对比行为) 就在这里选。**改完立即生效**, 不用重启。
        self.cmb_capture_backend = ComboBox()
        try:
            from render.capture import available_backends
            for label, val in available_backends():
                self.cmb_capture_backend.addItem(label, val)
        except Exception:  # noqa: BLE001
            for val in ("auto", "wgc", "dxgi", "mss"):
                self.cmb_capture_backend.addItem(val, val)
        l3.addWidget(self._labeled("采集后端", self.cmb_capture_backend))
        self.lbl_capture_state = CaptionLabel("")
        self.lbl_capture_state.setObjectName("hint")
        l3.addWidget(self.lbl_capture_state)

        # ── 键盘模式 (调试功能) ──────────────────────────────────────
        # **键盘模式的快捷键本质就是调试功能**: 它只在"键盘手动"角度源下
        # 才注册, 用来在没有摄像头/陀螺仪时手动拉浓度、开调试窗。所以整块
        # 放在调试页 —— 含一个"切到键盘模式"的按钮 + 该模式的全部快捷键。
        l3.addWidget(StrongBodyLabel("键盘模式"))
        self.btn_manual = TransparentPushButton("切到键盘手动")
        self.btn_manual.clicked.connect(self._switch_to_manual)
        self.lbl_manual_state = CaptionLabel("")
        self.lbl_manual_state.setObjectName("hint")
        l3.addWidget(self._labeled("", row(self.btn_manual, self.lbl_manual_state,
                                          spacing=8)[0]))
        # 键盘模式专属的快捷键列表
        self.lbl_keys_manual = CaptionLabel("")
        self._auto_height_label(self.lbl_keys_manual)
        self.lbl_keys_manual.setObjectName("hint")
        self.lbl_keys_manual.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        l3.addWidget(self.lbl_keys_manual)
        # 注: 原来这里还有一组「全部快捷键」(列各模式的键做对照)。它与上面的
        # 「键盘模式」高度重复, 所以删掉 —— 调试页只保留键盘模式这一组。
        # 也**不加 addStretch**: 页面高度要由内容决定 (见 _fit_adv_page 会按
        # 当前页 sizeHint 设固定高); 加了 stretch 反而让 sizeHint 虚高、留白。
        self.stack_adv.addWidget(p3)

        body.addWidget(self.stack_adv)
        self.tab_adv.changed.connect(self._on_adv_tab)
        # 默认停在第一页
        self.stack_adv.setCurrentIndex(0)

    # ---------------------------------------------------------- 状态行
    def _build_status(self):
        self.lbl_status = CaptionLabel("")
        # 单行 + 超出省略号 —— 关掉自动换行, 否则状态一长就折成两三行、占掉
        # 窗口底部一大块高度。文本在 _refresh_status 里按当前宽度 elide。
        self.lbl_status.setWordWrap(False)
        # 竖向固定: 永远只占一行高, 不会被布局拉伸去吃多余空间
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Preferred,
                                      QSizePolicy.Policy.Fixed)
        self.lbl_status.setObjectName("hint")
        return self.lbl_status

    # ================================================================ 小工具
    #: 标签列的固定宽度 (像素)。
    #:
    #: **必须 >= 最宽的那个标签**, 否则右对齐的文字会从左边溢出 —— 表现就是
    #: "这几个字怎么偏左了"。原先写死 64px, 而 4 字标签实测 56px 刚好放得下,
    #: 5 字的"低内存模式"实测 **70px** 就溢出了。
    #: 用一个**统一**宽度 (而不是各算各的) 才能让每行的控件左边缘对齐。
    _LABEL_W = 78

    def _labeled(self, text, widget, label_w=None):
        """一行「右对齐标签 + 控件」, 标签列宽统一 (见 `_LABEL_W`)。"""
        lab = BodyLabel(text)
        lab.setFixedWidth(self._LABEL_W if label_w is None else label_w)
        lab.setAlignment(Qt.AlignmentFlag.AlignRight
                         | Qt.AlignmentFlag.AlignVCenter)
        box, lay = row(lab, widget, spacing=10)
        lay.setStretch(1, 1)
        return box

    @staticmethod
    def _auto_height_label(lbl):
        """让一个 wordWrap 的提示标签**只占它实际需要的高度**。

        为什么要这个: `QLabel` 开了 `wordWrap` 后, `sizeHint()` 会按**最坏
        情况** (整个文本挤在一行) 去估高度, 算出来**虚高** —— 实测两行文字
        只需要 32px, sizeHint 却报 51px。后果有三个, 都是用户看到的:
          - 布局按虚高分配 -> 卡片/sizeHint 一起虚高 -> **顶部/底部空白**;
          - 空间不够时 Qt 又按真实需要压缩 -> 标签被压到 34px -> **文字被裁**;
          - 于是同一个标签高度在 34~51 之间摇摆 -> **看起来"错位/跳动"**。

        这里按"当前宽度下真正需要几行"来设固定高度, 并在宽度变化时重算
        (见 resizeEvent 钩子), 高度就稳定了。
        """
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Preferred,
                          QSizePolicy.Policy.Fixed)

        def fit():
            fm = lbl.fontMetrics()
            avail = max(40, lbl.width())
            text = lbl.text()
            # **空文本就彻底隐藏** —— 否则它仍占一行高 + 布局间距 (实测白白
            # 多出 ~28px 空白, 看着就是"这块区域太大")。
            if not text.strip():
                lbl.setVisible(False)
                return
            lbl.setVisible(True)
            # **自己算行数**, 不用 heightForWidth —— 后者偏保守 (实测 1 行文字
            # 会给 22px, 而 fm.height() 只要 16px), 多出来的就成了空白。
            rows = 0
            for line in (text.splitlines() or [""]):
                w = fm.horizontalAdvance(line)
                rows += max(1, -(-w // avail))      # 向上取整的换行数
            h = max(1, rows) * fm.height() + 2      # +2 余量, 免得最后一行被切
            if lbl.height() != h:
                lbl.setFixedHeight(h)

        lbl._fit_height = fit
        return lbl

    # ================================================================ 刷新
    def refresh_all(self):
        self._loading = True
        try:
            self._refresh_screens()
            self._refresh_sources()
            self._refresh_effect()
            self._refresh_glass()
            self.sw_autocal.setChecked(bool(self.cfg.get("autocal_on_glass_open", True)))
            self.sw_autoglass.setChecked(bool(self.cfg.get("autostart_glass", True)))
            self.sw_autostart.setChecked(autostart.is_enabled())
            self.sw_lowmem.setChecked(bool(self.cfg.get("low_memory_mode", False)))
            self._sync_capture_combo()
            self._refresh_capture_state()
        finally:
            self._loading = False
        self._refresh_status()

    def _refresh_screens(self):
        self.cmb_screen.clear()
        cur = self.controller.screen()
        for i, s in enumerate(monitors.screens()):
            self.cmb_screen.addItem(monitors.label(s, i), i)
            if s is cur:
                self.cmb_screen.setCurrentIndex(self.cmb_screen.count() - 1)

    def _refresh_sources(self):
        # 键盘手动不在主窗这一排里 (它在调试页), 所以当前是 manual 时
        # **不高亮任何一项** —— 硬 set_current("manual") 找不到项会出错。
        active = self.controller.hub.active_name()
        if active in PANEL_SOURCES:
            self.seg_source.set_current(active)
        # 主窗那一行"源设置"跟着当前源切换:
        #   摄像头 -> 摄像头选择 + 扫描 (+ 标定/翻转按钮)
        #   ESP32  -> 串口名
        # 键盘模式两者都用不上, 显示摄像头那页 (无害), 但把摄像头专属按钮藏掉。
        is_cam = (active == "camera")
        self.stack_src_setting.setCurrentIndex(0 if active != "serial" else 1)
        self.row_cam_ops.setVisible(is_cam)
        # 铰链方向按钮的图标状态跟配置同步 (不播动画, 免得每次刷新都转一下)
        if getattr(self, "btn_hinge", None) is not None:
            self.btn_hinge.set_flipped(
                bool(self.cfg.get("flip_hinge", False)), animate=False)
        self._fill_camera_combo()
        self.edt_port.setText(str(self.cfg.get("port", "COM3")))
        self._refresh_hotkeys()
        # **切源后必须强制重排这一块。** 否则会出现这个真 bug:
        # ESP32 -> 摄像头时, 上面那行从"串口输入框"换成"摄像头+扫描", 加上
        # 标定/翻转按钮重新出现、快捷键提示从 1 行变 2 行 —— 内容变高了, 但
        # 布局还按旧尺寸算, 于是**按钮被压扁、甚至被下面的提示遮住**。
        # (实测: 卡片内容底 133 > 卡片高 132 -> 溢出; 按钮高只有 18px 而非 31px。)
        self._relayout_source_card()

    def _relayout_source_card(self):
        """让主窗「角度源」卡片按新内容重新算尺寸。

        `setVisible` 切换 / `QStackedWidget` 换页之后, Qt **不保证**立刻重排;
        必须显式 invalidate + activate, 再让卡片和窗口重新贴合高度。
        """
        card = getattr(self, "_source_card", None)
        stack = getattr(self, "stack_src_setting", None)
        if stack is not None:
            cur = stack.currentWidget()
            if cur is not None:
                cur.updateGeometry()
                lay = cur.layout()
                if lay is not None:
                    lay.invalidate()
                    lay.activate()
        if card is not None:
            card.updateGeometry()
            lay = card.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
        # 内容可能变高 -> 窗口跟着长 (但不超过屏幕, 见 _fit_height)
        self._loading = True
        try:
            self._fit_hint_labels()
            self.updateGeometry()
            self._grow_to_fit()
        finally:
            self._loading = False

    def _grow_to_fit(self):
        """窗口内容变高后, 把它撑到刚好放得下 (不超出屏幕)。

        与 `_fit_height` 的区别: 那个只在**首次显示**跑一次; 这个可以在运行时
        反复调 (切源 / 扫描结果变化), 保证不会出现"内容比窗口高 -> 控件被遮"。
        用户手动拉大过就不动它 (尊重用户)。
        """
        if self.isMaximized():
            return
        try:
            need = self.sizeHint().height()
            scr = self.screen() or QApplication.primaryScreen()
            if scr is not None:
                avail = scr.availableGeometry().height()
                if avail > 100:
                    need = min(need, avail - 40)
            if need > self.height():
                self.resize(self.width(), need)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_hotkeys(self):
        """刷新两处快捷键文案。

        - **主窗 `lbl_keys`**: 只在**摄像头 / ESP32** 模式显示当前可用的热键
          (就是"紧急关闭 + 标定"这种兜底手段)。键盘模式不在这里显示 ——
          那一大串浓度/调试键会撑长主窗, 它们在下面那处。
        - **调试页 `lbl_keys_manual`**: 键盘模式块 (状态 + 该模式全部键)。
        """
        active = self.controller.hub.active_name()
        manual_now = (active == "manual")

        # ---- 键盘模式: 状态 + 该模式的快捷键 ----
        try:
            from .controller import HOTKEY_DEFS
        except Exception:  # noqa: BLE001
            HOTKEY_DEFS = ()

        def spec_of(key):
            return str(self.cfg.get(key, "") or "(未设置)")

        # ---- 主窗: 摄像头/ESP32 才显示 (键盘模式靠调试页那块) ----
        main_lbl = getattr(self, "lbl_keys", None)
        if main_lbl is not None:
            if manual_now:
                main_lbl.setText("")       # 键盘模式不在主窗列 (见调试页)
            else:
                try:
                    rows = self.controller.hotkey_lines()
                except Exception:  # noqa: BLE001
                    rows = []
                main_lbl.setText(
                    "\n".join("  %s  %s" % (k, v.replace("−", "-"))
                              for k, v in rows))

        if getattr(self, "lbl_manual_state", None) is not None:
            self.lbl_manual_state.setText(
                "（当前已是键盘模式）" if manual_now
                else "（当前是 %s）" % self.controller.hub.active_label())
        # 按钮文案跟着状态走 —— 它是**开关**: 进了键盘模式后要能切回去
        # (键盘模式已不在主窗"角度源"那一排里, 这是唯一的出口)。
        btn = getattr(self, "btn_manual", None)
        if btn is not None:
            btn.setText("切回传感器源" if manual_now else "切到键盘手动")
        manual_rows = ["%s  %s" % (spec_of(key), label.replace("−", "-"))
                       for name, key, label, mode in HOTKEY_DEFS
                       if mode == "manual"]
        if getattr(self, "lbl_keys_manual", None) is not None:
            self.lbl_keys_manual.setText("\n".join(manual_rows))

        self._fit_hint_labels()

    def _fit_hint_labels(self):
        """把三个 wordWrap 提示标签的高度按实际文字重算一遍, 并让弹窗跟着改高。

        必须在**每次 setText 之后**调用 —— 标签高度依赖当前文字行数, 不刷新
        就会出现"文字换了但高度还是旧的" -> 裁字或留白。

        ⚠️ **同时要重算弹窗高度 (`_fit_adv_page`)。** 否则会出现这个真 bug:
        默认 ESP32 -> 切到键盘模式时, 调试页的快捷键列表从 1 条变 6 条,
        标签变高了, 但弹窗高度还停在旧值 -> **下面的快捷键被遮住一部分**。
        (原来只在切 tab 时才调 _fit_adv_page, 切"模式"时不会。)
        """
        for lbl in (getattr(self, "lbl_keys", None),
                    getattr(self, "lbl_scan", None),
                    getattr(self, "lbl_keys_manual", None)):
            if lbl is None:
                continue
            fn = getattr(lbl, "_fit_height", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
        # 标签高度变了 -> 当前页需要的高度也变了 -> 弹窗跟着调
        d = getattr(self, "_adv_dialog", None)
        if d is not None and d.isVisible():
            page = self.stack_adv.currentWidget()
            if page is not None:
                lay = page.layout()
                if lay is not None:
                    lay.activate()          # 先让布局把新高度算出来
            self._fit_adv_page()
            if not d.isMaximized():
                d.adjustSize()
                self._clamp_adv_to_screen()

    def resizeEvent(self, ev):
        """窗口宽度变了 -> 提示标签可能换行数, 重算高度。"""
        super().resizeEvent(ev)
        self._fit_hint_labels()
        # 弹窗也重算 (它有自己的宽度)
        d = getattr(self, "_adv_dialog", None)
        if d is not None and d.isVisible():
            QTimer.singleShot(0, self._fit_hint_labels)

    def _switch_to_manual(self):
        """在"键盘手动"和上一个真实传感器源之间切换 (调试用)。

        **做成开关**: 键盘模式不在主窗的"角度源"那一排里了 (它的入口就在
        这个按钮), 所以必须有办法切回去 —— 否则进了键盘模式就只能改配置文件
        才能出去。切回去时优先回摄像头, 没摄像头就回 ESP32。
        """
        active = self.controller.hub.active_name()
        if active == "manual":
            # 切回去: 优先摄像头, 其次串口
            target = "camera" if self._camera_usable() else "serial"
        else:
            target = "manual"
        try:
            self.controller.set_source(target)
        except Exception as exc:  # noqa: BLE001
            print("[ui] 切换角度源失败: %s" % exc)
            return
        self.controller.save()
        # 切源后热键会重新注册 —— 刷新界面各处 (含本页快捷键文案)
        self._loading = True
        try:
            self._refresh_sources()
        finally:
            self._loading = False

    def _camera_usable(self):
        """摄像头源当前是否可用 (用于决定"切回去"的目标)。"""
        try:
            return self.controller.hub.device_available("camera") is not False
        except Exception:  # noqa: BLE001
            return True

    def _fill_camera_combo(self):
        self.cmb_camera.clear()
        cur = int(self.cfg.get("camera_index", 0))
        if self._scan_result:
            for item in self._scan_result:
                self.cmb_camera.addItem(
                    "index %d  %dx%d  %s"
                    % (item["index"], item["width"], item["height"], item["backend"]),
                    item["index"])
        else:
            for i in range(4):
                self.cmb_camera.addItem("index %d" % i, i)
        pos = self.cmb_camera.findData(cur)
        if pos < 0:
            self.cmb_camera.addItem("index %d (当前)" % cur, cur)
            pos = self.cmb_camera.count() - 1
        self.cmb_camera.setCurrentIndex(pos)

    def _refresh_effect(self):
        self.cmb_outside.setCurrentIndex(
            max(0, self.cmb_outside.findData(self.cfg.get("outside_mode", "black"))))
        # **必须先放宽范围, 再填值。** "重截频率"的默认值是显示器刷新率 (本机
        # 165), 而 refresh_hz 输入框建出来时的静态上限只有 120 —— 若先 setValue
        # 再放宽, 165 会被夹成 120, 接着任何一次效果改动都会把这个 120 写回
        # config.json, 用户的 refresh_hz 就被静默改小了。先把上限抬到刷新率,
        # setValue 就不会误夹。
        self._refresh_refresh_max()
        for key, box in self._inputs.items():
            # 缺失时用该字段自己的下限 (而不是硬编码 0) —— 否则 refresh_hz
            # 缺失时会填 0, 而 0 的语义是"不设限", 与默认 -1 不同, 容易混。
            lo = getattr(box, "_lo", 0)
            box.setValue(self.cfg.get(key, lo), emit=False)
        self.edt_backdrop.setText(str(self.cfg.get("backdrop_path", "")))

    def _refresh_refresh_max(self):
        """"重截频率"的上限设成**当前选中显示器的刷新率** —— 固定值, 不做动态测量。

        之前拿实测单帧耗时算上限, 结果它随负载在 18~165 之间跳, 用户看到一个
        变来变去的数字只会困惑。上限就该是一个稳定的"最高可用频率":
        合成器每秒最多产那么多帧, 填更高也没有用。

        ⚠️ **下限必须是 -1 (不设限), 不能写成 1。** 原来这里 `set_range(1, hi)`
        会把 INPUTS 里定义的 -1 下限冲掉 —— 于是输入框里根本填不了 -1, 用户
        看到的是"这个框写不了 -1", 实际是这里每次刷新都把它夹回 1。
        """
        box = self._inputs.get("refresh_hz")
        if box is None:
            return
        hi = max(1, int(round(self.controller.screen_hz())))
        # 下限固定 -1 (=-1 表示"自动跟随刷新率/不设限")
        if int(box._hi) != hi or int(box._lo) != -1:
            box.set_range(-1, hi)

    def _refresh_glass(self):
        on = self.controller.glass_on
        self.sw_glass.setChecked(on)
        self.lbl_glass_state.setText("显示中" if on else "待机")
        self.num_level.setValue(self.controller.manual_level() * 100, emit=False)

    def _refresh_status(self):
        # **窗口不可见时不干活。** 面板最小化只是 hide() (对象还活着), 定时器
        # 却仍每 500ms 跑一遍: 查角度源、字体度量、刷开关状态 —— 没人看的窗口上
        # 白做功。隐藏时直接返回, 再显示时 refresh_all/下一次 tick 会补上。
        if not self.isVisible():
            return
        # 实际在用的采集后端可能变 (auto 重试 / 回退), 顺手刷新那行说明
        self._refresh_capture_state()
        try:
            level, name, status, detail = self.controller.hub.resolve()
        except Exception:  # noqa: BLE001
            return
        bits = [LABELS.get(name, name), status]
        if detail:
            if "pitch" in detail:
                bits.append("pitch %.2f°" % detail["pitch"])
                bits.append("匹配 %d" % detail["matches"])
                bits.append(detail.get("backend", ""))
            if "angle" in detail:
                bits.append("%.1f°" % detail["angle"])
        bits.append("浓度 %.0f%%" % ((level or 0) * 100))
        text = "  ·  ".join(b for b in bits if b)
        # 单行显示, 放不下就省略号截断 (宽度变化时实时重算)
        fm = self.lbl_status.fontMetrics()
        avail = max(80, self.lbl_status.width() or (self.width() - 36))
        self.lbl_status.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, avail))
        # 这几行原本是 _refresh_glass() 的重复实现, 且 _loading 没用 try/finally
        # —— 中间任一句抛异常, _loading 就永久卡在 True, 之后所有用户交互都被
        # `if self._loading: return` 静默吞掉, 界面像死了。这里改成: 复用
        # _refresh_glass(), 并且用 try/finally 保证 _loading 一定复位。
        self._loading = True
        try:
            self._refresh_glass()
            self._refresh_refresh_max()
        finally:
            self._loading = False

    # ================================================================ 交互
    def _fit_height(self):
        """首次显示时把窗口高度贴合内容 —— 不留底部空白, 也**不超出屏幕**。

        只做一次 (`self._fitted`): 之后用户手动拉大/最大化都不再干预。
        用 sizeHint 而不是 setFixedHeight, 所以窗口仍然可自由缩放/最大化。

        ⚠️ **必须夹到屏幕可用高度。** 原来直接 `resize(w, sizeHint().height())`,
        内容一多 (快捷键列表、扫描结果) 窗口就长得比屏幕还高 -> **下半截跑到
        屏幕外**, 用户看到的就是"切换后把其他组件遮住了 / 窗口底不见了"。
        实测: 主窗高 401、y=849, 屏幕可用底只有 1019 -> 超出 231px。
        """
        if self._fitted:
            return
        self._fitted = True
        if self.isMaximized():
            return
        h = max(self.minimumHeight(), self.sizeHint().height())
        # 夹到当前屏幕的可用高度 (留一点边距, 免得贴着任务栏)
        try:
            scr = self.screen() or QApplication.primaryScreen()
            avail = scr.availableGeometry().height() if scr is not None else 0
            if avail > 100:
                h = min(h, avail - 40)
        except Exception:  # noqa: BLE001
            pass
        self.resize(self.width(), h)
        # 位置也拉回屏幕内 (内容变高后底边可能已经出屏)
        self._clamp_to_screen()

    def _clamp_to_screen(self):
        """把窗口挪回当前屏幕的可用区域内 (底边/右边出屏时)。"""
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            a = scr.availableGeometry()
            g = self.frameGeometry()
            x, y = g.x(), g.y()
            if x + g.width() > a.x() + a.width():
                x = a.x() + a.width() - g.width()
            if y + g.height() > a.y() + a.height():
                y = a.y() + a.height() - g.height()
            x = max(a.x(), x)
            y = max(a.y(), y)
            if (x, y) != (g.x(), g.y()):
                self.move(x, y)
        except Exception:  # noqa: BLE001
            pass

    def _open_advanced(self):
        """弹出高级设置窗 (modeless, 跟随主窗但不阻塞)。"""
        d = self._adv_dialog
        first = not d.isVisible()
        d.show()
        if first:
            # **只在首次显示时贴合内容**。之后用户可能拉大/最大化了, 再调
            # adjustSize 会把它强行缩回去, 跟最大化打架。
            # 先让 stack 贴合当前页高度, adjustSize 才不会被"最高那页"撑大。
            self._fit_adv_page()
            d.adjustSize()
        # 无论首次还是再次打开, 都拉回屏幕内 (内容可能变长过)
        self._clamp_adv_to_screen()
        d.raise_()
        d.activateWindow()

    def _on_glass_switch(self, checked):
        if self._loading:
            return
        if checked:
            self.controller.start_glass()
        else:
            self.controller.stop_glass()
        self._refresh_glass()
        self._refresh_status()

    def _on_adv_tab(self, key):
        mapping = {"effect": 0, "launch": 1, "debug": 2}
        if key in mapping:
            self.stack_adv.setCurrentIndex(mapping[key])
            self._fit_adv_page()
            # 换页后贴合新页内容 —— 但用户已拉大/最大化时别动, 否则会缩回去
            d = self._adv_dialog
            if not d.isMaximized():
                QTimer.singleShot(0, d.adjustSize)
                QTimer.singleShot(0, self._clamp_adv_to_screen)

    def _clamp_adv_to_screen(self):
        """把高级设置弹窗挪回、缩回屏幕可用区域内。

        它按内容长高 (快捷键列表一长就变高), 长过头就会**下半截跑到屏幕外**。
        这里: 先夹高度, 再把位置挪回屏内。
        """
        d = getattr(self, "_adv_dialog", None)
        if d is None or not d.isVisible() or d.isMaximized():
            return
        try:
            scr = d.screen() or self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            a = scr.availableGeometry()
            g = d.frameGeometry()
            h = min(g.height(), max(200, a.height() - 40))
            if h != g.height():
                d.resize(d.width(), h)
                g = d.frameGeometry()
            x, y = g.x(), g.y()
            if x + g.width() > a.x() + a.width():
                x = a.x() + a.width() - g.width()
            if y + g.height() > a.y() + a.height():
                y = a.y() + a.height() - g.height()
            d.move(max(a.x(), x), max(a.y(), y))
        except Exception:  # noqa: BLE001
            pass

    def _fit_adv_page(self):
        """让弹窗高度贴合**当前这一页**, 不留一大片空白。

        `QStackedWidget.sizeHint()` 取的是**所有子页里最大**的那个, 所以
        「启动与标定」只有 ~130px 内容, 却会被最高页 (效果参数 ~300px) 撑到
        同样高 —— 底部一大片空白。`adjustSize()` 也救不了 (它信的正是这个
        sizeHint)。

        解法: **让 stack 的 sizeHint 跟随当前页** (见下面的 _CurPageStack)。
        不能用 `setFixedHeight`: 那是**硬**约束, 一旦标签因为换行/DPI 需要更高
        的高度, 页面被钉死 -> 控件互相挤压/遮挡 (用户反馈的"切换后控件互遮")。
        """
        # 只需通知 stack 重新算 sizeHint (它会按当前页返回), 再让布局生效
        self.stack_adv.updateGeometry()
        page = self.stack_adv.currentWidget()
        if page is not None:
            page.updateGeometry()
            lay = page.layout()
            if lay is not None:
                lay.activate()

    def _toggle_hinge(self):
        """反转铰链方向 (底边 <-> 顶边), 并让按钮图标转过去。"""
        cur = bool(self.cfg.get("flip_hinge", False))
        new = not cur
        self.cfg["flip_hinge"] = new
        self.controller.save()
        # 让 overlay 立刻按新设置重画 (它每帧都从 cfg 读, apply_config 会刷新)
        try:
            if self.controller.overlay is not None:
                self.controller.overlay.apply_config()
                self.controller.overlay.update()
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "btn_hinge", None) is not None:
            self.btn_hinge.set_flipped(new, animate=True)
        print("[ui] 铰链方向 -> %s" % ("顶边 (反着用)" if new else "底边"))

    def _quick_calibrate(self):
        self.controller.calibrate_camera()

    def _quick_flip(self):
        self.controller.flip_camera_sign()
        # try/finally: 中间抛异常时 _loading 不能卡在 True —— 那会让之后所有
        # 用户交互被 `if self._loading: return` 静默吞掉 (整窗"假死")。
        self._loading = True
        try:
            self._refresh_effect()
        finally:
            self._loading = False

    def _quick_debug(self):
        self.controller.toggle_debug_window()

    def _on_screen(self, pos):
        if self._loading or pos < 0:
            return
        alls = monitors.screens()
        if 0 <= pos < len(alls):
            self.controller.set_screen(alls[pos])
            self.controller.save()

    def _on_level(self, pct):
        if self._loading:
            return
        self.controller.set_manual_level(pct / 100.0)

    def _on_source(self, key):
        if self._loading or not key:
            return
        self.controller.set_source(key)
        self.controller.save()
        # 用 _refresh_sources 而不是只刷热键 —— 它能一并把主窗那行"源设置"
        # 切到对应的控件 (摄像头选择 / 串口), 以及显隐标定按钮。
        self._loading = True
        try:
            self._refresh_sources()
        finally:
            self._loading = False
        self._refresh_status()

    def _on_camera(self, _pos):
        if self._loading:
            return
        data = self.cmb_camera.currentData()
        if data is None:
            return
        # 保留用户/扫描确定的 backend, **别硬编码 auto** —— 否则用户特意选的
        # dshow/msmf 会被这次换 index 抹回 auto。
        backend = str(self.cfg.get("camera_backend", "auto"))
        self.controller.set_camera(int(data), backend)
        self.controller.save()

    def _on_serial(self):
        if self._loading:
            return
        self.controller.set_serial(self.edt_port.text().strip())
        self.controller.save()

    def _on_effect(self, *_a):
        if self._loading:
            return
        for key, box in self._inputs.items():
            self.cfg[key] = float(box.value())
        self.cfg["outside_mode"] = self.cmb_outside.currentData()
        self.controller.apply_effect_settings()
        self.controller.save()

    def _on_autostart(self, checked):
        if self._loading:
            return
        try:
            autostart.set_enabled(checked)
        except Exception as exc:  # noqa: BLE001
            print("[autostart] %s" % exc)
        self._loading = True
        try:
            self.sw_autostart.setChecked(autostart.is_enabled())
        finally:
            self._loading = False

    def _on_autocal(self, checked):
        if self._loading:
            return
        self.cfg["autocal_on_glass_open"] = bool(checked)
        self.controller.save()

    def _on_autoglass(self, checked):
        if self._loading:
            return
        self.cfg["autostart_glass"] = bool(checked)
        self.controller.save()

    def _on_lowmem(self, checked):
        """低内存模式开关。

        开 -> 允许"闲置时释放 GL 窗口"; 关 -> 立刻取消待执行的释放, 并把
        窗口建回来 (否则用户刚关掉开关却要等下次开启才恢复)。
        """
        if self._loading:
            return
        self.cfg["low_memory_mode"] = bool(checked)
        self.controller.save()
        self.controller.apply_low_memory_mode()

    def _on_capture_backend(self, _idx):
        """采集后端下拉框变了 -> 立即切换 (不用重启)。"""
        if self._loading:
            return
        val = self.cmb_capture_backend.currentData()
        if not val:
            return
        ok = self.controller.set_capture_backend(val)
        self._refresh_capture_state()
        if not ok:
            # 切失败 (比如手动选了 wgc 但这台机器不支持) -> 把下拉框退回原值,
            # 免得界面显示的和实际在用的不一致。
            self._loading = True
            try:
                self._sync_capture_combo()
            finally:
                self._loading = False

    def _sync_capture_combo(self):
        """把下拉框设成 cfg 里的值 (不发信号)。"""
        cur = str(self.cfg.get("capture_backend", "auto"))
        i = self.cmb_capture_backend.findData(cur)
        if i >= 0:
            self.cmb_capture_backend.setCurrentIndex(i)

    def _refresh_capture_state(self):
        """显示"当前实际在用的后端" —— 与下拉框的选择可能不同 (auto 时由系统定)。"""
        if getattr(self, "lbl_capture_state", None) is None:
            return
        want = str(self.cfg.get("capture_backend", "auto"))
        actual = self.controller.capture_backend()
        if want == "auto":
            self.lbl_capture_state.setText(
                "auto —— 系统自动挑选最优; 当前实际在用: %s" % actual)
        else:
            self.lbl_capture_state.setText(
                "手动指定 %s; 当前实际在用: %s" % (want, actual))

    def _open_log_dialog(self):
        dlg = LogDialog(self)
        dlg.exec()

    def _pick_backdrop(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择背景图", str(Path.home()),
            "图片 (*.png *.jpg *.jpeg *.bmp);;所有文件 (*)")
        if not path:
            return
        self.edt_backdrop.setText(path)
        self.controller.set_backdrop(path)
        self.controller.save()

    def _clear_backdrop(self):
        self.cfg["backdrop_path"] = "desk_bg.png"
        self.edt_backdrop.setText("desk_bg.png")
        self.controller.set_backdrop("desk_bg.png")
        self.controller.save()

    # ================================================================ 扫描
    def start_scan(self):
        if self._scan is not None and self._scan.isRunning():
            return
        self.btn_scan.setEnabled(False)
        self.lbl_scan.setText("扫描中… 会逐个打开摄像头, 约需几秒")
        self._fit_hint_labels()
        self.controller.begin_scan()
        self._scan = CameraScanThread(self)
        self._scan.scanned.connect(self._on_scanned)
        self._scan.start()

    def _on_scanned(self, found):
        self._scan_result = found
        self.controller.end_scan()
        self.btn_scan.setEnabled(True)
        if not found:
            self.lbl_scan.setText("没扫到可用摄像头。检查相机权限, 或确认没被别的程序占用。")
        else:
            self.lbl_scan.setText("扫到 %d 个可用摄像头（虚拟摄像头的冻结帧已排除）"
                                  % len(found))
        self._loading = True
        try:
            self._fill_camera_combo()
        finally:
            self._loading = False
        # 扫描结果文字变了 (行数可能变), 重算提示标签高度
        self._fit_hint_labels()

    # ================================================================ 生命周期
    def showEvent(self, ev):
        super().showEvent(ev)
        # 从托盘唤出时可能还带着"已最小化"的状态 —— 必须先清掉, 否则窗口
        # 会以最小化状态出现 (或者干脆不出现)。
        if self.isMinimized():
            self.setWindowState(self.windowState()
                                & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()
        self.refresh_all()
        QTimer.singleShot(0, self._fit_height)
        # **打开设置窗口不再自动扫描摄像头。**
        #
        # 原因: 扫描要**独占**摄像头 (CameraScanThread 逐个 open_camera 探测),
        # 所以 begin_scan() 会先 stop_source("camera") 把正在跑的摄像头停掉, 扫完
        # end_scan() 再重启 —— 于是"一打开设置窗口, 正在追踪的摄像头就被重启",
        # 用户看到画面/角度短暂中断。而多数时候用户开设置只是改个参数, 根本不需要
        # 重新枚举摄像头。改成: **只在用户主动点「摄像头」那一行的「扫描」按钮时才
        # 扫** (btn_scan 已接 start_scan)。不扫时下拉框给 index 0..3 的占位项,
        # 够用; 要枚举真实设备就点一次「扫描」。

    def changeEvent(self, ev):
        """最小化 -> 直接收进托盘。

        这个程序是托盘常驻的, 任务栏上不该留一个最小化的窗口 —— 用户的预期是
        "点最小化 = 回到托盘", 而不是"缩到任务栏, 还得再点回来"。

        注意**不要** `ev.ignore()`: 那样窗口会保持"可见"(只是被标记为最小化),
        等于没进托盘。要放行状态变化, 再把窗口 hide() 掉。
        """
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            self.hide()
    def closeEvent(self, ev):
        """点 X = **真的退出程序**（不是收回托盘）。

        这个程序是托盘常驻的，所以"点 X 只是收起"曾经是刻意的设计；但实际用起来
        那个行为很别扭 —— 用户点 X 的意图就是"关掉它"，结果窗口没了、程序还在
        后台跑着、托盘图标也还在，只能再去托盘右键退出一次。

        现在放行关闭，并调 `app.quit()` 让事件循环退出 —— 这样 `run_tray()` 的
        `finally` 会照常执行完整清理（`panel.shutdown()` 收扫描线程 ->
        `controller.shutdown()` 停角度源/截屏/热键 -> `wait_orphans()`），
        和"托盘 -> 退出"走的是**同一条**退出路径。

        **为什么不能只 `ev.accept()` 不 quit**: 那样窗口会被销毁，但
        `setQuitOnLastWindowClosed(False)` 让事件循环继续跑 —— 程序变成
        "看不见也没托盘"的僵尸进程，比原来的行为更糟。
        """
        ev.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def shutdown(self):
        """程序退出前必须调用。

        扫描线程若还在原生 cv2 调用里, 直接销毁它会让 Qt 以
        `QThread: Destroyed while thread is still running` **abort 整个进程**
        (0xC0000409), 而且抓不到。

        **但不能为了安全就让用户等 20 秒。** `open_camera` 是同步阻塞的, 打断
        不了, 本机实测最坏一次要 20 秒。所以策略是:
          1. 先给一个短窗口 (2s) 让它自然结束 —— 绝大多数情况都够了;
          2. 还没结束就把它**从 Qt 的父子关系里摘出来** (`setParent(None)`),
             寄存到模块级列表。**光留 Python 引用是不够的** —— 线程是
             `CameraScanThread(self)` 建的, Qt 会在销毁父对象 (面板) 时连带
             删掉子线程, 于是照样 `QThread: Destroyed while thread is still
             running` → abort。必须把父对象也摘掉。
             它是 daemon 线程, 进程退出时由操作系统回收, 不会拖住退出。
        """
        self._timer.stop()
        th = self._scan
        if th is not None and th.isRunning():
            th.stop()
            if not th.wait(2000):
                th.setParent(None)          # 断开 Qt 父子关系, 否则面板一销毁就 abort
                self._scan = None
                _ORPHANED.append(th)
                print("[ui] 扫描线程仍在打开设备, 已寄存 (daemon, 随进程退出回收)")

    @staticmethod
    def wait_orphans(extra_ms=2500):
        """退出前再给寄存的扫描线程一点时间。

        `shutdown()` 只等 2 秒, 而 `open_camera` 最坏要 20 秒。如果进程就在
        线程还跑在 MediaFoundation 里的时候被拆掉, **解释器关闭阶段**会踩到
        已释放的 native 对象 —— 表现为进程退出码 `0xC0000409`, 而且是在所有
        测试都 PASS 之后才崩, 很容易误判成功能问题。
        这里再等一小段: 绝大多数情况线程已经结束了; 真等不到也无所谓, 它不会
        再崩 (父对象已经摘掉)。**注意别等太久** —— 那会让用户觉得程序关不掉。
        """
        for th in list(_ORPHANED):
            if th.isRunning():
                th.wait(extra_ms)
            if not th.isRunning():
                _ORPHANED.remove(th)
