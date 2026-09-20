"""把 win-duo 打包成单文件 exe。

用法:
    .venv\\Scripts\\python.exe scripts\\build_exe.py            # 单文件, 带控制台日志
    .venv\\Scripts\\python.exe scripts\\build_exe.py --onedir    # 目录版 (启动更快)
    .venv\\Scripts\\python.exe scripts\\build_exe.py --clean     # 先清掉旧产物

═══════════════════════════════════════════════════════════════════════
几个必须处理的点 (都踩过或差点踩到)
═══════════════════════════════════════════════════════════════════════

1. **`win-duo.ico` 是数据文件, PyInstaller 不会自动发现。**
   必须显式 `--add-data`。不放进去的话打包后 `make_icon()` 会退回运行时绘制
   —— 不崩, 但任务栏/托盘图标会变差 (少 9 档尺寸)。

2. **`config.json` 不打进 exe, 仓库里也不放它。** 它是用户数据, 首次启动时
   由 `main.DEFAULT_CFG` 在 exe 旁边现生成 (见 main.load_config /
   _seed_config_if_missing)。所以这里没有它的 `--add-data`。

3. **`bettercam` / `dxcam` 是 `__import__` 动态导入的, PyInstaller 的静态分析
   看不到。** 必须 `--hidden-import`, 否则打包后 DXGI 抓屏失效, 静默退回 mss
   (重截频率上限从 165Hz 掉到 37Hz, 用户只会觉得"变卡了")。

4. **`cv2` 有大量 OpenCV 二进制, 用 `--collect-binaries`** 或依赖 PyInstaller
   自带的 hook。这里用 hook + 排除掉用不到的。

5. **单文件模式启动慢 (要解压 ~400MB)。** 实测首次启动可能 10 秒以上。
   介意就用 `--onedir`。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

NAME = "win-duo"

#: 动态导入、PyInstaller 静态分析抓不到的模块。
HIDDEN = [
    "bettercam", "dxcam",          # __import__ 动态导入 (见文件头第 3 条)
    "comtypes", "comtypes.client",  # bettercam 的 COM 依赖
    "winreg",                      # 开机自启
    "serial", "serial.tools.list_ports",   # pyserial, 只在串口模式用
    "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets",
    "PyQt6.QtSvg", "PyQt6.QtSvgWidgets",   # qfluentwidgets 的图标用
    "qfluentwidgets",
]

#: 确定用不到的大块头, 排除掉能省很多体积。
EXCLUDE = [
    "tkinter", "matplotlib", "scipy", "pandas", "IPython", "jupyter",
    "notebook", "pytest", "setuptools", "pip", "wheel",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.Qt3DCore", "PyQt6.QtBluetooth",
    "PyQt6.QtCharts", "PyQt6.QtDataVisualization", "PyQt6.QtDesigner",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets", "PyQt6.QtNetworkAuth",
    "PyQt6.QtNfc", "PyQt6.QtPdf", "PyQt6.QtPdfWidgets", "PyQt6.QtPositioning",
    "PyQt6.QtRemoteObjects", "PyQt6.QtSensors", "PyQt6.QtSerialPort",
    "PyQt6.QtSpatialAudio", "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtTextToSpeech",
    "PyQt6.QtWebChannel", "PyQt6.QtWebSockets", "PyQt6.QtHelp",
]


def build(onedir=False, clean=False, console=False, name=NAME):
    if clean:
        for d in (ROOT / "build", ROOT / "dist"):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                print("[clean] 删掉 %s" % d)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir" if onedir else "--onefile",
        "--console" if console else "--windowed",
        "--name", name,
        # 图标: 同时用作 exe 的图标资源和运行时图标文件
        "--icon", str(ROOT / "win-duo.ico"),
        # ---- 数据文件 ----
        # win-duo.ico: 运行时 make_icon() 会去解包目录找它 (9 档尺寸)
        "--add-data", "%s%s." % (ROOT / "win-duo.ico", os.pathsep),
        # 注意: 不再打包 config.json。它是用户数据, 首次启动时由 main.DEFAULT_CFG
        # 在 exe 旁边现生成 (见 main.load_config / _seed_config_if_missing)。
    ]

    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]

    # 项目根要能被 import (paths.py / angles / render / ui)
    cmd += ["--paths", str(ROOT)]
    cmd += [str(ROOT / "main.py")]

    print("[build] " + " ".join(cmd[:14]) + " ...")
    print("[build] 工作目录: %s" % ROOT)
    print()
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print("\n[!] PyInstaller 失败, 退出码 %d" % rc)
        return rc

    out = ROOT / "dist" / (name if onedir else name + ".exe")
    if onedir:
        out = ROOT / "dist" / name / (name + ".exe")
    if not out.exists():
        print("[!] 没找到产物: %s" % out)
        return 1

    size = (sum(f.stat().st_size for f in out.parent.rglob("*") if f.is_file())
            if onedir else out.stat().st_size)
    print("\n" + "=" * 66)
    print("产物: %s" % out)
    print("大小: %.1f MB" % (size / 1048576))
    print("=" * 66)
    print("""
使用说明:
  1. 双击 %s 即可 —— 默认驻留托盘, 不弹窗。
  2. **config.json 在 exe 旁边**, 首次启动会自动生成。
     想在别的机器上用, 把 exe 和 config.json 一起拷过去。
  3. 日志也在 exe 旁边: win_duo.log (或界面里「高级设置 -> 调试 -> 查看运行日志」)。
  4. 关不掉时按 Ctrl+Alt+Shift+Esc (关玻璃层并退出)。
""" % (name + ".exe"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true",
                    help="目录版 (启动快, 但一堆文件)")
    ap.add_argument("--clean", action="store_true", help="先清掉旧产物")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口 (排查启动问题时用)")
    ap.add_argument("--name", default=NAME)
    args = ap.parse_args()

    # 打包前先确认图标在
    if not (ROOT / "win-duo.ico").exists():
        print("[!] 缺 win-duo.ico (仓库里应带着它)")
        return 1
    return build(onedir=args.onedir, clean=args.clean,
                 console=args.console, name=args.name)


if __name__ == "__main__":
    sys.exit(main())
