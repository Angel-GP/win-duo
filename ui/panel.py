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

from PyQt6.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QApplication, QFileDialog, QStackedWidget,
                             QVBoxLayout, QWidget)

from angles.hub import LABELS

from . import autostart, monitors
from .log_dialog import LogDialog
from .widgets import (BodyLabel, CaptionLabel, CardWidget, ComboBox, LineEdit,
                      NumberField, PrimaryPushButton, SegmentBar, StrongBodyLabel,
                      SwitchButton, TransparentPushButton, card_layout, make_icon, row)

SOURCE_ORDER = ("camera", "serial", "manual")
OUTSIDE = (("black", "纯黑 (原版)"), ("backdrop", "背景图兜底 (无黑场)"))

#: 退出时还没结束、又不能在原生调用里被打断的线程寄存处。
#: 见 `SettingsPanel.shutdown`: 留着引用 → Qt 不析构 → 不 abort;
#: 它们都是 daemon 线程, 进程退出时由系统回收。
_ORPHANED = []

#: 数值输入项: (config 键, 标签, 最小, 最大, 步进, 单位, 小数位)
INPUTS = (
    ("max_tilt_deg", "最大转角", 30, 89, 1, "°", 0),
    ("eye_dist_h", "眼距", 0.5, 6.0, 0.1, "×屏高", 1),
    ("blur_spread", "模糊强度", 0.0, 1.5, 0.01, "", 2),
    ("refresh_hz", "重截频率", 1, 120, 1, "Hz", 0),
)


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
        self._scan_scheduled = False
        self._inputs = {}

        self.setWindowTitle("win-duo 设置")
        self.setWindowIcon(make_icon())
        self.setFixedWidth(470)
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
        for box in self._inputs.values():
            box.changed.connect(self._on_effect)

    # ================================================================ 构建
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_glass_card())
        root.addWidget(self._build_source_card())
        root.addWidget(self._build_advanced_card())
        root.addWidget(self._build_status())

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
        lay = card_layout(card)

        self.seg_source = SegmentBar()
        for name in SOURCE_ORDER:
            self.seg_source.add(LABELS.get(name, name), name)
        lay.addWidget(row(StrongBodyLabel("角度源"), None, self.seg_source,
                          spacing=8)[0])

        self.cmb_camera = ComboBox()
        self.btn_scan = TransparentPushButton("扫描")
        self.btn_scan.clicked.connect(self.start_scan)
        lay.addWidget(self._labeled("摄像头", row(self.cmb_camera, self.btn_scan,
                                                  spacing=6)[0]))

        # 摄像头相关的快捷操作 (标定基准帧 / 翻转方向)
        self.btn_calib = TransparentPushButton("标定基准帧")
        self.btn_calib.clicked.connect(self._quick_calibrate)
        self.btn_flip = TransparentPushButton("翻转方向")
        self.btn_flip.clicked.connect(self._quick_flip)
        lay.addWidget(self._labeled("", row(self.btn_calib, self.btn_flip, spacing=6)[0]))

        self.lbl_keys = CaptionLabel("")
        self.lbl_keys.setWordWrap(True)
        self.lbl_keys.setObjectName("hint")
        lay.addWidget(self.lbl_keys)

        self.lbl_scan = CaptionLabel("")
        self.lbl_scan.setWordWrap(True)
        lay.addWidget(self.lbl_scan)
        return card

    # ---------------------------------------------------------- 高级设置
    def _build_advanced_card(self):
        card = CardWidget()
        lay = card_layout(card, spacing=6)

        self.btn_adv = TransparentPushButton("▸   高级设置")
        self.btn_adv.clicked.connect(self._toggle_advanced)
        lay.addWidget(self.btn_adv)

        self.adv_body = QWidget()
        body = QVBoxLayout(self.adv_body)
        body.setContentsMargins(0, 6, 0, 0)
        body.setSpacing(9)

        # 用分页切换取代超长纵向展开，避免撑爆屏幕
        self.tab_adv = SegmentBar()
        self.tab_adv.add("效果参数", "effect")
        self.tab_adv.add("启动与标定", "launch")
        self.tab_adv.add("调试", "debug")
        body.addWidget(self.tab_adv)

        self.stack_adv = QStackedWidget()

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

        self.sw_autocal = SwitchButton()
        self.sw_autocal.setOnText("开")
        self.sw_autocal.setOffText("关")
        l2.addWidget(row(BodyLabel("自动标定"), None, self.sw_autocal, spacing=8)[0])

        self.sw_autoglass = SwitchButton()
        self.sw_autoglass.setOnText("开")
        self.sw_autoglass.setOffText("关")
        l2.addWidget(row(BodyLabel("自动启动"), None, self.sw_autoglass, spacing=8)[0])

        self.sw_autostart = SwitchButton()
        self.sw_autostart.setOnText("开")
        self.sw_autostart.setOffText("关")
        l2.addWidget(row(BodyLabel("开机自启"), None, self.sw_autostart, spacing=8)[0])

        self.edt_port = LineEdit()
        self.edt_port.setPlaceholderText("COM3")
        l2.addWidget(self._labeled("ESP32 串口", self.edt_port, label_w=78))
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
        l3.addStretch(1)
        self.stack_adv.addWidget(p3)

        body.addWidget(self.stack_adv)
        self.tab_adv.changed.connect(self._on_adv_tab)

        self.adv_body.setVisible(False)
        lay.addWidget(self.adv_body)
        return card

    # ---------------------------------------------------------- 状态行
    def _build_status(self):
        self.lbl_status = CaptionLabel("")
        self.lbl_status.setWordWrap(True)
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
        self.seg_source.set_current(self.controller.hub.active_name())
        self._fill_camera_combo()
        self.edt_port.setText(str(self.cfg.get("port", "COM3")))
        self._refresh_hotkeys()

    def _refresh_hotkeys(self):
        """列出**当前真正生效**的全局热键。

        启动器用的是 pythonw, 没有控制台 —— 以前键盘模式靠 msvcrt 读控制台按键,
        那样根本读不到, 所以键盘模式"没生效"。现在改为全局热键, 并且要在这里
        明明白白告诉用户按什么 (只列注册成功的, 免得显示一堆按了没反应的键)。
        """
        try:
            lines = self.controller.hotkey_lines()
        except Exception:  # noqa: BLE001
            lines = []
        manual = self.controller.hub.active_name() == "manual"
        if not lines:
            self.lbl_keys.setText("")
            return
        head = ("键盘模式快捷键（全局生效，不需要窗口焦点）:"
                if manual else "全局热键:")
        body = "\n".join("      %-22s %s" % (k, v) for k, v in lines)
        self.lbl_keys.setText(head + "\n" + body)

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
            box.setValue(self.cfg.get(key, 0), emit=False)
        self.edt_backdrop.setText(str(self.cfg.get("backdrop_path", "")))

    def _refresh_refresh_max(self):
        """"重截频率"的上限设成**显示器刷新率** —— 固定值, 不做动态测量。

        之前拿实测单帧耗时算上限, 结果它随负载在 18~165 之间跳, 用户看到一个
        变来变去的数字只会困惑。上限就该是一个稳定的"最高可用频率":
        合成器每秒最多产那么多帧, 填更高也没有用。
        """
        box = self._inputs.get("refresh_hz")
        if box is None:
            return
        hi = max(1, int(round(self.controller.screen_hz())))
        if int(box._hi) != hi:
            box.set_range(1, hi)

    def _refresh_glass(self):
        on = self.controller.glass_on
        self.sw_glass.setChecked(on)
        self.lbl_glass_state.setText("显示中" if on else "待机")
        self.num_level.setValue(self.controller.manual_level() * 100, emit=False)

    def _refresh_status(self):
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
        self.lbl_status.setText("  ·  ".join(b for b in bits if b))
        self._loading = True
        self.sw_glass.setChecked(self.controller.glass_on)
        self.lbl_glass_state.setText("显示中" if self.controller.glass_on else "待机")
        self.num_level.setValue(self.controller.manual_level() * 100, emit=False)
        self._refresh_refresh_max()
        self._loading = False

    # ================================================================ 交互
    def _toggle_advanced(self):
        # 用 isHidden() 而不是 isVisible(): 后者在窗口自己没显示时恒为 False,
        # 会导致"展开"永远只往一个方向切
        show = self.adv_body.isHidden()
        self.adv_body.setVisible(show)
        self.btn_adv.setText("▾   高级设置" if show else "▸   高级设置")
        QTimer.singleShot(0, self.adjustSize)

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
            QTimer.singleShot(0, self.adjustSize)

    def _switch_adv_tab(self, key):
        self.tab_adv.set_current(key)
        self._on_adv_tab(key)

    def _quick_calibrate(self):
        self.controller.calibrate_camera()

    def _quick_flip(self):
        self.controller.flip_camera_sign()
        self._loading = True
        self._refresh_effect()
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
        self._refresh_hotkeys()
        self._refresh_status()

    def _on_camera(self, _pos):
        if self._loading:
            return
        data = self.cmb_camera.currentData()
        if data is None:
            return
        self.controller.set_camera(int(data), "auto")
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
        self.sw_autostart.setChecked(autostart.is_enabled())
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
        self._fill_camera_combo()
        self._loading = False

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
        # 首次打开时才扫描摄像头: 没必要在程序启动、还待在托盘里的时候就做。
        #
        # **加 `_scan_scheduled` 门闩**: showEvent 会被调用多次 (隐藏后再显示、
        # 以及一些 processEvents 场景), 每次都在这里排一个 `singleShot(150,
        # start_scan)` 的话, 前一个扫描线程还在跑时又会排一个 —— 而且
        # `start_scan` 里的守卫只看"当前 _scan 是否在跑", 挡不住这种情况。
        # 线程对象一旦被覆盖, Qt 就会在销毁它时 abort
        # (`QThread: Destroyed while thread is still running` -> 0xC0000409)。
        if (not self._scan_scheduled and self._scan_result is None
                and not (self._scan and self._scan.isRunning())):
            self._scan_scheduled = True
            QTimer.singleShot(150, self.start_scan)

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
