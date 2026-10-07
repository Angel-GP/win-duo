"""机器配置摘要 —— 给启动 banner 用: 出问题时先看清跑在什么硬件上。

═══════════════════════════════════════════════════════════════════════
为什么单独一个模块
═══════════════════════════════════════════════════════════════════════

这里全是 winreg / ctypes 查询, 跟渲染和界面无关。独立出来是为了**能被直接
测试** —— `import main` 会顺带装日志 tee、建日志文件、设 DPI 感知, 代价太大,
verify 脚本为了测一个 CPU 名字不值得付这个钱。

═══════════════════════════════════════════════════════════════════════
为什么要打这些
═══════════════════════════════════════════════════════════════════════

这个程序是"OpenGL 覆盖层 + 摄像头测角", 所以下面三件事直接决定它能不能正常
工作, 而它们都不是代码能控制的:

  - **显卡**: 混合显卡笔记本上 GL 上下文落在核显还是独显由驱动决定; 而虚拟
    显示适配器 (投屏 / 远程桌面 / 串流软件装的) 也会混在里面。覆盖层画不出来、
    截屏抓到黑屏时, 必须先知道这台机器上到底有几块适配器。
    (`render/overlay.py` 那条 `[INFO gl]` 只报**正在渲染**的那一块, 而且要等
     GL 上下文建好才有 —— 托盘模式下那是懒建的, 比 banner 晚得多。)
  - **内存**: 低内存模式 / 分级释放就是冲内存来的, 总容量和当前占用是判据。
  - **CPU**: 截屏 (DXGI/WGC) 与 ORB 特征匹配都是 CPU 活, 线程数影响上限。

═══════════════════════════════════════════════════════════════════════
契约: 永不抛异常
═══════════════════════════════════════════════════════════════════════

这些函数都在**启动路径**上, 唯一职责是"尽量多打印一点信息"。所以每一项都
单独兜底: 取不到就返回 None / 空列表 / 空串, 由调用方决定跳过还是写"(未知)"。
绝不该因为查不到配置而挡住程序启动。
"""
import ctypes
import os
import struct

try:
    import winreg
except ImportError:                    # 非 Windows: 所有查询直接返回空
    winreg = None


#: CPU 型号在注册表里的位置 (只取 0 号核心 —— 多路机器每个核心一份, 型号相同)
_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"

#: 显示适配器类 (GUID 是微软固定的"显示适配器"类); 下面 0000/0001/... 每块一个
_GPU_CLASS = (r"SYSTEM\CurrentControlSet\Control\Class"
              r"\{4d36e968-e325-11ce-bfc1-08002be10318}")


def bits():
    """当前**进程**的位数 (32 / 64)。

    注意是进程而不是系统: 64 位 Windows 上跑 32 位 exe 时这里是 32 —— 那才是
    有意义的那个数 (PyInstaller 产物、OpenGL 库、摄像头 SDK 的位数都要跟它
    对齐)。struct.calcsize("P") 是标准写法, 不依赖 platform。
    """
    try:
        return struct.calcsize("P") * 8
    except Exception:  # noqa: BLE001
        return 0


def cpu_name():
    """CPU 型号字符串; 取不到返回 None。

    走注册表而不是 `platform.processor()`: 后者在 Windows 上给的是
    `AMD64 Family 25 Model 97 Stepping 2, AuthenticAMD` 这种**家族标识**,
    看不出是什么 CPU。注册表里的 ProcessorNameString 才是
    `AMD Ryzen 9 7940HX with Radeon Graphics`。
    """
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY) as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
        # 注册表里的值**尾部带一串填充空格** (定长字段), 不归一化就会在日志里
        # 拉出一片空白。顺手把中间的多余空白也压掉。
        return " ".join(str(name).split())
    except Exception:  # noqa: BLE001
        return None


def cpu_threads():
    """逻辑处理器数量; 取不到返回 0。"""
    try:
        return int(os.cpu_count() or 0)
    except Exception:  # noqa: BLE001
        return 0


class _MEMORYSTATUSEX(ctypes.Structure):
    """`GlobalMemoryStatusEx` 的输出结构 (字段顺序即 ABI, 不能改)。"""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory():
    """`(总物理内存, 可用物理内存, 占用百分比)` (字节 / 字节 / %); 取不到 None。

    `dwLength` 必须先填结构体大小, 否则调用直接失败 —— 这是该 API 的约定。
    """
    if not hasattr(ctypes, "windll"):        # 非 Windows
        return None
    try:
        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullTotalPhys), int(st.ullAvailPhys), int(st.dwMemoryLoad)
    except Exception:  # noqa: BLE001
        return None


def gpus(limit=6):
    """`[(适配器名, 驱动版本), ...]`; 取不到返回空列表。

    **列的是全部适配器, 不只是正在渲染的那一块** —— 这正是它的价值: 混合显卡
    笔记本上会有核显 + 独显两条, 装了投屏/远程/串流软件的还会有虚拟显示适配器。
    覆盖层不显示、截屏全黑时, 这三者要一起看。

    驱动版本是 Windows 那一套编号 (如 `32.0.15.9186`), 与 NVIDIA 官网的
    `591.86` 不是同一个编号体系 —— 后者在 `[INFO gl]` 那行里, 两边对照着看。
    """
    if winreg is None:
        return []
    out = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS) as root:
            i = 0
            while len(out) < limit:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:              # 枚举到头
                    break
                i += 1
                if not sub.isdigit():        # 类键下还有 Properties 等非编号子键
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        _GPU_CLASS + "\\" + sub) as key:
                        desc = " ".join(
                            str(winreg.QueryValueEx(key, "DriverDesc")[0]).split())
                        try:
                            drv = str(
                                winreg.QueryValueEx(key, "DriverVersion")[0])
                        except OSError:
                            drv = ""
                    out.append((desc, drv))
                except OSError:
                    continue                 # 单个子键读不了 (权限/损坏) 就跳过
    except Exception:  # noqa: BLE001
        return out
    return out


def format_bytes(n):
    """人类可读的字节数 (如 `15.2 GB`); 输入无效返回 `?`。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n
