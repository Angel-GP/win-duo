"""win-duo -- 折叠屏「悬浮玻璃」动画 (摄像头测角版)

    摄像头 ORB 测角  +  windowsduo 的动画效果  =  这一个程序

- 角度: 默认用笔记本上盖摄像头 (ORB 特征匹配) 量上盖开合角, 不依赖额外硬件;
        --source serial 可切回 ESP32 + MPU6050 串口; --source manual 纯键盘。
- 渲染: windowsduo 的 GLSL 逆投影「悬浮玻璃」-- 铰链=屏幕底边, 间隙越大越模糊越暗。
- 界面: 默认**常驻系统托盘**, 玻璃层默认关着; 在托盘里开启, 设置窗口可配
        开机自启 / 摄像头 / 显示器 / 效果参数。

用法:
    python main.py                     托盘模式 (默认, 推荐)
    python main.py --glass             托盘模式 + 立刻开启玻璃层
    python main.py --no-tray           不进托盘, 直接开玻璃层 (老行为)
    python main.py --level 0.5         钉住浓度 0.5 (调试/演示)
    python main.py --selftest          无窗口自检 (角度源 + 截屏)
    python main.py --smoke             4 秒全屏演示后自动截图退出
"""
import argparse
import ctypes
import json
import os
import shutil
import sys
import time
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════
# 限制 BLAS 线程数 —— **必须在 numpy 被 import 之前设**。
# ═══════════════════════════════════════════════════════════════════════
# 本机 32 逻辑核, numpy 带的 OpenBLAS 会按核数开线程池: 实测光 `import
# numpy` 就把进程线程数从 4 拉到 27, 私有内存 +740MB (保留的线程栈)。
# 而**这个项目根本不做大矩阵运算** —— numpy 只用于:
#   - 求旋转的 Kabsch/SVD (3x3 矩阵, `solve_rotation`)
#   - 几百个特征点的向量化运算 (a @ R.T 之类)
# 这些规模下单线程反而更快 (多线程的 fork/join 开销比算本身还大),
# 所以线程池纯属白占内存。
#
# 环境变量必须在这里设: OpenBLAS/OpenMP 只在**库被加载时**读一次, numpy
# 一旦 import 完就晚了。`setdefault` 是为了不覆盖用户/打包器显式设的值。
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")

# ═══════════════════════════════════════════════════════════════════════
# 消除 Qt 的 DPI 警告 —— **必须在创建 QApplication 之前设**。
# ═══════════════════════════════════════════════════════════════════════
# 现象: 控制台刷一行
#   qt.qpa.window: SetProcessDpiAwarenessContext() failed: 拒绝访问
# 根因: python.exe / 打包 exe 的**清单 (manifest) 已经声明了进程 DPI 感知**,
# 进程一启动就锁定, 运行时改不了。Qt 默认想把它升级到 Per-Monitor-Aware-V2,
# 调 SetProcessDpiAwarenessContext 被系统拒绝 (清单设的不能改), 于是警告。
# **这条警告无害** (进程仍是 DPI 感知的), 纯粹难看, 而且没法在 ctypes 层改
# (进程已锁定)。唯一办法是让 Qt **别去尝试改**: 给 windows 平台插件传
# dpiawareness=2, Qt 就不再调那个会失败的 API (实测 0/1/3 仍警告, 只有 2 干净)。
# 用 setdefault: 离屏测试等显式设了 QT_QPA_PLATFORM 的场合不覆盖。
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:dpiawareness=2")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths   # noqa: E402  (必须在 sys.path 设置之后)

# 输出被重定向(管道)时 Windows 按 cp936 编码, 中文会乱码; 强制 UTF-8。
# 挂在真控制台上时 Python 本就写 UTF-8, 这一步无副作用。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

class _TeeLogger:
    def __init__(self, primary, log_path):
        self.primary = primary
        self.log_file = None
        try:
            self.log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        except Exception:  # noqa: BLE001
            pass

    def write(self, s):
        if self.primary is not None:
            try:
                self.primary.write(s)
            except Exception:  # noqa: BLE001
                pass
        if self.log_file is not None:
            try:
                self.log_file.write(s)
            except Exception:  # noqa: BLE001
                pass

    def flush(self):
        if self.primary is not None:
            try:
                self.primary.flush()
            except Exception:  # noqa: BLE001
                pass
        if self.log_file is not None:
            try:
                self.log_file.flush()
            except Exception:  # noqa: BLE001
                pass


_log_file_path = str(_paths.log_file("win_duo.log"))
sys.stdout = _TeeLogger(sys.stdout, _log_file_path)
sys.stderr = _TeeLogger(sys.stderr, _log_file_path)

#: config.json 等**用户数据**跟在 exe (或项目根) 旁边。
#: 打包后 `__file__` 指向临时解包目录, 直接用它会把配置写到一个马上被删掉的
#: 地方 —— 所以统一走 paths 模块 (见那里的说明)。
BASE_DIR = _paths.data_dir()
#: 配置统一放 <数据目录>/diagnostics/config/config.json (见 paths.config_file)
DEFAULT_CONFIG = _paths.config_file("config.json")


def config_path(explicit=None):
    return Path(explicit) if explicit else DEFAULT_CONFIG


#: **内置的出厂默认配置。**
#:
#: 唯一用途: `config.json` 不见了 (被删 / 第一次运行) 时**重新生成一份**。
#: 它**不会**覆盖已存在文件里的任何值 —— 那永远是用户的 (见 AGENTS 第 6 条:
#: "config.json 是唯一参数入口, 不要在代码里另加硬编码默认值去覆盖它")。
#:
#: **为什么不能只靠"从模板文件复制"**: 源码运行时"模板"和"活配置"是**同一个
#: 文件** (`<项目根>/config.json`)。用户把 config.json 删了, 模板也就没了,
#: 复制这一步无事可做 —— 接着 `load_config` 打开一个不存在的文件直接崩
#: (`FileNotFoundError` + 闪一下命令行窗口)。打包成 exe 时模板在解包目录、
#: 活配置在 exe 旁边, 是两个文件, 所以**这个 bug 只在源码模式下暴露**。
#: 内置一份, 两条路径就都稳了。
#: 只放**用户会调 / 换机器会变 / 必须持久化**的键。算法与滤波微调那一批
#: (camera_nfeatures / deadzone / fps / One-Euro / opencv_threads / 串口角度
#: 标定 / smoothing / darkening / max_taps / backdrop_blur / lock_at_close /
#: idle_* 阈值 等) 已改成源码常量, 不再进 config.json —— 分别定义在
#: angles/camera.py (CAMERA_*)、angles/serial_source.py (SERIAL_*)、
#: render/overlay.py (SMOOTHING / DARKENING / MAX_TAPS / IDLE_* 等)。
DEFAULT_CFG = {
    "source": "camera",
    "hotkey_toggle": "ctrl+alt+d",
    "hotkey_off": "ctrl+alt+shift+esc",
    "hotkey_level_up": "ctrl+alt+up",
    "hotkey_level_down": "ctrl+alt+down",
    "hotkey_level_full": "ctrl+alt+right",
    "hotkey_level_zero": "ctrl+alt+left",
    "hotkey_debug": "ctrl+alt+g",
    "screen_name": "",
    "screen_index": 0,
    "camera_index": 0,
    "camera_backend": "auto",
    "camera_scale": 1.1,
    "camera_sign": -1,
    "port": "COM3",
    "baud": 115200,
    "refresh_hz": 165.0,
    "render_fps": 30,
    "capture_backend": "auto",
    "max_tilt_deg": 88.0,
    "eye_dist_h": 2.0,
    "blur_spread": 0.42,
    "outside_mode": "black",
    "backdrop_path": "desk_bg.png",
    "autostart_glass": True,
    "autostart_seeded": False,
    "autocal_on_glass_open": True,
    "low_memory_mode": False,
}


def _seed_config_if_missing(cfg_path):
    """配置文件不存在时**生成一份**, 保证程序一定能启动。

    两条来源, 按顺序试:
      1. 打包进来的模板 (`--add-data` 的 config.json) —— **exe 模式**下它在解包
         目录, 和活配置不是同一个文件, 直接复制即可, 能保留打包时的定制值;
      2. **内置默认值** (`DEFAULT_CFG`) —— 兜底。源码模式下模板和活配置是同一个
         文件, 删掉 config.json 后第 1 条无事可做, 只能靠这条。

    `cfg_path != DEFAULT_CONFIG` 时不动 —— 用户用 `--config` 显式指了别的文件,
    不该替他凭空造一个出来。
    """
    if cfg_path.exists() or cfg_path != DEFAULT_CONFIG:
        return
    src = _paths.resource_file("config.json")
    if src.exists() and src.resolve() != cfg_path.resolve():
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, cfg_path)
            print("[config] 首次运行, 已从模板生成 %s" % cfg_path)
            return
        except Exception as exc:  # noqa: BLE001
            print("[config] 从模板复制失败, 改用内置默认值: %s" % exc)
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        # 不写 BOM, 和 controller.save() 保持一致 (读的一端用 utf-8-sig, 两种都能读)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CFG, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("[config] 配置不存在, 已用内置默认值生成 %s" % cfg_path)
    except Exception as exc:  # noqa: BLE001
        print("[config] 生成默认配置失败: %s" % exc)


def _seed_autostart_once(controller):
    """首次运行时把开机自启**默认打开**, 之后尊重用户的选择。

    开机自启的真实状态存在**注册表 HKCU Run 键**里, 不在 config.json。所以
    "默认打开"不能靠给 config 一个 `true` 就完事 —— 那样每次启动都会强行
    把注册表写回去, 用户手动关掉了也会被重新打开, 很烦人。

    正确做法是**播种一次**: 用 config 里的 `autostart_seeded` 记住"已经替用户
    默认开过了"。
      - 第一次运行 (没有这个标志): 写注册表开启自启, 然后把标志置 True 存档;
      - 以后每次运行: 标志已在, 什么都不做 —— 用户在设置里怎么改就是怎么样。

    这样既满足"默认打开", 又不会覆盖用户后来的手动关闭。
    """
    cfg = controller.cfg
    if cfg.get("autostart_seeded"):
        return
    from ui import autostart
    if autostart.available() and not autostart.is_enabled():
        try:
            autostart.enable()
            print("[autostart] 首次运行, 已默认开启开机自启")
        except Exception as exc:  # noqa: BLE001
            print("[autostart] 默认开启失败: %s" % exc)
    cfg["autostart_seeded"] = True
    controller.save()


def load_config(path=None):
    cfg_path = config_path(path)
    _seed_config_if_missing(cfg_path)
    # utf-8-sig: 用记事本/PowerShell 5.1 编辑过的 json 可能带 BOM,
    # 用 utf-8 读会直接抛 "Unexpected UTF-8 BOM"
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        # _seed 已经尽力了还是没文件 (比如目录不可写)。给一份内存里的默认值
        # 让程序**能起来**, 总好过闪一下就没了 —— 起来以后用户能在设置里改,
        # 也能看到日志里的原因。
        print("[config] 读不到 %s, 本次用内置默认值运行" % cfg_path)
        return dict(DEFAULT_CFG)
    except (json.JSONDecodeError, OSError) as exc:
        # 文件被写坏了 (空文件 / 手工改错 / 写一半断电)。**不静默吞掉** ——
        # 把坏文件留个备份再重建, 用户还能找回自己改过的内容。
        print("[config] %s 解析失败 (%s), 备份后重建" % (cfg_path, exc))
        try:
            bad = cfg_path.with_suffix(".json.bad")
            shutil.copyfile(cfg_path, bad)
            print("[config] 损坏的原文件已备份为 %s" % bad)
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(DEFAULT_CFG, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except Exception:  # noqa: BLE001
            pass
        return dict(DEFAULT_CFG)
    # 配置缺键时用内置默认值补齐 (老版本的 config 缺少新加的键很常见)
    for k, v in DEFAULT_CFG.items():
        cfg.setdefault(k, v)
    return cfg


def apply_args(cfg, args):
    if args.source:
        cfg["source"] = args.source
    if args.camera is not None:
        cfg["camera_index"] = args.camera
    if args.camera_backend:
        cfg["camera_backend"] = args.camera_backend
    if args.port:
        cfg["port"] = args.port
    if args.outside:
        cfg["outside_mode"] = args.outside
    if args.refresh_hz is not None:
        cfg["refresh_hz"] = args.refresh_hz
    if args.scale is not None:
        cfg["camera_scale"] = args.scale
    if args.screen is not None:
        cfg["screen_index"] = args.screen
    if args.low_memory:
        cfg["low_memory_mode"] = True
    return cfg


def make_qsurface_format():
    from PyQt6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    # **Core profile**: 着色器已全部改成 3.3 core 写法 (in/out + 自声明 out +
    # gl_VertexID 全屏三角形, 见 render/shader.py 与 overlay._draw_quad)。
    # 之前用 Compatibility, 在核显 (Intel/AMD) 上拿到的前向兼容上下文会把
    # varying/gl_FragColor/即时模式剥掉, 编译失败 -> 玻璃层白/黑屏。core 后
    # 任何合规驱动都一致。
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    return fmt


def print_banner(cfg, region):
    print("=" * 68)
    print("win-duo -- 折叠屏悬浮玻璃 (摄像头测角 + Duo 逆投影着色器)")
    print("-" * 68)
    print("  角度源    : %s" % cfg["source"])
    if cfg["source"] == "camera":
        print("  摄像头    : index=%s  后端=%s  SCALE=%.1f  SIGN=%+d"
              % (cfg.get("camera_index"), cfg.get("camera_backend"),
                 cfg["camera_scale"], cfg["camera_sign"]))
    elif cfg["source"] == "serial":
        print("  串口      : %s @ %d" % (cfg["port"], cfg["baud"]))
    print("  渲染      : 铰链=屏幕底边  最大转角 %.0f 度  眼距 %.1fx 屏高"
          % (cfg["max_tilt_deg"], cfg["eye_dist_h"]))
    print("              blur_spread=%.2f  出界=%s"
          % (cfg["blur_spread"], cfg["outside_mode"]))
    print("  截屏      : %dx%d @ %.0fHz"
          % (region["width"], region["height"], cfg.get("refresh_hz", 3)))
    print("=" * 68)


# ------------------------------------------------------------------ 单实例
_MUTEX_NAME = "win-duo-single-instance"
#: 句柄要一直留着 —— 关掉就等于把互斥体还回去了
_MUTEX_HANDLE = []


def already_running():
    """同一时间只允许一个 win-duo 实例。

    两个实例会互相打架, 而且现象很难自查:
      - 后启动的注册不了全局热键 (`RegisterHotKey` 撞车) —— 表现是"热键没反应";
      - 两层**全屏置顶**的玻璃层叠在一起, 你看到的那层可能是旧实例的 ——
        它连的是旧代码、旧帧, 表现就是"画面静止不动, 怎么改都没用"。
    这是个会浪费很多时间的坑, 所以直接拦住。

    `CreateMutexW` 返回的句柄在进程退出时由系统释放, 崩溃也一样, 不会残留。
    """
    try:
        # 必须 use_last_error=True: 直接 windll.kernel32.GetLastError() 不可靠,
        # ctypes 在两次调用之间可能已经动过 last-error, 结果永远拿到 0。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        _MUTEX_HANDLE.append(k32.CreateMutexW(None, False, _MUTEX_NAME))
        return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ 自检
def selftest(cfg):
    from PyQt6.QtWidgets import QApplication

    from angles.hub import SourceHub
    from angles.manual import KeyControl
    from render.capture import make_capture
    from ui import apply_theme, monitors

    app = QApplication([sys.argv[0]])
    apply_theme()          # 应用级图标 (任务栏/Alt-Tab/通知)
    screen = monitors.resolve(cfg) or app.primaryScreen()
    region = monitors.region_for(screen)
    print_banner(cfg, region)

    print("[自检] 启动角度源与截屏...")
    control = KeyControl(cfg, auto=False)
    hub = SourceHub(cfg, control)

    cap = make_capture(region, cfg, display_hz=screen.refreshRate())
    cap.start()
    cap.kick()

    ok = True
    # 等角度源给出 level。**时限要给足**: 摄像头是异步打开的, 而 `auto` 要逐个
    # 后端试 + 做冻结帧检测, 本机实测最坏要 ~20 秒 (DSHOW 那个虚拟摄像头打开
    # 失败时会卡很久)。12 秒曾经不够, 自检会误报 FAIL。
    deadline = time.time() + 40.0
    last = None
    while time.time() < deadline:
        last = hub.resolve()
        if last[0] is not None:
            break
        time.sleep(0.25)

    cap.done.wait(timeout=3)
    frame = cap.latest()

    lvl, name, status, detail = last if last else (None, "?", "?", {})
    print("-" * 68)
    if frame:
        print("[自检] 截屏   : OK %dx%d" % (frame[1], frame[2]))
    else:
        print("[自检] 截屏   : FAIL")
        ok = False
    if lvl is not None:
        print("[自检] 角度源 : OK  %s  level=%.3f  %s  %s"
              % (name, lvl, status, detail))
    else:
        print("[自检] 角度源 : FAIL  %s  %s  %s" % (name, status, detail))
        print("       摄像头需要对着有纹理的静止场景, 且上盖完全展开以便标定;")
        print("       也可以先跑 --source manual 确认渲染链路是好的。")
        ok = False
    print("-" * 68)
    print("[自检] %s" % ("PASS" if ok else "FAIL"))

    hub.stop_all()
    cap.stop()
    return 0 if ok else 1


# ------------------------------------------------------------------ 直接启动
def run_direct(cfg, smoke=False, seconds=0.0, level=None):
    """不经过托盘, 直接开玻璃层。给 --smoke / --level / --no-tray 用。"""
    from PyQt6.QtWidgets import QApplication

    from angles.hub import SourceHub
    from angles.manual import KeyControl
    from render.capture import make_capture
    from render.overlay import GlassOverlay
    from ui import apply_theme, monitors

    make_qsurface_format()
    app = QApplication([sys.argv[0]])
    apply_theme()          # 应用级图标 (任务栏/Alt-Tab/通知)
    screen = monitors.resolve(cfg) or app.primaryScreen()
    region = monitors.region_for(screen)

    control = KeyControl(cfg, auto=(cfg["source"] != "manual"))
    control.start()
    hub = SourceHub(cfg, control)

    cap = make_capture(region, cfg, display_hz=screen.refreshRate())
    cap.start()

    widget = GlassOverlay(screen, hub, cap, control, cfg)
    print_banner(cfg, region)

    # 设备打不开只提示, **不自动切换角度源** —— 模式由用户显式选择。
    # 注意角度源是异步打开的 (打开设备要 ~1 秒), 这里立即查还看不出结果,
    # 所以延后一点再判断。
    def _check_source():
        if cfg["source"] == "manual":
            return
        if hub.device_available(cfg["source"]) is False:
            label = "摄像头" if cfg["source"] == "camera" else "串口"
            print("!" * 68)
            print("[!] %s打不开。角度源仍是「%s」, 未自动切换。" % (label, label))
            print("[!] 在设置窗口点「扫描」探测可用摄像头, 或换成别的角度源。")
            print("!" * 68)

    from PyQt6.QtCore import QTimer as _QTimer
    _QTimer.singleShot(3000, _check_source)

    if level is not None:
        control.set_auto(False)
        control.set_level(float(level))
        widget.g = float(level)
        print("[运行] 浓度已被 --level 钉在 %.2f" % float(level))

    if smoke:
        from PyQt6.QtCore import QTimer

        def dump_and_quit():
            try:
                img = widget.grabFramebuffer()
                out = str(_paths.debug_file("smoke_widget.png"))
                img.save(out)
                print("\n[smoke] grabFramebuffer -> %s (%dx%d)"
                      % (out, img.width(), img.height()))
            except Exception as exc:  # noqa: BLE001
                print("\n[smoke] grabFramebuffer 失败:", exc)
            app.quit()

        # 关掉后台重截: 截图里会包含 Overlay 自身, 反复重截会反馈污染成纯色
        widget.refresh_hz = 0.0
        # 浓度必须按在角度源上, 不能只设 widget.g: tick() 每帧都会用
        # hub.resolve() 的结果覆盖 target, 只设 g 的话会被手动源的 0.0 拉回去,
        # 2 秒后截到的就是一张摊平的普通桌面, 等于什么都没验证到。
        control.set_auto(False)
        control.set_level(0.85)
        widget.g = 0.85
        QTimer.singleShot(2000, dump_and_quit)

    widget.show()
    cap.kick()

    if seconds and seconds > 0:
        from PyQt6.QtCore import QTimer as _QT
        _QT.singleShot(int(seconds * 1000), app.quit)

    print("[运行] Overlay 常驻显示。Esc 或 Ctrl+C 退出。")
    t_cpu0, t_wall0 = time.process_time(), time.perf_counter()
    try:
        rc = app.exec()
    finally:
        cpu = time.process_time() - t_cpu0
        wall = time.perf_counter() - t_wall0
        if wall > 0.5:
            print("\n[stats] 进程 CPU %.2fs / 墙钟 %.2fs = 单核 %.1f%%"
                  % (cpu, wall, cpu / wall * 100.0))
        hub.stop_all()
        cap.stop()
    return rc


# ------------------------------------------------------------------ 托盘模式
def run_tray(cfg, cfg_path, start_glass=False, seconds=0.0, panel_at_start=False):
    """默认模式: 只驻留托盘。玻璃层由用户在托盘里开(除非配了自动启动)。

    参数名**不能叫 `open_panel`** —— 里面那个"打开设置窗口"的嵌套函数也叫这个
    名字, 会把参数遮蔽掉, 于是 `if open_panel:` 永远为真, **每次启动都无条件
    弹出设置窗口**。踩过一次。
    """
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

    from ui import AppController, DuoTray, SettingsPanel, apply_theme, monitors

    make_qsurface_format()
    app = QApplication([sys.argv[0]])
    # 关掉设置窗口不应该退出程序 —— 它还要在托盘里活着
    app.setQuitOnLastWindowClosed(False)
    apply_theme()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("[!] 系统托盘不可用, 退回直接启动模式")
        return run_direct(cfg)

    controller = AppController(cfg, cfg_path)
    # 首次运行默认开启开机自启 (只播种一次, 之后尊重用户选择)
    _seed_autostart_once(controller)

    # ── 设置窗口**惰性创建** ────────────────────────────────────────────
    # 实测 SettingsPanel 光"建出来"就占 ~69MB (一堆 qfluent 控件 + 布局 +
    # 两个下拉框要枚举显示器/摄像头)。而托盘用户绝大多数时间根本不打开它 ——
    # 启动就建等于白付这 69MB。
    # 改成第一次真的要看的时候才建 (`_panel` 是 None 就建), 之后复用。
    # 用 dict 装是为了让闭包能改它 (Python 闭包不能给外层变量赋值)。
    _state = {"panel": None}

    def get_panel():
        if _state["panel"] is None:
            _state["panel"] = SettingsPanel(controller)
        return _state["panel"]

    def show_settings():
        p = get_panel()
        p.show()
        p.raise_()
        p.activateWindow()
        print("[ui] 设置窗口已打开")

    tray = DuoTray(controller, show_settings)
    tray.show()

    # 全局热键由 controller 管 —— 键盘模式那几个键要随角度源切换动态注册/注销。
    # 玻璃层是全屏置顶且穿透输入的, 必须留一条**不依赖托盘、也不依赖控制台**
    # 的后路, 否则屏幕被盖住时就真"回不去了"。
    controller.register_hotkeys()

    screen = monitors.resolve(cfg) or app.primaryScreen()
    print_banner(cfg, monitors.region_for(screen))
    print("  模式      : 托盘常驻 (启动不弹窗, 只在托盘留一个图标)")
    print("  用法      : 双击/右键托盘图标 -> 设置 / 开关玻璃层")
    print("              (设置窗口点最小化收进托盘, 点 X 直接退出程序)")
    print("  脱困      : Ctrl+Alt+Shift+Esc 立刻关掉玻璃层并退出")
    print("              (屏幕被玻璃层盖住、托盘也点不到时的最后手段)")
    print("=" * 68)

    if start_glass:
        controller.start_glass()
    else:
        # 玻璃层关着的时候也先把 GL 上下文/着色器建好 —— 第一次要 ~680ms,
        # 放在这里做, 用户点"开启"时就不会卡 (实测第 2 次只要 3ms)。
        from PyQt6.QtCore import QTimer as _PT
        _PT.singleShot(1200, controller.prewarm_overlay)

    if panel_at_start:
        show_settings()

    if seconds and seconds > 0:
        from PyQt6.QtCore import QTimer as _QT
        _QT.singleShot(int(seconds * 1000), app.quit)

    try:
        rc = app.exec()
    finally:
        # 顺序要紧: 先收掉带线程的界面 (扫描线程没结束的话 Qt 会 abort),
        # 再停角度源和截屏线程。热键由 controller.shutdown() 一并释放。
        # **面板可能是惰性创建的, 没建过就没什么可收** —— 那些 wait_orphans
        # 之类只对"真的建过面板"的情况有意义。
        panel = _state["panel"]
        if panel is not None:
            panel.shutdown()
        controller.shutdown()
        # 最后再给"还在打开设备"的扫描线程一点时间 —— 否则解释器关闭阶段会踩到
        # 已释放的 native 对象, 进程以 0xC0000409 退出 (功能其实全对)。
        if panel is not None:
            panel.wait_orphans()
    return rc


# ------------------------------------------------------------------ 入口
def _check_interpreter():
    """用系统 Python 直接跑 main.py 会找不到 PyQt6 —— 给一句明确的提示。

    依赖装在项目内的 .venv 里, 系统 Python 上没有。
    """
    try:
        import PyQt6  # noqa: F401
        return True
    except ImportError:
        pass
    venv_py = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    print("=" * 68)
    print("[!] 当前这个 Python 里没有 PyQt6 —— 多半是没走项目自带的虚拟环境。")
    print("    你现在用的是 : %s" % sys.executable)
    if venv_py.exists():
        print("    请改用       : %s" % venv_py)
        print("    或直接运行   : .venv\\Scripts\\python.exe main.py")
    else:
        print("    还没建环境, 先跑:")
        print("      powershell -ExecutionPolicy Bypass -File scripts\\setup_env.ps1")
    print("=" * 68)
    return False


def main():
    if not _check_interpreter():
        return 1
    ap = argparse.ArgumentParser(
        description="win-duo: 摄像头测角 + Duo 悬浮玻璃折叠动画")
    ap.add_argument("--source", choices=["camera", "serial", "manual"],
                    help="角度源 (默认取 config.json 的 source)")
    ap.add_argument("--camera", type=int, help="摄像头 index")
    ap.add_argument("--camera-backend", dest="camera_backend",
                    choices=["auto", "dshow", "msmf", "any"],
                    help="摄像头取流后端 (默认 auto: 会跳过冻结帧)")
    ap.add_argument("--port", help="ESP32 串口, 如 COM3")
    ap.add_argument("--screen", type=int, help="用第几块显示器 (0 起)")
    ap.add_argument("--outside", choices=["black", "backdrop"],
                    help="视线出界处理: black=原版纯黑, backdrop=背景兜底")
    ap.add_argument("--refresh-hz", type=float, dest="refresh_hz",
                    help="桌面重截频率")
    ap.add_argument("--scale", type=float, help="camera_scale 灵敏度")
    ap.add_argument("--config", help="指定 config.json")
    ap.add_argument("--tray", action="store_true",
                    help="托盘模式 (默认行为, 显式写出用)")
    ap.add_argument("--no-tray", dest="no_tray", action="store_true",
                    help="不进托盘, 直接开启玻璃层")
    ap.add_argument("--glass", action="store_true",
                    help="托盘模式下立刻开启玻璃层")
    ap.add_argument("--panel", action="store_true",
                    help="托盘模式下立刻打开设置窗口 (GUI)")
    ap.add_argument("--low-memory", dest="low_memory", action="store_true",
                    help="低内存模式: 玻璃层闲置 30 秒后释放 GL 窗口 (~130MB)")
    ap.add_argument("--selftest", action="store_true", help="无窗口自检")
    ap.add_argument("--smoke", action="store_true", help="4 秒演示后自动退出")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="跑够这么多秒自动退出 (脚本化验证用)")
    ap.add_argument("--level", type=float, default=None,
                    help="直接指定玻璃浓度 0..1 (手动模式, 不用按键)")
    args = ap.parse_args()

    cfg_file = config_path(args.config)
    cfg = apply_args(load_config(cfg_file), args)

    # 单实例: 两个实例会叠两层全屏置顶的玻璃层, 你看到的那层可能是旧实例的
    # (旧代码/旧帧), 表现就是"画面静止, 怎么改都没用"。这个坑很难自查。
    # `--selftest` 不涉及窗口, 放行。
    if not args.selftest and already_running():
        print("!" * 68)
        print("[!] 已经有一个 win-duo 在运行了。")
        print("[!] 两个实例会叠两层玻璃层、还抢全局热键 —— 请先在托盘右键退出")
        print("[!] 那一个 (或任务管理器里结束 pythonw.exe), 再启动这个。")
        print("!" * 68)
        return 2

    if args.selftest:
        return selftest(cfg)

    # 这些参数都要求"立刻看到画面", 所以走直启, 不经过托盘
    if args.smoke or args.level is not None or args.no_tray:
        return run_direct(cfg, smoke=args.smoke, seconds=args.seconds,
                          level=args.level)

    start_glass = args.glass or bool(cfg.get("autostart_glass", True))
    return run_tray(cfg, cfg_file, start_glass=start_glass, seconds=args.seconds,
                    panel_at_start=args.panel)


if __name__ == "__main__":
    sys.exit(main())
