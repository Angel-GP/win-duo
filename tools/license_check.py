"""校验许可证声明文件的完整性与准确性。

**为什么要写这个**: 上传 GitHub 前必须如实声明依赖的许可证。GPL-3.0 有传染性,
漏一个就可能让整份声明失效。这个脚本把"实际装了什么"和"文档里写了什么"
对起来, 免得手写清单漂移。

用法:
    .venv\\Scripts\\python.exe tools\\license_check.py
"""
import re
import sys
from importlib.metadata import distributions, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

FAILS = []


def check(label, cond, extra=""):
    print("  [%s] %-44s %s" % ("OK" if cond else "FAIL", label, extra))
    if not cond:
        FAILS.append(label)


#: 本项目**直接**依赖。每个都必须在 THIRD_PARTY_NOTICES.md 里出现。
DIRECT = ("PyQt6", "PyQt6-Fluent-Widgets", "PyOpenGL", "numpy",
          "opencv-python", "mss", "bettercam", "Pillow", "pyserial")

#: Windows 上的传递依赖 (本项目代码不直接调用, 但装了就要声明)。
TRANSITIVE = ("PyQt6-Qt6", "PyQt6-sip", "PyQt6-Frameless-Window",
              "darkdetect", "comtypes", "pywin32", "setuptools")

#: 已知的强 copyleft 库。它们决定了本项目**必须**用 GPL 发布。
COPYLEFT = {
    "PyQt6": "GPL-3.0-only",
    "PyQt6-Fluent-Widgets": "GPLv3",
    "PyQt6-Frameless-Window": "GPLv3",
}


def main():
    notices = ROOT / "THIRD_PARTY_NOTICES.md"
    license_file = ROOT / "LICENSE"
    readme = ROOT / "README.md"

    print("=" * 70)
    print("许可证声明校验")
    print("=" * 70)

    print("\n① 声明文件都在:")
    check("THIRD_PARTY_NOTICES.md 存在", notices.exists())
    check("LICENSE 存在", license_file.exists())
    if not notices.exists() or not license_file.exists():
        return 1

    src = notices.read_text(encoding="utf-8")
    lic = license_file.read_text(encoding="utf-8")
    rd = readme.read_text(encoding="utf-8")

    print("\n② 直接依赖都列了:")
    for pkg in DIRECT:
        check(pkg, pkg in src)

    print("\n③ 传递依赖也列了:")
    for pkg in TRANSITIVE:
        check(pkg, pkg in src)

    print("\n④ 实际装着的包有没有漏:")
    installed = {}
    for d in distributions():
        n = d.metadata["Name"]
        if n:
            installed[n] = d
    # 排除**打包/开发工具** —— 它们不随程序分发, 没有声明义务。
    # (PyInstaller 只在打包时用到, 不会进 exe; 它的依赖同理。)
    skip = {
        "pip", "setuptools", "wheel",
        "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
        "pywin32-ctypes", "importlib-metadata", "zipp", "packaging",
    }
    missing = []
    for name in sorted(installed):
        norm = name.lower().replace("_", "-")
        if norm in skip:
            continue
        # 文档里的写法可能带连字符/大小写差异, 宽松匹配
        if norm not in src.lower().replace("_", "-"):
            missing.append(name)
    check("没有未声明的已装包", not missing, str(missing) if missing else "")

    print("\n⑤ GPL 传染性链条是否说清了:")
    for pkg, lics in COPYLEFT.items():
        check("%s 标了 %s" % (pkg, lics.split("-")[0]), pkg in src and "GPL" in src)
    check("LICENSE 明确声明 GPL-3.0", "GPL-3.0" in lic or "GPLv3" in lic)
    # GPL-3.0 §4 要求随程序附上**许可证完整正文**, 只给一个 URL 不合规。
    # 所以这里卡死"正文在不在", 而不是只看有没有写 "GPL-3.0" 这几个字。
    check("LICENSE 含 GPL-3.0 完整正文",
          "GNU GENERAL PUBLIC LICENSE" in lic
          and "Version 3, 29 June 2007" in lic
          and "TERMS AND CONDITIONS" in lic
          and "END OF TERMS AND CONDITIONS" in lic
          and "How to Apply These Terms" in lic,
          "%d 行" % lic.count("\n"))
    check("README 也说明了 GPL-3.0", "GPL-3.0" in rd)
    check("写明了改用宽松许可的条件", "PySide6" in src)

    print("\n⑥ 上游出处是否如实:")
    check("列了 WindowsDuo (MIT)", "WindowsDuo" in src and "MIT" in src)
    # MIT 要求保留版权声明 + 许可声明。现在 LICENSE 是纯 GPL 正文, 所以这两样
    # 落在 THIRD_PARTY_NOTICES.md 里 —— 校验要跟着改, 否则会误报。
    check("MIT 版权声明原样保留", "KaedeharaKazuha1029" in src
          and "Permission is hereby granted" in src)

    print("\n⑦ 文档里的最低版本要求不高于实际装的:")
    # 文档写的是**最低要求** (如 `Pillow>=10.0`), 实际装的通常更高 (11.3)。
    # 所以这里比的是"文档要求的 >= 版本 <= 实际装的版本", 而不是大版本相等
    # —— 后者会把正常的升级误报成问题 (踩过一次)。
    doc = notices.read_text(encoding="utf-8")
    for pkg in ("PyQt6", "PyQt6-Fluent-Widgets", "numpy", "opencv-python",
                "mss", "bettercam", "Pillow", "pyserial", "PyOpenGL"):
        try:
            v = version(pkg)
        except Exception:  # noqa: BLE001
            print("  (未安装, 跳过 %s)" % pkg)
            continue
        # 按行找, 而不是用一个跨字段的大正则 —— 表格里包名后面紧跟着
        # `](url) | ≥6.5 |`, 用 `[^\n|]*?` 这种"不含竖线"的写法会直接失配
        # (踩过一次)。这里逐行处理, 只要求同一行里既有包名又有 ≥ 版本。
        need = None
        for line in doc.splitlines():
            if pkg not in line:
                continue
            # 版本要求写法是 `≥6.5`（全角 ≥ 后**直接跟数字，没有等号**），
            # 也兼容 `>=6.5`。所以正则不能写成 `[≥>]=` —— 那样要求必须有等号,
            # 会全部失配 (踩过一次, 排查了两轮)。
            m = re.search(r"(?:[≥>]=?|&gt;=?)\s*([\d.]+)", line)
            if m:
                need = m.group(1)
                break
        if need is None:
            check("%s 标了最低版本" % pkg, False, "文档里没写 ≥ 要求")
            continue
        need_t = tuple(int(x) for x in need.split(".") if x.isdigit())
        have_t = tuple(int(x) for x in v.split(".")[:len(need_t)] if x.isdigit())
        check("%s 实际版本满足最低要求" % pkg, have_t >= need_t,
              "要求 ≥%s, 实装 %s" % (need, v))

    print("-" * 70)
    if FAILS:
        for f in FAILS:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 许可证声明完整且与实际依赖一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
