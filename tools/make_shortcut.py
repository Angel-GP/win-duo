"""在桌面/开始菜单创建 win-duo 快捷方式, 并把图标指到 win-duo.ico。

**为什么需要这一步**: Windows 的 .bat 文件本身没有"图标"这个概念 ——
资源管理器按扩展名给它一个通用图标, 你在 .bat 里写什么都改不了。
要看到自定义图标, 只能是**快捷方式** (.lnk), 由它去引用 .ico。

用法:
    .venv\\Scripts\\python.exe tools\\make_shortcut.py            # 只建桌面
    .venv\\Scripts\\python.exe tools\\make_shortcut.py --start-menu
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "win-duo.ico"
TARGET = ROOT / "run_now.bat"
LNK_NAME = "win-duo.lnk"


def create_shortcut(lnk_path, target, icon, workdir):
    """用 WScript.Shell 建 .lnk —— 不需要额外的 Python 依赖。"""
    import ctypes
    # pythoncom/win32com 没装, 所以借 PowerShell 的 COM 一句建完
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut('%s'); "
        "$s.TargetPath = '%s'; "
        "$s.WorkingDirectory = '%s'; "
        "$s.IconLocation = '%s,0'; "
        "$s.Description = 'win-duo 折叠屏悬浮玻璃'; "
        "$s.Save()"
        % (lnk_path, target, workdir, icon)
    )
    return ctypes.windll.shell32.ShellExecuteW(
        None, "open", "powershell", "-NoProfile -Command \"%s\"" % ps, None, 0)


def main():
    if not ICON.exists():
        print("[!] 还没有 %s, 先生成:  .venv\\Scripts\\python.exe tools\\make_icon.py"
              % ICON.name)
        return 1
    if not TARGET.exists():
        print("[!] 找不到启动器: %s" % TARGET)
        return 1

    desktop = Path(os.path.join(os.environ["USERPROFILE"], "Desktop"))
    made = []
    desktop_lnk = desktop / LNK_NAME
    create_shortcut(desktop_lnk, TARGET, ICON, ROOT)
    made.append(desktop_lnk)

    if "--start-menu" in sys.argv:
        sm = Path(os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                               "Start Menu", "Programs"))
        sm_lnk = sm / LNK_NAME
        create_shortcut(sm_lnk, TARGET, ICON, ROOT)
        made.append(sm_lnk)

    print("已创建快捷方式 (图标 -> %s):" % ICON.name)
    for p in made:
        print("  %s" % p)
    print("\n提示: 快捷方式的目标是 run_now.bat, 它用 pythonw 启动 —— 不留命令行窗口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
