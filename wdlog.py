"""分级日志 —— win-duo 全项目统一的日志入口。

═════════════════════════════════════════════════════════════════════════
为什么不用标准库 logging
═════════════════════════════════════════════════════════════════════════
项目原本全是裸 print(), 靠 main.py 的 _TeeLogger 把 stdout/stderr tee 进
win_duo.log。改成标准库 logging 要么另开一套输出通道 (日志窗口/tee 读不到),
要么 handler 写 stdout 再被 tee —— 绕一圈。本模块直接沿用 tee 通道:
    log() -> stdout(已被 _TeeLogger 接管) -> 控制台(带 ANSI 颜色) + 日志文件
这样 ui/log_dialog.py 和"弹出命令行日志"窗口**不需要任何改动**就能继续工作,
文件里写的是剥掉颜色码的纯文本 (见 _strip_ansi)。

═════════════════════════════════════════════════════════════════════════
等级
═════════════════════════════════════════════════════════════════════════
    FATAL   进程马上要退出/已无法继续提供服务
    ERROR   一次操作失败, 功能受损 (设备打不开、写盘失败、GL 编译失败)
    WARN    异常但已兜底/降级 (后端回退、配置损坏重建、DPI 虚拟化)
    INFO    用户关心的关键事件 (开关玻璃层、切角度源、配置保存)
    DEBUG   诊断用的详细过程 (设备选择、标定、扫描细节)
    TRACE   高频/逐帧数据 (状态行、匹配数) —— 默认永不开启, 专门留给排查
    ALL     等同 TRACE (全部输出)
    OFF     一条都不输出

默认等级 INFO。--log-level 可调, --plain 关掉 ANSI 颜色。

线程安全: 只在 _emit 里做一次 sys.stdout.write, CPython 的 write 本身有
GIL 保护; 多线程交错最多在"行内", 不在行间 (每条日志一次 write 一行)。
"""
import re
import sys
import time
import unicodedata

# ═══════════════════════════════════════════════════════════════════
# 等级定义
# ═══════════════════════════════════════════════════════════════════
FATAL = 50
ERROR = 40
WARN = 30
INFO = 20
DEBUG = 10
TRACE = 5
ALL = TRACE          # 别名: --log-level all = 全开
OFF = 0              # 别名: --log-level off = 全关

#: 名字 -> 数值 (含 ALL/OFF 两个别名)。数值越小越啰嗦。
_LEVELS = {
    "fatal": FATAL, "error": ERROR, "warn": WARN, "warning": WARN,
    "info": INFO, "debug": DEBUG, "trace": TRACE,
    "all": ALL, "off": OFF,
}

#: 数值 -> 显示名 (固定 5 字符宽, 时间戳后对齐)
_NAMES = {
    FATAL: "FATAL", ERROR: "ERROR", WARN: "WARN ", INFO: "INFO ",
    DEBUG: "DEBUG", TRACE: "TRACE",
}

# ═══════════════════════════════════════════════════════════════════
# ANSI 颜色 (按等级)
# ═══════════════════════════════════════════════════════════════════
_CSI = "\x1b["
_RESET = _CSI + "0m"
#: EL (Erase in Line): 从光标清到行尾。擦状态行残尾用, 比"按字符数补空格"
#: 可靠 —— 中文在终端上占 2 格, len() 数不出来。
_EL = _CSI + "K"


def _display_width(s):
    """终端显示宽度: 东亚宽字符 (CJK/全角) 按 2 算, 其余按 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
               for ch in s)
#: 每级一个颜色: FATAL/ERROR 红, WARN 黄, INFO 默认, DEBUG 青, TRACE 暗灰
_COLORS = {
    FATAL: "1;97;41",     # 高亮白字红底
    ERROR: "1;91",        # 亮红
    WARN: "1;93",         # 亮黄
    INFO: "1;97",         # 亮白 (比正文醒目一点)
    DEBUG: "1;96",        # 亮青
    TRACE: "90",          # 暗灰
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s):
    """剥掉 ANSI 颜色码。_TeeLogger 写文件前用它, 日志文件保持纯文本。"""
    return _ANSI_RE.sub("", s)


class Logger:
    """进程级单例 (见模块尾部的 log)。"""

    def __init__(self):
        self.level = INFO
        self.color = True
        #: 程序启动时刻 (秒)。时间戳用它算偏移 —— 比 datestr 短, 行更窄;
        #: 排查时更关心"相对启动过了多久"而不是绝对日期。
        self._t0 = time.time()
        #: 上一条日志的时刻 (秒), 给时间戳旁边的增量 (+0.012s) 用。
        #: 两行间隔一眼可见, 抓"哪一步卡了两秒"特别有用。
        self._last = None
        #: overlay 的 \r 状态行: 当前行内容 (None = 没画)。
        #: 日志输出前要"擦掉它 -> 写日志 -> 画回去" (见 _emit), 这样状态行
        #: 永远贴在控制台最后一行原地刷新, 日志行也不会糊在它上面。
        self._status = None

    # ------------------------------------------------------------ 配置
    def set_level(self, name):
        """按名字设等级; 不认识就保持原样并返回 False。"""
        lv = _LEVELS.get(str(name).strip().lower())
        if lv is None:
            return False
        self.level = lv
        return True

    def set_color(self, on):
        self.color = bool(on)

    def _write(self, s):
        try:
            out_write = getattr(sys.stdout, "write", None)
            if out_write is not None:
                out_write(s)
        except Exception:  # noqa: BLE001  控制台没了 (关窗) 也不能把业务代码带走
            pass

    @staticmethod
    def _terminal_cols():
        """当前控制台的列数。取不到 (重定向/非常规终端) 时退回保守的 80。

        优先 Win32 GetConsoleScreenBufferInfo —— 它返回的是**缓冲区**宽度,
        不受 `python | grep` 之类影响; 取不到再用 shutil (查 stdout, 重定向下
        会失败)。
        """
        if sys.platform == "win32":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE

                class _CSBI(ctypes.Structure):
                    _fields_ = [
                        ("dwSize", ctypes.c_short * 2),
                        ("dwCursorPosition", ctypes.c_short * 2),
                        ("wAttributes", ctypes.c_ushort),
                        ("srWindow", ctypes.c_short * 4),
                        ("dwMaximumWindowSize", ctypes.c_short * 2),
                    ]

                csbi = _CSBI()
                if k32.GetConsoleScreenBufferInfo(h, ctypes.byref(csbi)):
                    # srWindow = 可见窗口的 (L,T,R,B), 宽度按可见列算:
                    # \r 擦写在可见区域内, 用缓冲区宽度 (可能比窗口宽) 会截短
                    # 失败, 用窗口宽度才保证不折行。
                    return int(csbi.srWindow[2] - csbi.srWindow[0])
            except Exception:  # noqa: BLE001
                pass
        try:
            import shutil
            cols = shutil.get_terminal_size().columns
            if cols and cols > 0:
                return int(cols)
        except Exception:  # noqa: BLE001
            pass
        return 80

    def status_line(self, text):
        """TRACE 状态行的专用入口: \\r 原地刷新 (永远贴控制台最后一行)。

        **必须按终端列数截断**: \\r/EL 只对"当前物理行"生效, 状态行一旦比
        终端窄 (中文显示宽度算 2 格), 终端就把它折成多行 —— 擦除只擦得到
        最后一折, 前面几折留在屏幕上, 每秒十次刷新就是滚屏雪崩 (换分辨率/
        缩放/字号后列数变窄的设备上必现)。截断到 cols-1, 留 1 格余量防
        个别终端的自动换行竞态。

        旧内容比新内容长时, 用 EL ("\\x1b[K", 清到行尾) 擦掉残尾 —— 中文
        按字符数补空格会算错, EL 一刀切最稳。plain 模式按"显示宽度"补空格。
        """
        if self.level > TRACE or self.level == OFF:
            return
        # 截断按"显示宽度" (CJK 算 2), 而不是字符数
        cols = self._terminal_cols() - 1
        if _display_width(text) > cols:
            out, w = [], 0
            for ch in text:
                cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
                if w + cw > cols:
                    break
                out.append(ch)
                w += cw
            text = "".join(out)
        if self.color:
            # EL 在刷新频率高时也比对空格便宜
            self._status = text
            self._write("\r" + text + _EL)
        else:
            if self._status is not None:
                old_w = _display_width(self._status)
                w = _display_width(text)
                if w < old_w:
                    text = text + " " * (old_w - w)
            self._status = text
            self._write("\r" + text)

    # ------------------------------------------------------------ 输出
    def _emit(self, level, tag, msg):
        if level < self.level or self.level == OFF:
            return
        now = time.time()
        dt = 0.0 if self._last is None else now - self._last
        self._last = now
        stamp = "%6.1f (+%5.2f)" % (now - self._t0, dt)
        name = _NAMES.get(level, str(level))

        # 整行 "[LEVEL] [tag] 消息" 上色, 时间戳保持素色 —— 刷屏时眼睛扫的
        # 是颜色, 不是时间。plain 模式直接给素文本。
        # 经 stdout 走 _TeeLogger: 控制台得到颜色, 文件得到 _strip_ansi 后的
        # 纯文本 (见 main._TeeLogger.write)。
        if self.color:
            c = _COLORS.get(level)
            if c:
                line = "%s %s%sm[%s %s]%s %s\n" % (
                    stamp, _CSI, c, name, tag, _RESET, msg)
            else:
                line = "%s [%s %s] %s\n" % (stamp, name, tag, msg)
        else:
            line = "%s [%s %s] %s\n" % (stamp, name, tag, msg)
        # 状态行"贴底"协议: 写日志前先把状态行擦掉 (\r 回行首 + EL 清到行尾,
        # plain 模式按显示宽度补空格), 写完日志再画回最后一行。效果:
        # 日志正常滚屏, 状态行永远停在屏幕底部原地刷新, 互不干扰。
        if self._status is not None:
            if self.color:
                self._write("\r" + _EL)
            else:
                self._write("\r" + " " * _display_width(self._status) + "\r")
        self._write(line)
        if self._status is not None:
            self._write("\r" + self._status
                        + (_EL if self.color else ""))


#: 全局单例。各模块 `from wdlog import log` 后 log.info("...", tag="glass")。
log = Logger()

# 便捷方法直接挂在实例上 (模块级函数与实例方法二选一会引起两套调用习惯,
# 统一只用 log.xxx)。无法确定等级的调用点先按 INFO 打, 留待用户复核。
for _lv, _name in ((FATAL, "fatal"), (ERROR, "error"), (WARN, "warn"),
                   (INFO, "info"), (DEBUG, "debug"), (TRACE, "trace")):
    def _make(level):
        def _f(msg, tag="app"):
            log._emit(level, tag, msg)
        return _f
    setattr(log, _name, _make(_lv))
