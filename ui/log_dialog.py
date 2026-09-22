"""日志查看与导出弹窗。"""
import subprocess
import sys
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QVBoxLayout

import wdlog
from paths import log_file
from .widgets import BodyLabel, PlainTextEdit, PrimaryPushButton, TransparentPushButton, make_icon

#: 日志文件: <数据目录>/diagnostics/debug/log/win_duo.log (打包后是 exe 旁边)
LOG_FILE = log_file("win_duo.log")


def get_all_logs() -> str:
    """读日志文件内容。

    日志是 main.py 把 stdout 重定向到 `win_duo.log` 写的, 所以**唯一**的来源
    就是那个文件 —— 原来还有一套 `_MEMORY_LOGS` 内存缓冲 + `record_log()`,
    但 `record_log()` 全项目从没被调用过, 缓冲永远是空的, 于是这里每次都得
    落到磁盘那条分支。那套东西是没接上的半成品, 已删除。
    """
    if not LOG_FILE.exists():
        return ""
    try:
        return LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


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
            sb.setValue(min(prev, sb.maximum()))

    def _open_console(self):
        """新开一个原生控制台窗口, 实时 tail 日志文件。

        为什么这么做: 打包成 exe 是 **--windowed** 的, 进程没有控制台, 平时看不到
        stdout。而 main.py 把所有 stdout/stderr 都 tee 进了 win_duo.log, 所以这里
        另起一个控制台窗口去实时跟随那个文件, 就等于"随时调出一个命令行日志窗"。
        (不动主进程的流, 只读文件 —— 安全、不影响运行。)

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

    def save_log(self):
        target, _ = QFileDialog.getSaveFileName(
            self, "保存日志文件", f"win_duo_{time.strftime('%Y%m%d_%H%M%S')}.log",
            "日志文件 (*.log);;文本文件 (*.txt);;所有文件 (*)")
        if not target:
            return
        content = self.text_edit.toPlainText()
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:  # noqa: BLE001
            wdlog.log.error("保存日志失败: %s" % exc, tag="log")
