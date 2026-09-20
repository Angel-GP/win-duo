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

  `data_dir()`     用户数据 (config.json / win_duo.log / win-duo.ico …)
  `resource_dir()` 只读资源 (打包进去的图标等, 位于解包目录)

判定规则：
  - **打包后**：`data_dir()` = exe 所在目录。用户看到的就是"程序旁边的配置
    文件"，符合绿色软件的习惯；只读资源仍从解包目录取。
  - **源码运行**：两者都是项目根目录。

这样上层代码不用到处写 `if frozen`。
"""
import os
import sys
from pathlib import Path


def is_frozen():
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


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
        print("[paths] %s 不可写, 数据目录改用 %s" % (d, fallback))
        return fallback


def data_file(name):
    """数据目录里的一个文件路径。"""
    return writable_data_dir() / name


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
