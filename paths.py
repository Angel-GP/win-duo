"""运行期路径解析 —— 让源码运行和 PyInstaller 打包后的行为一致。

═══════════════════════════════════════════════════════════════════════
为什么需要这个
═══════════════════════════════════════════════════════════════════════

PyInstaller 打包后 `__file__` 指向**临时解包目录**（onefile 模式是
`C:\\Users\\...\\AppData\\Local\\Temp\\_MEIxxxxxx\\`），而且那个目录：
  - 每次启动路径都不一样
  - 进程退出就被删掉

所以任何"把文件放在自己旁边"的代码，打包后都会**写到一个马上消失的目录** ——
配置存不住、日志看不到、图标找不到。这个模块把这些路径统一收口：

  `data_dir()`     用户数据 (config.json / 日志 / win-duo.ico …)
  `resource_dir()` 只读资源 (打包进去的图标等, 位于解包目录)

判定规则：
  - **打包后**：`data_dir()` = exe 所在目录。用户看到的就是"程序旁边的配置
    文件"，符合绿色软件的习惯；只读资源仍从解包目录取。
  - **源码运行**：两者都是项目根目录。

这样上层代码不用到处写 `if frozen`。
"""
import os
import sys
import time
from pathlib import Path

import wdlog


def is_frozen():
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


#: 应用版本 —— **单一来源**, 发版时与 git tag 同步。
#:
#: 为什么要有它: 打包出来的 exe 文件属性里版本号是空的 (0.0.0.0), 收到一份
#: 运行日志时无从判断"这是哪个版本", 只能靠猜。banner 每次启动都把它打出来,
#: 日志本身就带上了版本身份。放在 paths.py 是因为它是唯一没有内部依赖的模块,
#: 谁都能 import 而不会绕出循环。
#:
#: 当前 = v1.4.0 (v1.3.3 之后的 15 个提交: 测角自愈/信号相关越界判据/WGC 会话
#: 重建/显隐阈值/显示器变化监听 + 日志系统整轮改造)。
__version__ = "1.4.0"


def _exe_dir():
    """exe 所在目录 (打包后)。"""
    return Path(sys.executable).resolve().parent


#: 项目根目录 —— 本文件就在根下, 所以是 parent 而不是 parent.parent。
#: (`ui/widgets.py` 那种一层深的模块才需要 parent.parent。)
_PROJECT_ROOT = Path(__file__).resolve().parent


def resource_dir():
    """**只读**资源目录。

    打包后是 PyInstaller 的解包目录 (`sys._MEIPASS`)；源码运行时是项目根目录。
    往里写文件是错的 —— 见模块开头。
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return _exe_dir()
    return _PROJECT_ROOT


def data_dir():
    """**可写**数据目录 —— 放 config.json / 日志 / 用户换的背景图。

    打包后落在 exe 旁边；源码运行就是项目根目录。
    这个目录在打包后可能没有写权限（比如装在 `C:\\Program Files`），所以下面
    还给了一个 `writable_data_dir()` 兜底。
    """
    if is_frozen():
        return _exe_dir()
    return _PROJECT_ROOT


def writable_data_dir():
    """确定可写的数据目录，必要时退到 `%LOCALAPPDATA%`。

    实测常见情况：用户把 exe 放进 `C:\\Program Files\\...`，那里普通用户没有
    写权限。此时如果硬写，配置和日志会静默失败 —— 表现为"改了设置重启就没了"，
    很难查。所以这里探测一次，写不进去就换地方，并且**打印一条说明**，
    免得用户找不到配置文件。
    """
    d = data_dir()
    try:
        probe = d / ".win-duo-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except Exception:  # noqa: BLE001
        fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        fallback = fallback / "win-duo"
        fallback.mkdir(parents=True, exist_ok=True)
        wdlog.log.warn("%s 不可写, 数据目录改用 %s" % (d, fallback), tag="paths")
        return fallback


# 注: 原来这里有个 `data_file(name)`。配置/日志收进 diagnostics/ 之后已改走
# config_file()/log_file()/debug_file(), 它就没有调用者了, 故删除。


# ═══════════════════════════════════════════════════════════════════════
# 分类目录: 配置 / 调试产物
# ═══════════════════════════════════════════════════════════════════════
# 让程序旁边不再是"一地散file": 配置进 diagnostics/config/, 日志与调试抓图进
# diagnostics/debug/。都建在**可写数据目录**下 (打包后 = exe 旁边)。
def config_dir():
    """配置文件目录: <数据目录>/diagnostics/config"""
    d = writable_data_dir() / "diagnostics" / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def debug_dir():
    """调试产物目录: <数据目录>/diagnostics/debug (日志 + 抓图)"""
    d = writable_data_dir() / "diagnostics" / "debug"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir():
    """日志目录: <数据目录>/diagnostics/debug/log"""
    d = debug_dir() / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file(name):
    """配置目录里的一个文件路径。"""
    return config_dir() / name


def log_file(name):
    """日志目录里的一个文件路径。"""
    return log_dir() / name


def debug_file(name):
    """调试目录里的一个文件路径 (日志的子目录同级)。"""
    return debug_dir() / name


def resource_file(name):
    """只读资源目录里的一个文件路径。"""
    return resource_dir() / name


# ═══════════════════════════════════════════════════════════════════════
# 日志文件名 —— 模板可自定义 (见设置窗口「高级设置 -> 调试 -> 日志」)
# ═══════════════════════════════════════════════════════════════════════
#: 默认模板 (strftime 记法): `2026-10-07-20-30-15.log`。
#: 一次运行一个日志文件, 名字由这个模板生成 —— 用户可以在设置里改。
LOG_NAME_FORMAT = "%Y-%m-%d-%H-%M-%S"

#: **本次运行正在写的日志文件** (由 main 在启动时写入)。
#: `None` = 本次不保存日志 (保留时间/数量里有一个是 0), 或者还没算出来。
#:
#: 为什么放在这里: 设置窗口的「立即清理」必须**保护**这个文件 (它就是用户正在
#: 看的这份日志), 而 `ui/` 不该 `import main` —— 那会绕出循环, 而且 main 有
#: 装日志 tee、设 DPI 感知、开摄像头这一堆副作用, 界面模块不该被它们牵连。
#: paths 是所有模块都能安全 import 的底层模块, 借它传一个值最省事。
CURRENT_LOG = None

#: Windows 文件名里不允许出现的字符 (含路径分隔符 —— 模板里带 `/` 的话
#: 会试图写到别的目录去)。
_BAD_NAME_CHARS = '\\/:*?"<>|'

#: Windows 的保留设备名。叫 CON.log 的文件根本建不出来 (报错很费解), 加前缀躲开。
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + ["com%d" % i for i in range(1, 10)]
    + ["lpt%d" % i for i in range(1, 10)])


def sanitize_log_stem(stem):
    """把模板产出的一段文本收拾成**安全的文件名主干** (不含 `.log`)。

    用户可编辑的模板必须当作不可信输入: 里面可能有路径分隔符 (会写到别的目录)、
    非法字符、控制字符, 或者干脆产出一个空串。这里一律替换/剥离, 让它只可能
    落成一个本目录下的普通文件名。
    """
    s = "".join("_" if (ch in _BAD_NAME_CHARS or ord(ch) < 32) else ch
                for ch in str(stem or ""))
    # 结尾的点和空格在 Windows 上会被**静默吃掉**, 导致"界面显示的名字"和
    # "磁盘上真正的名字"对不上 —— 提前剥掉。
    s = s.strip().rstrip(".")
    if s.lower() in _RESERVED_NAMES:
        s = "_" + s
    # NTFS 单个名字上限 255; 后面还要接 ".log" 和可能的 "-2" 去重后缀, 留足余量。
    return s[:80]


def format_log_name(fmt=None, when=None):
    """按模板生成日志文件名主干 (不含 `.log`)。

    `fmt` 是 strftime 模板 (如 `%Y-%m-%d-%H-%M-%S`)。**任何情况下都会返回一个
    可用的名字**: 模板为空、不是字符串、strftime 报错、或者收拾完变成空串,
    都退回内置的 `LOG_NAME_FORMAT`。

    为什么要这么兜: 这个名字是在**进程启动最早期**算的 (faulthandler 和日志 tee
    都要用它), 那时还没有任何界面能把错误显示给用户。一个写坏的模板绝不能变成
    "启动时抛异常"或者"日志凭空消失"。
    """
    when = time.time() if when is None else when
    stem = ""
    if fmt:
        try:
            stem = time.strftime(str(fmt), time.localtime(when))
        except Exception:  # noqa: BLE001  模板里有非法指令等
            stem = ""
    stem = sanitize_log_stem(stem)
    if not stem:
        stem = time.strftime(LOG_NAME_FORMAT, time.localtime(when))
    return stem


def log_name_custom_ok(fmt):
    """这个模板能不能产出一个可用的名字? (给设置界面做提示用)

    设置窗口要在用户**还没保存**时就告诉他"你写的模板其实会被退回默认" ——
    光看 `format_log_name` 的返回值分不出来 (退回默认时它照样返回一个合法名字,
    那正是它该做的)。
    """
    if not fmt or not str(fmt).strip():
        return False
    try:
        raw = time.strftime(str(fmt), time.localtime())
    except Exception:  # noqa: BLE001
        return False
    return bool(sanitize_log_stem(raw))
