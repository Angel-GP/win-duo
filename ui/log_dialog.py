"""日志查看与导出弹窗。"""
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QFileDialog, QHBoxLayout, QVBoxLayout

from paths import data_file
from .widgets import BodyLabel, PlainTextEdit, PrimaryPushButton, TransparentPushButton, make_icon

#: 日志文件和 config.json 放在一起 (打包后是 exe 旁边, 不是临时解包目录)
LOG_FILE = data_file("win_duo.log")


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
        self.btn_refresh.clicked.connect(self.refresh_log)
        btn_row.addWidget(self.btn_refresh)

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

    def refresh_log(self):
        content = get_all_logs()
        if content != self.text_edit.toPlainText():
            self.text_edit.setPlainText(content)
            sb = self.text_edit.verticalScrollBar()
            if sb:
                sb.setValue(sb.maximum())

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
            print("[log] 保存日志失败:", exc)
