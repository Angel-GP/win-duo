"""桌面截屏。

**优先走 WGC (Windows.Graphics.Capture), 退回 DXGI Desktop Duplication, 再退 GDI BitBlt (mss)。**

为什么 WGC 优先: 玻璃层靠 SetWindowDisplayAffinity 把自己排除出捕获, 否则就是
"截到已渲染的上一帧 -> 再叠一层效果" 的正反馈回路。DDA (Desktop Duplication) 的
排除是否生效取决于显示驱动 —— 实测 Intel Arc 曾出现"API 收下、合成器不执行"。
WGC 的排除发生在 Windows.Graphics.Capture 的合成节点里, **不走 OEM 显示驱动**,
驱动无法绕过 (scripts/wgc_affinity_probe.py 在本机 Intel Arc 上实测: affinity
测试窗在 DDA/WGC 下命中均 0%, 基线 100%)。

为什么换掉 mss: mss 走 GDI BitBlt, 每次都把整屏重拷一遍。本机 2560x1600 实测:

    后端          单帧耗时    实际帧率上限
    mss          27~29 ms     ~37 Hz
    bettercam    1.1 ms       ~156 Hz   (DXGI, output_color="BGRA")
    wgc          ~0.5 ms      合成器节拍 (同 DDA)

真正能拿到的帧率上限是**显示器刷新率** (本机 165Hz) —— 合成器每秒最多产
那么多帧。

后端返回 None 只表示"这一瞬没有新帧", 按目标频率继续跑即可。

**别再做"内容有没有变"的过滤。** 我加过一个全图平均差的静止检测
(`_signature_changed`, 阈值 1.5), 结果把整个画面冻死了: 日常操作
(打字、滚动、小窗口刷新) 只影响几万像素, 摊到 400 万像素上平均差只有
**0.2~0.3**, 永远够不到 1.5 —— 于是帧号永不推进, 玻璃层停在第一帧。

注意 DPI: DXGI 报的是**物理**像素, 但只在进程 DPI-aware 时才准, 否则
Windows 会按缩放比例虚拟化 (本机 2560x1600 会变成 1707x1067)。运行
`ensure_dpi_aware()` 兜底。
"""
import ctypes
import json
import sys
import threading
import time

import mss
import numpy as np

import paths


def ensure_dpi_aware():
    """让 DXGI 报出物理分辨率。

    Qt 一般已经设过; mss 在初始化时也会设。这里再兜一次底, 因为进程一旦先
    建了 DXGI 设备、后设 DPI 感知, 拿到的尺寸就是错的, 分辨率匹配会失败。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _co_initialize():
    """在当前线程初始化 COM (STA)。返回 True 表示"该由我们负责反初始化"。

    **为什么必须显式做**: `bettercam` 通过 comtypes 调 DXGI 的 COM 接口, 而
    comtypes **不会**自己 `CoInitialize` —— 翻过 bettercam 源码, CoInitialize
    出现 **0 次**。而 COM 是**按线程**初始化的:

      - 主线程能用, 是因为 Qt 启动时已经替它初始化过了;
      - 采集线程是我们自己建的, 没人给它初始化 —— 在里面调 DXGI 会直接
        **native 崩溃** (0xC0000409), Python 侧连异常都抓不到, 只看到
        "Unhandled Python exception"。

    实测症状: 玻璃层一显示、采集线程起来抓第一帧时进程就没了; 而在**主线程**
    里调几乎一样的代码却好好的 —— 差别就是线程。查了好几轮才定位到。

    参数用 `COINIT_APARTMENTTHREADED` (STA): DXGI 的桌面复制接口要求 STA。
    """
    if sys.platform != "win32":
        return False
    try:
        # 0x2 = COINIT_APARTMENTTHREADED, 0x4 = COINIT_DISABLE_OLE1DDE
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4)
    except Exception:  # noqa: BLE001
        return False
    if hr in (0, 1):            # S_OK / S_FALSE (本线程已初始化过)
        return True
    # RPC_E_CHANGED_MODE (0x80010106 -> 有符号 -2147417850): 线程已经是别的
    # 模式了, 不是错误, 只是不该由我们反初始化。
    return False


def _co_uninitialize():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def frame_bgr(frame):
    """把帧转成 cv2 用的 BGR ndarray。DXGI 给 BGRA, mss 给 BGRA。

    ⚠️ **必须返回"连续且可写"的数组。** 这里踩过一个会**崩进程**的坑:

    原来的写法是 `np.frombuffer(data, ...).reshape(h, w, 4)[:, :, :3]`。
    - `np.frombuffer` 对 **ndarray** 也能成功 (走缓冲协议), 但它返回的是
      **只读视图**, 底层并不拥有数据;
    - 最后那个 `[:, :, :3]` 是**跨步切片, 内存不连续** (每行跳过第 4 个通道)。

    把这个非连续只读视图交给 OpenCV (`cv2.resize` / `cv2.GaussianBlur`) 时,
    OpenCV 在 native 层按"连续三通道"去读 —— **直接崩**, Python 侧一个异常
    都看不到 (`Unhandled Python exception`, 退出码 0xC0000409)。

    触发路径: 玻璃层第一次上传纹理后 `_build_backdrop` -> `_fallback_backdrop`
    -> 这里 -> `cv2.resize`。所以表现为"玻璃层一显示就整个崩掉"。

    现在统一走 `np.ascontiguousarray(...)` 把切片拷成连续数组 —— 顺便也拿到
    了可写性。代价是一次 12MB 的拷贝 (只在建背景图时发生一次, 不在每帧路径上)。
    """
    data, w, h, _seq, fmt = frame
    if fmt == "RGBA":
        arr = np.asarray(data)
        return np.ascontiguousarray(arr[:, :, 2::-1])      # RGB -> BGR
    if isinstance(data, (bytes, bytearray, memoryview)):
        arr = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
    else:
        arr = np.asarray(data).reshape(h, w, 4)
    # 切片是跨步的, 交给 OpenCV 之前必须拷成连续内存 (见上面说明)
    return np.ascontiguousarray(arr[:, :, :3])


# ═══════════════════════════════════════════════════════════════════
# 后端记忆: 与 angles/camera.py 的"记住上次成功的后端"同思路。
# DDA 在部分驱动 (Intel Arc) 上会无视 SetWindowDisplayAffinity -> 反馈回路,
# 所以顺序是 wgc -> dda -> mss。哪台机器上次哪个后端真的在用, 下次优先试它。
# ═══════════════════════════════════════════════════════════════════


def _enum_monitors():
    """EnumDisplayMonitors 枚举所有显示器: [(origin, (w, h)), ...]。

    顺序就是 Windows API 的枚举顺序, 与 windows-capture 的 monitor_index
    (1-based) 一一对应: monitors[i] 的 index 是 i+1。实测单屏环境:
    index=1 -> 主屏, index>=2 报 "Failed to find the specified monitor"。

    用物理坐标 (进程 DPI-aware 时), 与 region_for() 换算出的 origin 同一坐标系。
    """
    if sys.platform != "win32":
        return []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
                    ("szDevice", ctypes.c_wchar * 32)]

    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.POINTER(RECT), ctypes.c_double)
    def _cb(_hmon, _hdc, _rect, _data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(_hmon, ctypes.byref(mi)):
            rc = mi.rcMonitor
            out.append(((int(rc.left), int(rc.top)),
                        (int(rc.right - rc.left), int(rc.bottom - rc.top))))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    return out


def _monitor_index_for(origin, size):
    """按目标屏的桌面 origin (物理像素) 找 windows-capture 的 monitor_index (1-based)。

    匹配规则与 _DxgiSource 的选屏一致: 先精确 origin, 再"最近 origin",
    最后拿分辨率兜底。找不到返回 None (调用方退回主屏)。
    """
    if origin is None:
        return None
    monitors = _enum_monitors()
    if not monitors:
        return None
    for i, (org, _wh) in enumerate(monitors):
        if tuple(org) == tuple(origin):
            return i + 1
    best_i, best_d = None, None
    for i, (org, _wh) in enumerate(monitors):
        d = abs(org[0] - origin[0]) + abs(org[1] - origin[1])
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    if best_i is not None and best_d <= 64:
        return best_i + 1
    if size:
        for i, (_org, wh) in enumerate(monitors):
            if tuple(wh) == tuple(size):
                return i + 1
    return None


_STATE_PATH = None
#: "上次成功的采集后端"的**内存缓存**。
#: `remember_capture_backend()` 在每次成功抓帧时都被调用 (WGC/DXGI 两条路径),
#: 玻璃层显示时最高 ~140 帧/秒 —— 若每次都去 `read_text` + `json.loads` 判"值
#: 变没变", 就是采集线程里每秒 140 次同步磁盘读, 纯白烧 I/O。这里缓存住,
#: 只有**值真的变化**时才写盘。`_STATE_LOADED` 区分"没读过"和"读过但没有值"。
_STATE_CACHE = None
_STATE_LOADED = False


def _backend_state_path():
    global _STATE_PATH
    if _STATE_PATH is None:
        try:
            _STATE_PATH = paths.config_file("capture_backend.json")
        except Exception:  # noqa: BLE001
            return None
    return _STATE_PATH


def load_good_capture_backend():
    """读"上次成功的后端"。**带内存缓存** —— 第一次读盘, 之后走缓存。"""
    global _STATE_CACHE, _STATE_LOADED
    if _STATE_LOADED:
        return _STATE_CACHE
    p = _backend_state_path()
    val = None
    if p is not None and p.exists():
        try:
            val = str(json.loads(p.read_text(encoding="utf-8")).get("backend")
                      or "") or None
        except Exception:  # noqa: BLE001  坏了就当没有
            val = None
    _STATE_CACHE = val
    _STATE_LOADED = True
    return val


def remember_capture_backend(name):
    """记住成功的后端。**只在值变化时写盘** (热路径零 I/O)。"""
    global _STATE_CACHE, _STATE_LOADED
    if _STATE_LOADED and _STATE_CACHE == name:
        return                          # 热路径: 命中缓存, 直接返回 (不碰磁盘)
    p = _backend_state_path()
    if p is None:
        return
    try:
        if load_good_capture_backend() == name:
            return
        p.write_text(json.dumps({"backend": name}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        _STATE_CACHE = name
        _STATE_LOADED = True
    except Exception:  # noqa: BLE001  静默, 别让记忆失败影响采集
        pass


# ═══════════════════════════════════════════════════════════════════════
# "哪些后端已知不可用" —— auto 模式用它跳过, 而不是把次优的记成首选
# ═══════════════════════════════════════════════════════════════════════
# 与上面的"记住成功的后端"是**两回事**:
#   - remember_capture_backend: "上次成功的" (仅参考)
#   - 这里: "上次**失败**的" -> auto 时直接跳过, 免得每次启动都要等它超时。
# 语义上这是**黑名单**, 只记"打不开"的后端; 一旦它某次打开成功就立刻移除。
_BAD_CACHE = None


def _bad_state_path():
    try:
        return paths.config_file("capture_backend_bad.json")
    except Exception:  # noqa: BLE001
        return None


def _load_bad_backends():
    """已知打不开的后端名集合 (打不开才在里面)。坏了就当空集。"""
    global _BAD_CACHE
    if _BAD_CACHE is not None:
        return _BAD_CACHE
    val = set()
    p = _bad_state_path()
    if p is not None and p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, list):
                val = {str(x) for x in d}
        except Exception:  # noqa: BLE001
            val = set()
    _BAD_CACHE = val
    return val


def _write_bad(bad):
    p = _bad_state_path()
    if p is None:
        return
    try:
        p.write_text(json.dumps(sorted(bad), ensure_ascii=False) + "\n",
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _remember_bad_backend(name):
    bad = _load_bad_backends()
    if name not in bad:
        bad.add(name)
        _write_bad(bad)


def _clear_bad_backend(name):
    bad = _load_bad_backends()
    if name in bad:
        bad.discard(name)
        _write_bad(bad)


def available_backends():
    """给界面用的后端清单: (显示名, 内部值)。"""
    return (("自动 (系统推荐最优)", "auto"),
            ("WGC (Windows.Graphics.Capture)", "wgc"),
            ("DXGI (Desktop Duplication)", "dxgi"),
            ("mss (GDI BitBlt, 兜底)", "mss"))


class _WgcSource:
    """Windows.Graphics.Capture 封装 (windows-capture 包, 优先于 DDA)。

    为什么优先: affinity 排除走 WGC 合成节点, 不经 OEM 显示驱动, 驱动无法
    "收下不执行" (见模块 docstring)。缺包 / Win10 2004 以下时创建失败,
    上层自动退回 DDA。

    windows-capture 的 API 坑 (实测):
      - `start_free_threaded()` 前必须注册 on_closed 回调, 否则直接抛异常;
      - monitor_index 从 1 起 (EnumDisplayMonitors 顺序), 0 会报
        "must be greater than zero", 不传默认主屏;
      - 帧 frame_buffer 是 BGRA ndarray (OWNDATA, 独占内存), 直接可用。
    """

    COLOR = "BGRA"

    def __init__(self, size, origin=None):
        self.w, self.h = int(size[0]), int(size[1])
        self._warned_size = False
        from windows_capture import WindowsCapture   # noqa: PLC0415  探测导入
        # 多屏时按 origin 选屏 —— 只按分辨率选会永远命中第一块, "选了副屏
        # 还截主屏" (与 _DxgiSource 的 origin 消歧同一bug, 见它的注释)。
        idx = _monitor_index_for(origin, (self.w, self.h))
        if idx is not None:
            print("[capture] WGC 选屏: origin=%s -> monitor_index=%d"
                  % (origin, idx))
        self.cap = WindowsCapture(cursor_capture=False, draw_border=False,
                                  monitor_index=idx)
        self._latest = None
        self._ctrl = None

        @self.cap.event
        def on_frame_arrived(frame, _ctx):
            self._latest = frame

        @self.cap.event
        def on_closed():
            self._latest = None

        self._ctrl = self.cap.start_free_threaded()

    def grab(self):
        """返回 (h, w, 4) 的 BGRA ndarray; 没有新帧返回 None。"""
        frame = self._latest
        if frame is None:
            return None
        self._latest = None          # 取走即清: 与 DDA "AcquireNextFrame 语义" 对齐
        arr = frame.frame_buffer
        fh, fw = arr.shape[0], arr.shape[1]
        if fw != self.w or fh != self.h:
            # 不要硬失败: WGC 会话的帧尺寸跟着**进程 DPI 感知**走 (DPI 不感知时
            # 2560x1600@133% 会给 1920x1200 的虚拟化尺寸), 但我们 probe 实测
            # ensure_dpi_aware 后仍可能拿到虚拟化尺寸 —— 与其炸掉回退 DDA,
            # 不如按实际尺寸用: overlay 上传纹理时用的是 _uploaded_size, 天然适配。
            # 只警告一次, 防止每帧刷屏。
            if not self._warned_size:
                self._warned_size = True
                print("[capture] WGC 帧尺寸 %dx%d != 预期 %dx%d (DPI 虚拟化?), "
                      "按实际尺寸用" % (fw, fh, self.w, self.h))
            self.w, self.h = fw, fh
        return arr

    def release(self):
        if self._ctrl is not None:
            try:
                self._ctrl.stop()
            except Exception:  # noqa: BLE001
                pass
            self._ctrl = None
        self._latest = None


class _DxgiSource:
    """DXGI Desktop Duplication 封装 (bettercam 优先, 退化到 dxcam)。"""

    #: **必须用 BGRA。** DXGI 桌面复制的原生格式就是 BGRA; 请求 RGBA 会让
    #: bettercam 在 16MB 缓冲上多做一次逐像素换 R/B, 实测单次调用从 1.12ms
    #: 涨到 4.42ms —— 而 4ms 直接把采集线程忙住, 重截频率就上不去了。
    #: 顺带好处: 和 mss 的 BGRA 统一, frame_bgr() 只剩一条路径。
    COLOR = "BGRA"

    #: 连续错这么多次才认定 DXGI 真的坏了 (掉回 mss 的代价是每帧 30ms, 别轻易)
    MAX_ERRORS = 5

    def __init__(self, size, origin=None):
        self.mod = None
        self.cam = None
        self.name = ""
        self.output_idx = 0
        self.errors = 0
        #: 目标显示器的桌面左上角 (物理像素)。分辨率相同的两块屏只靠 size 分不开,
        #: 这时再用坐标消歧 (见 _open_matching)。取不到就退化成"只按分辨率"。
        self.origin = tuple(origin) if origin else None
        for name in ("bettercam", "dxcam"):
            try:
                mod = __import__(name)
            except Exception:  # noqa: BLE001
                continue
            if self._open_matching(mod, size):
                return
        raise RuntimeError("DXGI 不可用 (没装 bettercam/dxcam, 或没有分辨率匹配的显示器)")

    @staticmethod
    def _cam_origin(cam):
        """某个 bettercam/dxcam 相机对应 output 的桌面左上角 (物理像素)。

        bettercam 的 Output 里有 DXGI 的 DesktopCoordinates; dxcam 结构不同,
        取不到就返回 None (调用方会退回"只按分辨率")。
        """
        try:
            dc = cam._output.desc.DesktopCoordinates
            return (int(dc.left), int(dc.top))
        except Exception:  # noqa: BLE001
            return None

    def _open_matching(self, mod, size):
        """挑出**目标那块屏**的 output 并留着用。

        选择规则:
          1. 先按分辨率筛 —— 分辨率唯一时这一步就定了 (最常见);
          2. 分辨率相同的多块屏 (例如两台 1920x1080) 用桌面左上角坐标区分:
             在所有分辨率匹配里选 origin **最接近**目标的那块。
             用"最近"而不是"精确相等": Qt 的 geometry 可能是逻辑坐标, 而 DXGI
             报的是物理坐标, 缩放屏上两者不一定逐像素相等, 精确比对会错过 ——
             取最近既能在同分辨率双屏里分对屏, 又能容忍这点坐标偏差。
          3. 没给 size 时用第一块。

        不能"先遍历一遍探测、再 create 一次" —— bettercam 对同一个 output
        返回的是**单例**, 探测时 release() 掉的正是后面要用的那个对象。这里
        每个 output_idx 是不同实例, 所以边遍历边比较、只保留当前最优、把落选的
        当场 release 掉, 不会误伤最终要用的那个。
        """
        try:
            count = len([ln for ln in mod.output_info().strip().splitlines()
                         if ln.strip()])
        except Exception:  # noqa: BLE001
            count = 1
        want = tuple(size) if size else None

        best = None                        # (cam, idx, dist)
        for idx in range(max(1, count)):
            try:
                cam = mod.create(output_idx=idx, output_color=self.COLOR)
            except Exception:  # noqa: BLE001
                continue
            wh = (getattr(cam, "width", 0), getattr(cam, "height", 0))
            if want is not None and wh != want:
                self._release_cam(cam)
                continue
            # 分辨率匹配。算它到目标坐标的距离 (取不到坐标或没给目标就当 0)。
            dist = 0
            if self.origin is not None:
                org = self._cam_origin(cam)
                if org is not None:
                    dist = abs(org[0] - self.origin[0]) + abs(org[1] - self.origin[1])
            if best is None or dist < best[2]:
                if best is not None:
                    self._release_cam(best[0])
                best = (cam, idx, dist)
            else:
                self._release_cam(cam)
            if dist == 0:                  # 已经完美命中, 不用再看后面的
                break

        if best is None:
            return False
        cam, idx, _ = best
        self.mod, self.cam, self.name, self.output_idx = mod, cam, mod.__name__, idx
        return True

    @staticmethod
    def _release_cam(cam):
        try:
            cam.release()
        except Exception:  # noqa: BLE001
            pass

    def grab(self):
        """返回 (h, w, 4) 的 **BGRA** ndarray; 桌面没变时返回 None。

        (COLOR = "BGRA"; 全链路按 BGRA 处理 —— 别被旧注释"RGBA"误导。)

        DXGI 偶发 `DXGI_ERROR_INVALID_CALL` (输出切换、桌面锁定、上一帧没来得及
        释放等)。这种情况**不要立刻放弃 DXGI** —— 掉回 mss 就是每帧 30ms,
        代价太大。先容忍几次 (期间当作"没有新帧", 渲染层会继续用上一帧),
        连续失败才真的抛出去让上层退回 mss。
        """
        if self.cam is None:
            raise RuntimeError("DXGI camera 已释放")
        try:
            frame = self.cam.grab()
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            if self.errors >= self.MAX_ERRORS:
                raise
            print("[capture] DXGI 抓帧出错 (%d/%d), 先当作没有新帧: %s"
                  % (self.errors, self.MAX_ERRORS, exc))
            return None
        self.errors = 0
        return frame

    def release(self):
        if self.cam is not None:
            try:
                self.cam.release()
            except Exception:  # noqa: BLE001
                pass
            self.cam = None


class CaptureWorker(threading.Thread):
    def __init__(self, region, size=None, backend="auto", display_hz=60.0):
        super().__init__(daemon=True, name="capture")
        self.region = region
        self.size = tuple(size) if size else (region.get("width"), region.get("height"))
        #: 目标屏桌面左上角 (物理像素), 用来在**同分辨率双屏**里区分是哪一块 ——
        #: 只按分辨率选会永远命中第一块 (通常是主屏), 于是"选了副屏还截主屏"。
        self.origin = self._region_origin(region)
        self.want_backend = backend
        self.display_hz = float(display_hz or 60.0)

        self.request = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.frame = None          # (data, w, h, seq, "BGRA")
        #: 双缓冲 (_store 用): 两块预分配 BGRA 帧缓冲, 按帧号奇偶轮流写入,
        #: 消掉每帧 malloc/free 16MB 的堆碎片与分配开销 (见 _store 说明)。
        self._bufs = [None, None]
        self.busy = False
        # **不能叫 `_stop`** —— threading.Thread 内部有个 `_stop()` 方法,
        # 用同名属性会把它遮蔽掉, `join()` 一调就
        # `TypeError: 'bool' object is not callable`。踩过一次。
        self._halt = False
        self.last_ms = 0.0
        self.backend = "?"
        self._dxgi = None
        self._wgc = None
        self._sct = None
        #: set_region() 换屏后置 True; 采集线程在安全点重挑 DXGI output。
        self._reopen_pending = False
        # 实测计数: 用来回答"设了 137Hz 到底跑到了多少"
        self.kicks = 0
        self.pumps = 0
        self.grabs = 0
        #: 后端真正返回了数组的次数 (None = 那一瞬没有新帧)
        self.frames_in = 0
        self.rate_hz = 0.0
        self._rate_t = time.time()
        self._rate_n = 0
        #: >0 时采集线程**自己连续跑** (按这个频率), 不走 kick 往返。
        #: "主线程 kick -> 唤醒线程 -> 抓 -> 回信"一个来回要 ~7ms, 这条路
        #: 只能跑到 ~100 次/秒; 设 137 就永远差一截。连续模式绕开这个开销。
        #: 玻璃层关着/收起来时由渲染层置 0, 线程回到等待状态, 不白烧 CPU。
        self.target_hz = 0.0

    # ------------------------------------------------------------ 生命周期
    def open_now(self, timeout=6.0):
        """**同步**等后端打开好 (供 GUI 切换后端后用)。

        `_open()` 是采集线程里懒调的, 所以 start() 返回时 `self.backend` 还是
        "?"。切换后端时我们想立刻知道"成没成、用的是什么", 就得在这里等一下。
        返回实际打开的后端名 (超时/失败时抛异常)。
        """
        deadline = time.time() + max(0.5, float(timeout))
        while time.time() < deadline:
            if self.backend != "?":
                return self.backend
            self.kick()                      # 催一下, 让它尽快跑到 _open
            time.sleep(0.02)
        raise RuntimeError("等待采集后端打开超时 (backend 仍为 '?')")

    def _open(self):
        """幂等 —— start() 里会调一次, 外部也可能先调一次做探测。

        不幂等的话第二次会再走一遍探测 —— bettercam 是**单例**, 探测时的
        release() 会把手上正在用的 camera 销毁掉。

        ══════════════════════════════════════════════════════════════
        auto 与手动的语义 (刻意区分, 别混)
        ══════════════════════════════════════════════════════════════
        **auto = 系统按"最优"推荐**: 永远从**性能最好**的后端开始试
        (wgc ~0.5ms/帧 < dxgi ~1.1ms < mss ~27ms), 不可用才往下退。
        "记住上次成功的后端"在这里**只用于跳过已知不可用的** (比如上次 dxgi
        起不来), **不会**因为"上次回退到了 dxgi"就再也不试更优的 wgc ——
        否则一次偶发失败会被永久记住, 之后一直用次优后端。

        **手动选 = 完全听用户的**: 只用那一个, 起不来就**明确报错**
        (不静默降级 —— 否则用户以为在用 dxgi, 实际跑的是 mss)。
        """
        if self.backend != "?":
            return
        ensure_dpi_aware()

        # 手动指定: 只试这一个, 失败就硬失败 (含 mss)
        if self.want_backend != "auto":
            self._open_one(self.want_backend, strict=True)
            return

        # auto: 按性能从好到差试; 记住的后端只用来**跳过已知不可用的**
        bad = _load_bad_backends()
        order = [b for b in ("wgc", "dxgi") if b not in bad]
        if not order:
            # 两个 GPU 后端都被记成"不可用" -> 直接 mss
            order = []
        if bad:
            print("[capture] auto: 跳过上次失败的 %s" % ",".join(sorted(bad)))
        for name in order:
            if self._open_one(name, strict=False):
                return
        # 都不可用 -> mss (唯一的软件兜底, 一定能用)
        self.backend = "mss"
        self._sct = mss.mss()
        print("[capture] 后端 mss (GDI BitBlt), %dx%d" % self.size)

    def _open_one(self, name, strict):
        """尝试打开一个指定后端。成功返回 True 并设好 self.backend。

        `strict=True` (手动指定) 时失败会抛异常 —— 手动选择必须让用户知道
        失败了, 不能悄悄换一个。
        """
        if name == "mss":
            self.backend = "mss"
            self._sct = mss.mss()
            print("[capture] 后端 mss (GDI BitBlt), %dx%d" % self.size)
            return True
        if name == "wgc":
            try:
                self._wgc = _WgcSource(self.size, origin=self.origin)
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise RuntimeError("WGC 后端不可用: %s" % exc)
                print("[capture] WGC 不可用, 回退: %s" % exc)
                _remember_bad_backend("wgc")
                return False
            self.backend = "wgc"
            _clear_bad_backend("wgc")
            print("[capture] 后端 wgc (Windows.Graphics.Capture), %dx%d @ 桌面坐标 %s"
                  % (self.size + (self.origin,)))
            return True
        if name == "dxgi":
            try:
                self._dxgi = _DxgiSource(self.size, origin=self.origin)
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise RuntimeError("DXGI 后端不可用: %s" % exc)
                print("[capture] DXGI 不可用, 回退: %s" % exc)
                _remember_bad_backend("dxgi")
                return False
            self.backend = self._dxgi.name
            _clear_bad_backend("dxgi")
            print("[capture] 后端 %s (DXGI), output=%d, %dx%d @ 桌面坐标 %s"
                  % (self._dxgi.name, self._dxgi.output_idx, *self.size,
                     self.origin))
            return True
        raise RuntimeError("未知采集后端 %r" % name)

    def _drop_dxgi(self):
        if self._dxgi is not None:
            self._dxgi.release()
            self._dxgi = None
        if self._sct is None:
            self._sct = mss.mss()
        self.backend = "mss"
    def stop(self):
        """停掉采集线程。

        **必须先等线程退出再释放设备。** 连续模式下线程一直在 native 层跑
        `grab()`, 从主线程直接 `release()` 会让它踩到已释放的对象 ——
        实测直接 `access violation reading 0x...168`, 而且是 native 崩溃,
        Python 侧什么都抓不到。改成"先停后等, 再释放"。
        """
        self._halt = True
        self.target_hz = 0.0
        self.request.set()
        if self.is_alive():
            self.join(timeout=2.0)
        if self._dxgi is not None:
            self._dxgi.release()
        if self._wgc is not None:
            self._wgc.release()
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:  # noqa: BLE001
                pass
            self._sct = None

    # ------------------------------------------------------------ 能力查询
    def max_hz(self):
        """重截频率的**最高可用值** —— 就是显示器刷新率。

        合成器每秒最多产出那么多帧, 抓得再勤也拿不到更新的画面。所以这是一个
        **固定值** (本机 165Hz), 不随负载浮动。

        DXGI 抓一帧只要 0.1ms, 完全跟得上; 万一退回了 mss (没装 bettercam),
        实际只跑得到 ~37Hz —— 但上限仍然按刷新率给: 那是"可用"的目标, 填高了
        也不会出错 (采集线程忙的时候 `kick()` 会自己跳过)。
        """
        return max(60.0, self.display_hz)

    def latest(self):
        with self.lock:
            return self.frame

    def kick(self):
        if not self.busy:
            self.kicks += 1
            self.request.set()

    @staticmethod
    def _region_origin(region):
        """截屏区域左上角 (物理像素桌面坐标), 用于在同分辨率双屏里定位目标屏。"""
        if not region:
            return None
        left, top = region.get("left"), region.get("top")
        if left is None or top is None:
            return None
        return (int(left), int(top))

    def set_region(self, region):
        """切换目标显示器 (换屏时 controller 会调)。

        **不只是换 region。** DXGI 后端在 `_open` 时就**绑定了某一块 output**,
        只改 self.region 对它没有任何作用 —— 它照旧抓原来那块屏。这正是
        "在设置里选了副屏, 画面却还是主屏"的根因。所以换屏时必须让采集线程
        **重新挑一次 output**。

        重建绝不能在这里 (主线程) 直接做: 采集线程可能正卡在 native 层的
        `grab()` 里, 从主线程 release() 掉它手上的相机会踩到已释放对象 ——
        直接 native 崩溃 (见 stop() 的说明)。所以只在这里登记"待重开", 由采集
        线程在两帧之间的安全点自己重建。mss 后端没有绑定问题, 换 region 即可。
        """
        with self.lock:
            self.region = region
            self.size = (region.get("width"), region.get("height"))
            self.origin = self._region_origin(region)
            self.frame = None
            # 双缓冲不在这里重建: _buf_for 只在采集线程里跑, 尺寸不符时自己
            # 会按新尺寸重建 —— 这里碰它反而要考虑与采集线程的并发。
            self._reopen_pending = True
        self.kick()

    # ------------------------------------------------------------ 主循环
    def run(self):
        # **必须在采集线程里初始化 COM。**
        # bettercam 底层是 DXGI 的 COM 接口 (通过 comtypes 调), 而 comtypes
        # 自己**不会** CoInitialize —— 实测 bettercam 源码里 CoInitialize
        # 出现 0 次。
        # 主线程能用是因为 Qt 早就替它初始化过了; 采集线程是新线程, COM 是
        # **按线程**初始化的, 没初始化就在里面调 DXGI 会直接 native 崩溃
        # (0xC0000409, Python 侧抓不到任何异常, 只看到 "Unhandled Python
        # exception")。这就是"玻璃层一显示、截屏线程起来就崩"的原因。
        com_ready = _co_initialize()
        try:
            self._open()
        except Exception:  # noqa: BLE001
            if com_ready:
                _co_uninitialize()
            raise
        seq = 0
        try:
            seq = self._run_loop(seq)
        finally:
            if com_ready:
                _co_uninitialize()

    def _run_loop(self, seq):
        while not self._halt:
            hz = self.target_hz
            if hz <= 0:
                # 空闲: 等主线程 kick (玻璃层关着/收起来了, 不要白烧)。
                # 超时说明**没人叫我**, 直接回去等 —— 别顺手抓一帧, 那样
                # 空闲时也会以 1/timeout 的频率白抓。
                if not self.request.wait(timeout=0.5):
                    continue
                self.request.clear()
                if self._halt:
                    break
            t0 = time.perf_counter()
            self.busy = True
            try:
                self.pumps += 1
                # 实测**抓屏速率** (每秒抓了几次)。这才是"重截频率"的真实值;
                # 别用 _store 的次数 —— 那只是"内容真的变了"的帧数, 桌面静止
                # 时近乎 0, 看起来像"帧率 0"。
                _now = time.time()
                if _now - self._rate_t >= 0.5:
                    self.rate_hz = self._rate_n / (_now - self._rate_t)
                    self._rate_t, self._rate_n = _now, 0
                self._rate_n += 1
                seq = self._pump_once(seq)
            except Exception as exc:  # noqa: BLE001
                print("[capture] 抓屏失败: %s" % exc)
                time.sleep(0.05)
            finally:
                self.busy = False
                self.done.set()
            if hz > 0:
                spare = (1.0 / hz) - (time.perf_counter() - t0)
                if spare > 0:
                    time.sleep(spare)
        if self._dxgi is not None:
            self._dxgi.release()

    def _maybe_reopen(self):
        """换屏后重挑采集设备 (WGC 会话 / DXGI output) —— **只在采集线程里调**。

        WGC 和 DXGI 后端都在 open 时**绑定了某一块显示器**, 只改 region 不会
        换屏 —— 它照旧抓原来那块屏。这正是"在设置里选了副屏, 画面却还是主屏"
        的根因。这里把旧会话放掉, 按新的 size/origin 重挑。挑不到就退回 mss
        —— mss 直接用 region 抓, 不受绑定影响。

        注意顺序: 先 `_drop_dxgi` 再把 backend 改成 "mss" —— `_drop_dxgi`
        只在 self._dxgi 非 None 时才动手, 所以这里不能用它清 WGC。
        """
        if not self._reopen_pending:
            return
        self._reopen_pending = False
        if self._wgc is None and self._dxgi is None:
            return                      # mss 后端: region 改了就行, 无需重开
        with self.lock:
            size, origin = self.size, self.origin
        if self._wgc is not None:
            old, self._wgc = self._wgc, None
            try:
                old.release()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._wgc = _WgcSource(size, origin=origin)
                self.backend = "wgc"
                print("[capture] 已切到显示器: wgc, %dx%d @ 桌面坐标 %s"
                      % (size + (origin,)))
            except Exception as exc:  # noqa: BLE001
                print("[capture] 换屏后 WGC 重开失败, 退回 mss: %s" % exc)
                self.backend = "mss"
                if self._sct is None:
                    self._sct = mss.mss()
            return
        old, self._dxgi = self._dxgi, None
        try:
            old.release()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._dxgi = _DxgiSource(size, origin=origin)
            self.backend = self._dxgi.name
            print("[capture] 已切到显示器: %s output=%d, %dx%d @ 桌面坐标 %s"
                  % (self._dxgi.name, self._dxgi.output_idx, *size, origin))
        except Exception as exc:  # noqa: BLE001
            print("[capture] 换屏后 DXGI 重开失败, 退回 mss: %s" % exc)
            self._drop_dxgi()

    def _pump_once(self, seq):
        self._maybe_reopen()
        if self._wgc is not None:
            t0 = time.perf_counter()
            try:
                arr = self._wgc.grab()
            except Exception as exc:  # noqa: BLE001
                print("[capture] WGC 出错, 退回 DXGI/mss: %s" % exc)
                self._wgc.release()
                self._wgc = None
                arr = None
            if arr is not None:
                elapsed = (time.perf_counter() - t0) * 1000.0
                self.last_ms = elapsed if self.last_ms <= 0 else \
                    self.last_ms * 0.8 + elapsed * 0.2
                self.frames_in += 1
                remember_capture_backend("wgc")
                return self._store(arr, arr.shape[1], arr.shape[0], seq + 1, "BGRA")
            # 没帧 = 这一瞬没有新帧
            if self.frame is None:
                self._grab_mss(seq)      # 初始兜底种子帧
            return seq
        if self._dxgi is not None:
            t0 = time.perf_counter()
            try:
                arr = self._dxgi.grab()
            except Exception as exc:  # noqa: BLE001
                print("[capture] DXGI 出错, 退回 mss: %s" % exc)
                self._drop_dxgi()
                arr = None
            if arr is not None:
                elapsed = (time.perf_counter() - t0) * 1000.0
                self.last_ms = elapsed if self.last_ms <= 0 else \
                    self.last_ms * 0.8 + elapsed * 0.2
                self.frames_in += 1
                remember_capture_backend(self._dxgi.name)
                return self._store(arr, arr.shape[1], arr.shape[0], seq + 1, "BGRA")
            # 没帧 = 这一瞬没有新帧
            if self.frame is None:
                # 初始兜底种子帧
                self._grab_mss(seq)
            return seq
        return self._grab_mss(seq)

    def _grab_mss(self, seq):
        if self._sct is None:
            self._sct = mss.mss()
        # **在锁内取 region 快照。** set_region 会在 self.lock 下改 self.region
        # (主线程切屏), 这里若直接把自己的引用传进去, 可能拿到"半更新"的字典
        # (改到一半被读)。取一份拷贝再用, 抓这一帧就用它自己的 region。
        with self.lock:
            region = dict(self.region) if self.region else self.region
        t0 = time.perf_counter()
        shot = self._sct.grab(region)
        raw = shot.raw
        if self._dxgi is None:
            self.last_ms = (time.perf_counter() - t0) * 1000.0
        return self._store(raw, shot.width, shot.height, seq + 1, "BGRA")

    def _store(self, data, w, h, seq, fmt):
        # **DXGI 的数组不拥有自己的内存** —— bettercam 只维护几个缓冲轮换
        # (`flags["OWNDATA"] == False`, 实测只有 3 个 id 交替)。必须**真的拷贝
        # 一份**再存, 否则渲染层上传纹理时读到的是已经被下一次 grab 覆写的
        # 内存 —— 轻则撕裂、看到旧图, 重则踩到失效缓冲**直接 native 崩溃**
        # (0xC0000409, Python 侧抓不到)。
        #
        # ⚠️ **不能用 `np.ascontiguousarray`**: 它对"内存已经连续"的数组
        # **原样返回、不做拷贝**。而 bettercam 给的数组恰好就是 C_CONTIGUOUS,
        # 于是 `ascontiguousarray` 返回同一个对象 (`b is a == True`, data 指针
        # 完全相同, OWNDATA 仍然是 False) —— 那个"保护"从来就没生效过。
        # 我原来就是这么写的, 一直以为有拷贝, 直到它真的崩了才量出来。
        # 必须真的拷贝。16MB 拷一次约 1.5~3ms, 在 refresh_hz=30 下可接受 ——
        # 这是**正确性**的成本, 不能省。
        #
        # ═════════════════════════════════════════════════════════════════
        # **双缓冲** (预分配 `_bufs` 两块轮流写 + `np.copyto`)
        # ═════════════════════════════════════════════════════════════════
        # 每帧 `arr.copy()` 都要 malloc/free 16MB 级大块, Python 堆"涨了不缩",
        # 常驻 RSS 里沉淀出几十 MB 碎片; 分配开销也让拷贝本身变慢。改成**两块
        # 预分配缓冲按帧号奇偶轮流写入**, 块里只发生 `np.copyto` (同样拷全部
        # 字节, 无分配)。
        #
        # 安全性 —— 渲染层不会读到"正在被覆写"的缓冲:
        #   1. overlay.paintGL 只在帧号变化时把 latest() 拷进 GL 纹理, 且这是
        #      **同步调用**, paintGL 返回前一定拷完;
        #   2. 同一块预分配缓冲要隔 2 帧才被复用 (奇偶轮换), 而 capture_hz 最多
        #      是重绘率的 2 倍 —— 采集线程写 buf[N%2] 时, 渲染层对它的上一次
        #      使用早已结束;
        #   3. 双缓冲足够覆盖后端自有缓冲的轮换深度 (bettercam 实测 3 块)。
        # 语义与旧版逐字节一致: 挂出去的永远是"我们拥有的独立内存"。
        if isinstance(data, (bytes, bytearray, memoryview)):
            # mss 给 bytes: 灌进预缓冲, 顺便把"frombuffer 只读视图"问题一起消掉
            buf = self._buf_for(w, h)
            buf[:] = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
        else:
            arr = np.asarray(data)
            buf = self._buf_for(w, h)
            np.copyto(buf, arr.reshape(h, w, 4))
        with self.lock:
            self.frame = (buf, w, h, seq, fmt)
        self.grabs += 1
        return seq

    def _buf_for(self, w, h):
        """取本帧该写的预分配缓冲 (按帧号奇偶轮换); 尺寸变了就整对重建。

        换屏/DPI 虚拟化都会让 w/h 变 —— 按新尺寸重建, 旧尺寸的缓冲自然被
        替换掉, 不会累积。只在采集线程调用, 无并发。
        """
        idx = (self.grabs + 1) % 2
        buf = self._bufs[idx]
        if buf is None or buf.shape != (h, w, 4):
            buf = np.empty((h, w, 4), dtype=np.uint8)
            self._bufs[idx] = buf
        return buf


def make_capture(region, cfg, display_hz=60.0):
    """按 config 建 CaptureWorker。三个调用点共用, 免得哪天漏传参数。"""
    return CaptureWorker(
        region,
        size=(region.get("width"), region.get("height")),
        backend=str(cfg.get("capture_backend", "auto")),
        display_hz=float(display_hz or 60.0))
