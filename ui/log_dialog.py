"""日志查看与导出弹窗。"""
import shutil
import subprocess
import sys
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QVBoxLayout,
                             QWidget)

import paths
import wdlog
from paths import log_file
from .widgets import (BodyLabel, CaptionLabel, ComboBox, LineEdit, PlainTextEdit,
                      PrimaryPushButton, StrongBodyLabel, TransparentPushButton,
                      make_icon)

#: 日志文件: <数据目录>/diagnostics/debug/log/last.log (打包后是 exe 旁边)
#:
#: **用 last.log 而不是某个固定文件名** —— 日志现在一次运行一个文件, 名字是
#: 启动时刻 (见 main._run_log_path), 硬编码一个名字只能看到那一次。last.log
#: 通过硬链接始终指向**最新一次运行**的日志, 所以这里永远读到当前这一份。
LOG_FILE = log_file("last.log")


#: 日志窗最多读文件**末尾**这么多字节。
#: 日志"不轮转、不删除", 一次长时间运行可以到几十 MB; 每次都整份读进内存、
#: 再整段塞进 QPlainTextEdit, 会让 UI 线程卡住。日志是**只追加**的, 排查时看
#: 的也永远是末尾 —— 读尾部就够, 完整文件留给"保存日志文件"和控制台 tail。
#: 512KB 大约几千行, 在弹窗里翻已经是绰绰有余。
TAIL_BYTES = 512 * 1024


def get_all_logs(max_bytes=TAIL_BYTES) -> str:
    """读日志文件内容 (超长时**只读末尾** max_bytes 字节)。

    日志是 main.py 把 stdout 重定向到日志文件写的, 所以**唯一**的来源
    就是那个文件 —— 原来还有一套 `_MEMORY_LOGS` 内存缓冲 + `record_log()`,
    但 `record_log()` 全项目从没被调用过, 缓冲永远是空的, 于是这里每次都得
    落到磁盘那条分支。那套东西是没接上的半成品, 已删除。
    """
    if not LOG_FILE.exists():
        return ""
    try:
        size = LOG_FILE.stat().st_size
        with open(LOG_FILE, "rb") as fh:
            if max_bytes is not None and size > max_bytes:
                fh.seek(size - max_bytes)
                # 起点可能落在半个 UTF-8 字符上 —— errors="replace" 兜住它,
                # 再把第一个换行之前那半行 (被截断的) 整行丢掉, 免得日志窗
                # 开头挂一行乱码残片。
                text = fh.read().decode("utf-8", errors="replace")
                nl = text.find("\n")
                if nl >= 0:
                    text = text[nl + 1:]
                return ("... [只显示末尾 %d KB, 全文共 %d KB; "
                        "完整日志请点「保存日志文件」]\n"
                        % (max_bytes // 1024, size // 1024)) + text
            return fh.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def open_console_tail():
    """新开一个原生控制台窗口, 实时 tail 日志文件。

    为什么这么做: 打包成 exe 是 **--windowed** 的, 进程没有控制台, 平时看不到
    stdout。而 main.py 把所有 stdout/stderr 都 tee 进了日志文件, 所以这里
    另起一个控制台窗口去实时跟随那个文件, 就等于"随时调出一个命令行日志窗"。
    (不动主进程的流, 只读文件 —— 安全、不影响运行。)
    跟随的是 last.log: 它是**当前这次运行**的硬链接, 内容实时同步。

    - chcp 65001: 把控制台切到 UTF-8, 否则中文日志会乱码 (默认 cp936)。
    - Get-Content -Wait: PowerShell 的实时 tail, 新写入的行会自动冒出来。
    - CREATE_NEW_CONSOLE (0x10): 给子进程分配独立的控制台窗口。
    """
    if sys.platform != "win32":
        return
    try:
        # 文件不存在时 Get-Content -Wait 会直接报错, 先确保它在
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch(exist_ok=True)
        log = str(LOG_FILE).replace("'", "''")   # PowerShell 单引号转义
        cmd = (
            'cmd /k chcp 65001>nul & title win-duo log & '
            'powershell -NoProfile -ExecutionPolicy Bypass -Command '
            '"Get-Content -LiteralPath \'%s\' -Encoding utf8 -Wait -Tail 400"'
            % log)
        subprocess.Popen(cmd, creationflags=0x00000010)   # CREATE_NEW_CONSOLE
    except Exception as exc:  # noqa: BLE001
        wdlog.log.error("打开命令行日志窗口失败: %s" % exc, tag="log")


def save_log_file(parent=None):
    """把**整份**日志文件另存到用户选的位置。"""
    target, _ = QFileDialog.getSaveFileName(
        parent, "保存日志文件", f"win_duo_{time.strftime('%Y%m%d_%H%M%S')}.log",
        "日志文件 (*.log);;文本文件 (*.txt);;所有文件 (*)")
    if not target:
        return
    # **存的是文件本身, 不是控件里的文本。** 日志窗为了不卡顿只加载末尾
    # 一段 (见 TAIL_BYTES), 拿 toPlainText() 去存就会把用户要发出去排查的
    # 日志**悄悄截断** —— 前面正好是启动、设备枚举、后端选择这些最要紧的
    # 上下文。整份拷贝又快又不会漏。
    try:
        shutil.copyfile(LOG_FILE, target)
    except Exception as exc:  # noqa: BLE001
        wdlog.log.error("保存日志失败: %s" % exc, tag="log")


class LogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("win-duo 运行日志")
        self.setWindowIcon(make_icon())
        self.resize(680, 480)
        # **关掉就销毁。** 否则 parent 是 panel 时, accept()/点 X 只是 hide(),
        # 对象活到程序结束 —— 而它带着一个 1000ms 定时器, 每次开日志窗就多一个
        # 永久定时器, 各自每秒把整份日志读一遍 (开 N 次 = N 个)。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)

        self.lbl_info = BodyLabel("实时运行日志（包含启动、角度追踪、设备状态与报错）:")
        lay.addWidget(self.lbl_info)

        self.text_edit = PlainTextEdit(self)
        self.text_edit.setReadOnly(True)
        lay.addWidget(self.text_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_refresh = TransparentPushButton("刷新")
        # clicked 会带一个 checked 布尔; 显式 force=True, 别让它当参数用
        self.btn_refresh.clicked.connect(lambda: self.refresh_log(force=True))
        btn_row.addWidget(self.btn_refresh)

        # 弹一个**原生命令行窗口**实时滚动日志 —— 打包成无控制台 exe 后, 这是
        # 唯一能"调出命令框看实时输出"的入口。
        self.btn_console = TransparentPushButton("弹出命令行日志")
        self.btn_console.clicked.connect(self._open_console)
        if sys.platform != "win32":
            self.btn_console.setEnabled(False)
        btn_row.addWidget(self.btn_console)

        btn_row.addStretch(1)

        self.btn_save = PrimaryPushButton("保存日志文件...")
        self.btn_save.clicked.connect(self.save_log)
        btn_row.addWidget(self.btn_save)

        self.btn_close = TransparentPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)

        lay.addLayout(btn_row)

        self.refresh_log()

        # 自动轮询刷新日志
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_log)
        self.timer.start(1000)

    def closeEvent(self, ev):
        """关窗即停轮询 (配合 WA_DeleteOnClose, 对象随后被销毁)。

        双重保险: WA_DeleteOnClose 已保证销毁, 但定时器是在**销毁前**那一刻仍
        可能触发一次; 显式停掉更干净, 也让"关掉就不再读日志"这件事不依赖
        Qt 的销毁时机。
        """
        try:
            self.timer.stop()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(ev)

    def refresh_log(self, force=False):
        # **先看文件有没有变, 没变就直接返回。** 原来无条件 read_text + 跟整个
        # 文档做 O(n) 字符串比较 —— 日志几 MB 时每秒一遍全文读 + 比较, 全在
        # UI 线程上。用 (size, mtime) 快速判据挡掉绝大多数白做的事。
        # force=True (用户点"刷新") 时跳过判据, 无条件重读。
        if not force:
            try:
                st = LOG_FILE.stat()
                stamp = (st.st_size, st.st_mtime_ns)
            except OSError:
                stamp = None
            if stamp is not None and stamp == getattr(self, "_log_stamp", None):
                return
            self._log_stamp = stamp

        content = get_all_logs()
        if content == self.text_edit.toPlainText():
            return                      # 没变化就别动, 免得白重设、白跳
        sb = self.text_edit.verticalScrollBar()
        # 刷新前先记住: 用户本来是不是就贴在底部? (留几像素容差)
        # 只有"贴底"时才自动跟随滚到最新; 否则用户在往上翻历史, 别把人拽走。
        follow = sb is None or sb.value() >= sb.maximum() - 4
        prev = sb.value() if sb is not None else 0

        self.text_edit.setPlainText(content)

        if sb is None:
            return
        if follow:
            # 跟随: 用光标移到末尾再滚到底 —— setPlainText 刚结束时 maximum()
            # 可能还没重算, 直接 setValue(maximum) 会滚不到真正的底 (就是
            # "有时候不自己动"的原因)。移光标到末尾最稳。
            self.text_edit.moveCursor(QTextCursor.MoveOperation.End)
            sb.setValue(sb.maximum())
        else:
            # 日志是只追加的, 顶部内容不变, 所以原来的滚动位置仍指向同一批行 ——
            # 恢复它, 用户就停在原地不被弹走。
            # (唯一例外: 文件超过 TAIL_BYTES 后窗口会往前滑, 顶部内容确实变了,
            #  这时行号会漂一点。宁可漂也不要每秒把翻历史的用户拽回底部。)
            sb.setValue(min(prev, sb.maximum()))

    def _open_console(self):
        """按钮槽: 转调模块级实现 (二级弹窗也用它)。"""
        open_console_tail()

    def save_log(self):
        """按钮槽: 转调模块级实现 (二级弹窗也用它)。"""
        save_log_file(self)


#: 等级下拉的选项 (值, 显示文字)。值与 wdlog 的 _LEVELS 一致。
LOG_LEVELS = [
    ("fatal", "fatal — 进程即将退出"),
    ("error", "error — 操作失败, 功能受损"),
    ("warn", "warn — 异常但已兜底/降级"),
    ("info", "info — 关键事件 (默认)"),
    ("debug", "debug — 诊断用的详细过程"),
    ("trace", "trace — 逐帧数据, 最啰嗦"),
    ("all", "all — 等同 trace"),
    ("off", "off — 一条都不输出"),
]


class LogSettingsDialog(QDialog):
    """「高级设置 -> 调试 -> 日志」的二级弹窗。

    收口两件**运行期可调**的事, 外加原有的查看/保存入口:

      - **日志等级**: 改完**立即生效** (`wdlog.log.set_level`)。
      - **日志文件名模板**: 只影响**下一次启动** —— 本次的文件在进程一起来就
        打开了 (faulthandler 和日志 tee 都指着它), 没法改名。界面上明确写着
        这一点, 否则用户改完会以为"没反应"。

    两个值都写回 cfg 并落盘, 所以下次启动也记得。
    """

    def __init__(self, parent=None, cfg=None, save=None):
        super().__init__(parent)
        self.cfg = cfg if cfg is not None else {}
        self._save_cb = save
        self.setWindowTitle("win-duo 日志")
        self.setWindowIcon(make_icon())
        self.resize(460, 330)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(9)

        # ── 日志等级 ──────────────────────────────────────────────
        self.cmb_level = ComboBox()
        for value, text in LOG_LEVELS:
            self.cmb_level.addItem(text, value)
        cur = str(self.cfg.get("log_level", "info")).lower()
        i = self.cmb_level.findData(cur)
        if i < 0:
            i = self.cmb_level.findData("info")
        self.cmb_level.setCurrentIndex(i)
        self.cmb_level.currentIndexChanged.connect(self._on_level)
        lay.addLayout(self._row("日志等级", self.cmb_level))

        self.lbl_level = CaptionLabel("")
        self.lbl_level.setObjectName("hint")
        lay.addWidget(self.lbl_level)

        # ── 日志文件名模板 ────────────────────────────────────────
        self.edt_fmt = LineEdit()
        self.edt_fmt.setText(str(self.cfg.get("log_name_format",
                                             paths.LOG_NAME_FORMAT)))
        self.edt_fmt.setPlaceholderText(paths.LOG_NAME_FORMAT)
        # 每次按键都更新预览 (便宜), 但**只在编辑结束/回车时落盘** ——
        # 否则打一串模板会写十几次 config.json。
        self.edt_fmt.textChanged.connect(self._update_preview)
        self.edt_fmt.editingFinished.connect(self._persist_fmt)
        btn_default = TransparentPushButton("默认")
        btn_default.clicked.connect(self._reset_fmt)
        lay.addLayout(self._row("文件名模板", self._hrow(self.edt_fmt,
                                                         btn_default)))

        self.lbl_preview = CaptionLabel("")
        self.lbl_preview.setObjectName("hint")
        self.lbl_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_preview)

        self.lbl_cur = CaptionLabel("")
        self.lbl_cur.setObjectName("hint")
        self.lbl_cur.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_cur)

        # ── 查看 / 保存 ───────────────────────────────────────────
        lay.addWidget(StrongBodyLabel("日志文件"))
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.btn_view = TransparentPushButton("查看运行日志...")
        self.btn_view.clicked.connect(self._open_viewer)
        row2.addWidget(self.btn_view)
        self.btn_save = TransparentPushButton("保存日志文件...")
        self.btn_save.clicked.connect(lambda: save_log_file(self))
        row2.addWidget(self.btn_save)
        self.btn_console = TransparentPushButton("弹出命令行日志")
        self.btn_console.clicked.connect(open_console_tail)
        if sys.platform != "win32":
            self.btn_console.setEnabled(False)
        row2.addWidget(self.btn_console)
        row2.addStretch(1)
        lay.addLayout(row2)

        lay.addStretch(1)
        row3 = QHBoxLayout()
        row3.addStretch(1)
        btn_close = PrimaryPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        row3.addWidget(btn_close)
        lay.addLayout(row3)

        self._update_preview(self.edt_fmt.text())
        self._update_level_hint()

    # ------------------------------------------------------------ 布局小工具
    @staticmethod
    def _row(text, widget, label_w=76):
        """一行「标签 : 控件」—— 标签定宽, 各行左对齐 (与主面板同一套观感)。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = BodyLabel(text)
        lbl.setFixedWidth(label_w)
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        return row

    @staticmethod
    def _hrow(*widgets):
        """把几个控件横向摆成一个整体 (给 _row 当右侧用)。"""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        for w in widgets:
            h.addWidget(w)
        return box

    # ------------------------------------------------------------ 等级
    def _on_level(self, _idx):
        val = self.cmb_level.currentData()
        if not val:
            return
        # **先设等级**: 后面落盘如果打日志, 也按新等级走。
        wdlog.log.set_level(val)
        self.cfg["log_level"] = val
        self._persist()
        self._update_level_hint()

    def _update_level_hint(self):
        val = str(self.cfg.get("log_level", "info")).lower()
        if val == "off":
            txt = "已生效: 日志全部关闭 (界面上不会再收到任何日志)"
        else:
            txt = "已生效: 等级 %s —— 立即生效, 无需重启" % val
        self.lbl_level.setText(txt)

    # ------------------------------------------------------------ 文件名
    def _update_preview(self, text):
        name = paths.format_log_name(text) + ".log"
        if paths.log_name_custom_ok(text):
            self.lbl_preview.setText("下次启动生成:  %s" % name)
        else:
            self.lbl_preview.setText(
                "下次启动生成:  %s\n模板没产出可用名字, 已自动退回默认 (%s)"
                % (name, paths.LOG_NAME_FORMAT))
        # 当前这次运行的文件**不会**改名 —— 说清楚, 免得以为没生效。
        self.lbl_cur.setText(
            "本次运行:  %s\n(文件名只影响下一次启动; 本次的文件已打开)"
            % LOG_FILE)

    def _reset_fmt(self):
        self.edt_fmt.setText(paths.LOG_NAME_FORMAT)
        self._persist_fmt()

    def _persist_fmt(self):
        text = self.edt_fmt.text()
        if str(self.cfg.get("log_name_format", "")) == text:
            return
        self.cfg["log_name_format"] = text
        self._persist()

    # ------------------------------------------------------------ 落盘
    def _persist(self):
        if self._save_cb is None:
            return
        try:
            self._save_cb()
        except Exception as exc:  # noqa: BLE001
            wdlog.log.error("保存日志设置失败: %s" % exc, tag="log")

    def _open_viewer(self):
        LogDialog(self).exec()
