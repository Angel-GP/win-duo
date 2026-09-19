"""Windows 开机自启 —— 写 HKCU 的 Run 键。

只动 HKCU (当前用户), 不需要管理员权限, 也不碰系统级设置。
用 pythonw.exe 启动, 开机时不会弹一个黑色控制台窗口。
"""
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows: 让模块仍可导入, 功能全部降级为"不可用"
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "win-duo"


def available():
    return winreg is not None


def _pythonw():
    """优先 pythonw.exe: 它没有控制台, 开机自启时不会闪黑窗。

    **打包成 exe 后没有 pythonw.exe 这一说** —— 直接返回 exe 自己。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return cand if cand.exists() else exe


def launch_command():
    """写进注册表的完整命令行。"""
    if getattr(sys, "frozen", False):
        # 打包后: 直接就是 exe 本身, 不带脚本路径 (没有 main.py 了)
        return '"%s" --tray' % Path(sys.executable)
    script = Path(__file__).resolve().parent.parent / "main.py"
    return '"%s" "%s" --tray' % (_pythonw(), script)


def is_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def current_value():
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0]
    except OSError:
        return None


def enable():
    if winreg is None:
        raise RuntimeError("当前平台不支持开机自启")
    cmd = launch_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    print("[autostart] 已开启: %s" % cmd)
    return cmd


def disable():
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        print("[autostart] 已关闭")
    except FileNotFoundError:
        pass
    except OSError as exc:
        print("[autostart] 关闭失败: %s" % exc)


def set_enabled(on):
    if on:
        return enable()
    disable()
    return None
