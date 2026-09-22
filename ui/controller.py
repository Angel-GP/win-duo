"""AppController —— 把角度源、截屏、玻璃层捏在一起, 管它们的开关与重建。

托盘和设置窗口只跟这个对象打交道, 不直接碰 angles/ 和 render/。
几条关键约定:
  - 玻璃层默认关着。关着的时候角度源是**停掉**的 —— 摄像头模式常驻要吃
    ~70% 单核, 没开玻璃层就没必要占着设备。
  - 换显示器要同时改两样: 截屏区域 (mss 用物理像素) 和玻璃层窗口几何。
  - 改了摄像头 index/后端之后必须重建角度源对象, 否则还连着旧设备。
"""
import ctypes
import json
import os
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from angles.hub import SourceHub
from angles.manual import KeyControl
from render.capture import make_capture
from render.overlay import GlassOverlay

from . import monitors
from .hotkey import HotkeyManager

#: 全局热键表: (名字, config 键, 说明, 生效模式)
#:   None     = 一直生效
#:   "manual" = 只在「键盘」角度源下生效
#:
#: 键盘模式要用**全局热键**而不是控制台按键: 启动器是 pythonw.exe, 没有控制台,
#: `msvcrt.getwch()` 根本读不到键。
#:
#: **摄像头模式下只留紧急停止。** 摄像头模式是"全自动跟手"的, 多一个键反而
#: 容易误触 (尤其浓度键会和自动跟踪打架、标定/翻转会在你不想动的时候改参数)。
#: 标定和翻转方向改用设置窗口里的按钮。
#: 全局热键定义: (内部名, 配置键, 说明文案, 生效模式)
#:
#: **生效模式** = 只有当前角度源等于它时才注册 (None = 总是注册):
#:   - `off` (紧急关闭): None —— **任何模式都要有**, 它是屏幕被玻璃层盖住、
#:     托盘也点不到时的最后退路。
#:   - `calibrate` (标定): camera —— 标定的意义是给摄像头"建立基准帧",
#:     所以只在摄像头源下有意义。串口/键盘源下不注册 (按了也没用, 显示出来
#:     只会误导)。
#:   - 其余 (开关玻璃/调试窗/浓度): manual —— 那些是键盘模式下的调节手段。
HOTKEY_DEFS = (
    ("off", "hotkey_off", "紧急关闭（关玻璃层并退出）", None),
    ("calibrate", "hotkey_calibrate", "标定基准帧（上盖完全展开时按）", "camera"),
    ("toggle", "hotkey_toggle", "开关玻璃层", "manual"),
    ("debug", "hotkey_debug", "匹配调试窗", "manual"),
    ("level_up", "hotkey_level_up", "浓度 +5%", "manual"),
    # 注: 用普通半角 `-` 而不是全角 `−` (U+2212) —— 后者在 GBK 等编码下
    # 会 UnicodeEncodeError, 打印/写日志时直接崩 (实测踩过)。
    ("level_down", "hotkey_level_down", "浓度 -5%", "manual"),
    ("level_full", "hotkey_level_full", "浓度拉满 100%", "manual"),
    ("level_zero", "hotkey_level_zero", "浓度清零 0%", "manual"),
)


class AppController(QObject):
    glassChanged = pyqtSignal(bool)
    notified = pyqtSignal(str)          # 需要弹气泡提示的消息

    def __init__(self, cfg, cfg_path, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.cfg_path = Path(cfg_path)
        self.control = KeyControl(cfg, auto=(cfg.get("source") != "manual"))
        self.control.start()
        self.hub = SourceHub(cfg, self.control, autostart=False)
        self.capture = make_capture(
            monitors.region_for(self.screen()), cfg,
            display_hz=self.screen().refreshRate())
        self.capture.start()
        self.overlay = None
        self.glass_on = False
        self._scan_was_running = False
        #: 低内存模式的周期检查定时器 (惰性创建) + "从何时起一直不可见"
        self._release_timer = None
        self._last_hidden_at = None
        #: 连续"该显示了"的计数 (防噪声抖动, 见 _RECREATE_CONFIRM)
        self._recreate_streak = 0
        self._shutting_down = False
        #: ═══ 分级释放·软裁剪 (替代低内存模式的默认行为) ═══
        #: 周期检查定时器 (与 _release_timer 共用轮询) + 隐藏起点/"已裁过"标记
        self._soft_timer = None
        self._soft_trimmed = False
        self._soft_hidden_since = None

        # 全局热键由 controller 管: 键盘模式那几个键要随角度源切换动态注册/注销
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self._hotkey_ok = {}          # name -> 是否注册成功

        # **启动时就把低内存模式应用上。** 早先版本只在"用户拨开关"那一刻起
        # 计时器, 所以用户开了开关重启后配置虽是 true 却没人应用 —— 表现就是
        # "开了没生效"。放进构造函数就不依赖任何调用时机了。
        if self.cfg.get("low_memory_mode", False):
            self._start_release_poll()

    # ------------------------------------------------------------ 全局热键
    def register_hotkeys(self):
        """按当前角度源注册/注销热键。切换角度源时会再调一次。"""
        active = self.hub.active_name()
        for name, key, label, mode in HOTKEY_DEFS:
            spec = self.cfg.get(key)
            if not spec or (mode is not None and mode != active):
                if self.hotkeys.has(name):
                    self.hotkeys.unregister(name)
                self._hotkey_ok.pop(name, None)
                continue
            if self.hotkeys.has(name):
                continue
            ok = self.hotkeys.register(spec, name)
            self._hotkey_ok[name] = ok
            print("[hotkey] %-22s %s%s"
                  % (spec, label, "" if ok else "   ← 注册失败(可能被别的程序占用)"))

    def hotkey_lines(self):
        """当前**生效**的热键说明, 给设置窗口显示用。

        只列真正注册成功的 —— 显示一堆按了没反应的键反而误导。
        """
        active = self.hub.active_name()
        out = []
        for name, key, label, mode in HOTKEY_DEFS:
            if mode is not None and mode != active:
                continue
            if not self._hotkey_ok.get(name):
                continue
            out.append((str(self.cfg.get(key, "")), label))
        return out

    def _on_hotkey(self, name):
        if name == "toggle":
            self.toggle_glass()
        elif name == "off":
            self.emergency_off()
        elif name == "calibrate":
            # 标定摄像头基准帧 (上盖完全展开时按才有意义)。
            # **不依赖设置窗口** —— 摄像头模式下用户可能正对着屏幕合盖,
            # 有个全局热键就能随时重标, 不用去托盘开窗口。
            self.calibrate_camera()
        elif name == "level_up":
            self.bump_level(+0.05)
        elif name == "level_down":
            self.bump_level(-0.05)
        elif name == "level_full":
            self.set_manual_level(1.0)
        elif name == "level_zero":
            self.set_manual_level(0.0)
        # 注: HOTKEY_DEFS 里没有 flip (翻转方向已改成设置窗口的按钮),
        # 所以这里没有对应分支 —— 免得看着像"热键还在"。
        elif name == "debug":
            self.toggle_debug_window()
        else:
            print("[hotkey] 未知动作 %r" % name)

    def release_hotkeys(self):
        try:
            self.hotkeys.release()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 显示器
    def screen(self):
        return monitors.resolve(self.cfg)

    def set_screen(self, screen):
        if screen is None:
            return
        self.cfg["screen_name"] = screen.name()
        self.cfg["screen_index"] = monitors.index_of(screen)
        self.capture.set_region(monitors.region_for(screen))
        # #12: 换屏后刷新率可能变了 (如 165Hz 主屏 -> 60Hz 副屏), 更新采集上限,
        # 否则 max_hz() 仍按旧屏给, 重截频率上限不对。
        try:
            self.capture.display_hz = float(screen.refreshRate() or 60.0)
        except Exception:  # noqa: BLE001
            pass
        if self.overlay is not None:
            self.overlay.set_screen(screen)
            self.overlay.enabled = self.glass_on
            # 换屏清掉了 frame/上传纹理, 下一帧到达前 paintGL 会清成黑 -> 闪一下。
            # 和"首帧闪黑"同源: 让窗口先藏起来, 等新屏首帧就绪再显示。
            self.overlay.apply_config()      # 让 capture_hz 等按新屏重算
            if self.glass_on and not self.overlay.suppressed:
                self.overlay.hide()
                self._show_overlay_when_ready()

    # ------------------------------------------------------------ 玻璃层
    def _ensure_overlay(self):
        if self.overlay is None:
            self.overlay = GlassOverlay(self.screen(), self.hub,
                                        self.capture, self.control, self.cfg)
        return self.overlay

    # ------------------------------------------------------------ 低内存模式
    #: 窗口**连续不可见**多久之后释放 (秒)。**很短** —— 用户要的是"不用等一会"。
    #: 3 秒足够跨过"合盖过程中窗口短暂 hide"的瞬间, 又不会让那 120MB 白占。
    IDLE_RELEASE_SEC = 3.0
    #: 检查周期 (毫秒)。
    _RELEASE_POLL_MS = 250
    #: 建窗口前要求 level **连续**超过阈值多少个周期 (防抖)。
    #:
    #: 这是关键: 摄像头噪声会偶尔冒个尖峰 (实测 level 0.005 这种), 只要一次就
    #: 建窗口的话, 25 秒内会建 10 次 —— **反复创建/销毁 GL 上下文会把驱动搞出
    #: 状况**, 表现就是"用一会儿就不正常了/像被自动关了"。要求连续 2 次
    #: (共 500ms) 才动手; 真实合盖持续 1 秒以上, 不会被误挡。
    _RECREATE_CONFIRM = 2

    def apply_low_memory_mode(self):
        """低内存模式开关**变更时**调用 (设置窗口拨开关)。

        真正决定"此刻该不该有窗口"的是周期检查 `_maybe_release_overlay()` ——
        它直接读 `cfg["low_memory_mode"]`, 不依赖"谁在什么时候调了这个方法"。
        早先只靠一次性定时器, 有个漏洞: **启动时没人调它** —— 用户开了开关重启
        后配置是 true, 却没有任何代码去应用 (表现就是"开了不生效")。
        """
        if self.cfg.get("low_memory_mode", False):
            self._start_release_poll()
            # 立刻按新模式收敛一次 (该释放就释放 / 该裁就裁), 不用等下一个周期
            self._maybe_release_overlay()
        else:
            self._stop_release_poll()
            self._last_hidden_at = None
            self._recreate_streak = 0
            # 关掉模式要把窗口建回来, 否则用户下次合盖会卡一下
            if self.glass_on and self.overlay is None:
                self.start_glass()

    def _overlay_visible(self):
        """玻璃层窗口此刻是不是真的显示在屏幕上。"""
        ov = self.overlay
        return bool(ov is not None and self.glass_on
                    and not getattr(ov, "suppressed", False)
                    and getattr(ov, "_visible", False))

    def _glass_wants_window(self):
        """低内存模式下: 现在**该不该**有 GL 窗口。

        判据 = 玻璃层开着 且 浓度够高 (真的要显示)。这样"开着待命"时**根本
        不建窗口** —— 那 120MB 一点都不会被加载, 而不是"建了再释放"。
        比"建-释放-再建"稳得多 (后者反复创建 GL 上下文, 实测会把驱动搞出毛病),
        也不会在待命时白占内存。
        """
        if not self.glass_on:
            return False
        try:
            level = self.hub.resolve()[0]
        except Exception:  # noqa: BLE001
            return False
        if level is None:
            return False
        from render.overlay import IDLE_SHOW_ABOVE
        return level >= IDLE_SHOW_ABOVE

    def _start_release_poll(self):
        if self._release_timer is None:
            self._release_timer = QTimer(self)
            self._release_timer.timeout.connect(self._maybe_release_overlay)
        if not self._release_timer.isActive():
            self._release_timer.start(self._RELEASE_POLL_MS)

    def _stop_release_poll(self):
        if self._release_timer is not None:
            self._release_timer.stop()

    def _maybe_release_overlay(self):
        """周期检查 (低内存模式):

          - 没窗口 且 浓度够高 (连续 `_RECREATE_CONFIRM` 次) -> 建窗口;
          - 有窗口 但 已隐藏够久 (`IDLE_RELEASE_SEC`)          -> 销毁 + 裁内存;
          - 用户在托盘里关掉玻璃层 (glass_on=False)            -> 销毁 + 裁内存。

        **本模式的核心是"不建", 而不是"建了再放"**: 实测 (2560x1600) GL 窗口
        一旦 show 过, 那 ~120MB 的驱动 DLL 映射就驻留, `hide()` 和销毁窗口都
        不还 —— 必须再裁一次工作集才降得下来 (215MB -> 5MB)。所以待命状态
        压根不建窗口, 是最稳也最省的。
        """
        if self._shutting_down or not self.cfg.get("low_memory_mode", False):
            return
        now = time.time()

        if self.overlay is None:
            # ---- 没有窗口: 看要不要建 ----
            if not self._glass_wants_window():
                self._recreate_streak = 0
                return
            self._recreate_streak += 1
            if self._recreate_streak < self._RECREATE_CONFIRM:
                return                       # 再观察一个周期, 防噪声尖峰
            self._recreate_streak = 0
            print("[glass] 低内存模式: 该显示了, 建 GL 窗口")
            # **不重新标定** —— 这是"合盖过程中"的重建, 上盖没有展开, 标定会把
            # 合到一半的画面当基准, 导致 level 归零、玻璃层刚亮起又被关掉。
            self.start_glass(recalibrate=False)
            return

        # ---- 有窗口: 看要不要放 ----
        self._recreate_streak = 0
        if self._overlay_visible():
            self._last_hidden_at = None      # 又显示出来了, 重新计时
            return
        if self._last_hidden_at is None:
            self._last_hidden_at = now
            return
        if now - self._last_hidden_at < self.IDLE_RELEASE_SEC:
            return
        self._release_overlay()

    def _release_overlay(self):
        """销毁 GL 窗口, 并把工作集**真正还给系统**。

        只销毁窗口是不够的: 实测 (2560x1600, tasklist 读数)
            只建 QApplication        39MB
            建 GL 窗口              133MB   (+94: NVIDIA 驱动 DLL 的文件映射)
            销毁窗口                132MB   (**几乎不降!**)
            再调内存回收             8MB   <- 关键
        那 94MB 是 `nvwgf2umx.dll`(81MB) / `nvgpucomp64.dll`(77MB) /
        `nvoglv64.dll`(41MB) 这些**驱动 DLL 的映射页**。GL 上下文一加载它们就
        驻留, 销毁窗口**不会卸载**; 只有把工作集裁掉才会挤出去 —— 它们本来就是
        共享的只读映射, 挤出去零风险, 要用手上再缺页调回。

        **不动 `glass_on`**: 它表示"用户要不要玻璃层", 和"窗口此刻存不存在"
        是两件事。窗口没了的话, 下次开合会由周期检查建回来。
        """
        if self.overlay is None:
            return
        print("[glass] 低内存模式: 释放 GL 窗口 (~130MB)")
        try:
            self.overlay.shutdown_gl()
            self.overlay.setParent(None)
            self.overlay.deleteLater()
        except Exception as exc:  # noqa: BLE001
            print("[glass] 释放 GL 窗口失败: %s" % exc)
        self.overlay = None
        self._last_hidden_at = None
        #: 连续的"该显示了"次数 (防抖, 见 `_RECREATE_CONFIRM`)
        self._recreate_streak = 0
        # 硬释放把窗口销毁了, 软裁剪的状态一并复位 (软检查对 None 窗口本就空转,
        # 这里清标记是防止窗口重建后沿用上一轮隐藏的"已裁过"状态)
        self._soft_trimmed = False
        self._soft_hidden_since = None
        # 回收要**延迟**做: 实测紧跟在 `deleteLater()` 之后调, 工作集几乎不降
        # (214MB 还是 214MB) —— 那时窗口/上下文还没真正销毁完, 而
        # `SetProcessWorkingSetSize` 是异步的 (只设目标, 裁剪要等系统处理),
        # 那一瞬间 Qt 还在跑, 页立刻又被调回。隔一小段再裁就降到 8MB。
        QTimer.singleShot(400, self.reclaim_memory)

    def reclaim_memory(self):
        """把本进程的工作集裁到最小 (把可回收页还给系统)。

        实测: "建过 GL 窗口" 之后 214MB -> **8MB** (tasklist 读数)。
        那 ~120MB 主要是 `nvwgf2umx.dll` / `nvgpucomp64.dll` / `nvoglv64.dll`
        这些**驱动 DLL 的文件映射页** —— GL 上下文加载后它们驻留, 销毁窗口不
        卸载, 但它们本来就是共享的只读映射, 裁出去零风险 (要用手上再缺页调回)。

        ⚠️ **必须用 `OpenProcess` 的真句柄**, 不能用 `GetCurrentProcess()` 的
        伪句柄 (-1): 后者在本机调这两个 API 直接失败 (`err=6`), 我一开始就是
        因此误判成"回收根本没用"。

        幂等、无害: 失败只是省不到内存, 所以异常全吞。

        **窗口已经建回来时直接跳过。** 裁剪会把驱动 DLL 的页从工作集里挤出
        去, 紧接着玻璃层要用就得缺页调回 —— 那一瞬间会明显拖慢重绘 (实测
        重绘从 ~28/s 掉到 4~10/s)。所以只在"窗口确实不存在"时才裁。
        软裁剪路径 (窗口还活着) 走 `_force_trim_working_set`。
        """
        if self.overlay is not None:
            return False
        return self._force_trim_working_set()

    def _force_trim_working_set(self):
        """裁工作集的核心实现 (无前置条件)。硬/软两条释放路径共用。"""
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            # **必须声明 argtypes/restype** —— 否则 `c_size_t(-1)` 会被按默认
            # int 传递、在 64 位上被截断, 调用直接崩 (我踩过: 独立脚本里就崩了)。
            k32.SetProcessWorkingSetSize.argtypes = [
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
            k32.SetProcessWorkingSetSize.restype = ctypes.c_int
            psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            psapi.EmptyWorkingSet.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            k32.GetCurrentProcessId.restype = ctypes.c_ulong

            h = k32.OpenProcess(0x0400 | 0x0100, False,       # QUERY_INFO|SET_QUOTA
                                k32.GetCurrentProcessId())
            if not h:
                print("[glass] 裁剪: OpenProcess 失败 err=%d"
                      % ctypes.get_last_error())
                return False
            try:
                k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1).value,
                                             ctypes.c_size_t(-1).value)
                psapi.EmptyWorkingSet(h)
            finally:
                k32.CloseHandle(h)
            return True
        except Exception as exc:  # noqa: BLE001
            print("[glass] 裁剪失败 %s" % exc)
            return False

    def _touch_idle_timer(self):
        """玻璃层被关闭时重置空闲计时 (低内存模式下)。"""
        if self._shutting_down:
            return
        self._last_hidden_at = None
        if self.cfg.get("low_memory_mode", False):
            self._start_release_poll()

    # ------------------------------------------------------------ 分级释放·软裁剪
    #: 玻璃层隐藏持续多久后把驱动 DLL 页从工作集裁出去 (秒)。
    #:
    #: ⚠️ **必须够长。** 原值 3.0 太激进: 合盖动画的典型节奏是"收起(浓度≈0,
    #: 玻璃层隐藏) -> 很快又合盖/展开", 3 秒就裁的话几乎每次都命中, 于是每次
    #: 恢复显示都要为驱动 DLL 页缺页回填。**实测: 裁剪后首帧 ~19ms, 稳态只有
    #: 7~8ms —— 单帧慢一倍多**, 表现就是"合盖刚开始、交界处出现时卡一下"。
    #: 作者原注释以为"软缺页回填是微秒级、首帧几乎无感", 实测不成立。
    #: 30 秒: 只有真正长时间待命(玻璃层一直没动)才裁, 正常使用完全不触发。
    SOFT_TRIM_SEC = 30.0
    #: 软裁剪周期检查 (毫秒)。复用一个惰性定时器。
    _SOFT_POLL_MS = 500

    def _start_soft_poll(self):
        """软裁剪轮询只在 glass_on=True 期间需要 (隐藏 -> 裁 -> 显示 -> 复位)。"""
        if self._soft_timer is None:
            self._soft_timer = QTimer(self)
            self._soft_timer.timeout.connect(self._maybe_soft_trim)
        if not self._soft_timer.isActive():
            self._soft_timer.start(self._SOFT_POLL_MS)

    def _stop_soft_poll(self):
        if self._soft_timer is not None:
            self._soft_timer.stop()
        self._soft_trimmed = False

    def _overlay_hidden_since(self):
        """玻璃层开着但已隐藏的起点时刻; 正显示着返回 None。

        判据与低内存模式的 _overlay_visible 相同: 玻璃层开 + 未被临时收起 +
        真的不可见。隐藏起点由本函数自行记录 (首个"观察到隐藏"的轮询拍),
        不依赖 overlay 逐个 hide() 点打桩。
        """
        if not self.glass_on or self.overlay is None:
            return None
        ov = self.overlay
        if not getattr(ov, "suppressed", False) and getattr(ov, "_visible", False):
            return None
        # 窗口建过 GL (initializeGL 跑过) 才有驱动 DLL 页可裁; 从未 show 过
        # 的待命窗口没加载过驱动页, 裁了也白裁, 还会把预热的页误裁掉。
        if not getattr(ov, "_gl_ready", False):
            return None
        # 已隐藏 -> 起点第一次观察到时打上
        if self._soft_hidden_since is None:
            self._soft_hidden_since = time.time()
        return self._soft_hidden_since

    def _maybe_soft_trim(self):
        """软裁剪周期检查: 玻璃层隐藏满 SOFT_TRIM_SEC -> 裁工作集, **不销毁窗口**。

        这是低内存模式的替代方案。分级:
          - 本层 (软): 隐藏满 SOFT_TRIM_SEC -> EmptyWorkingSet 把 ~120MB 驱动 DLL
            映射页挤出工作集。GL 上下文/窗口还活着 —— 下次合盖**不需要重建窗口**。
          - 低内存模式 (硬, 若用户开了): 隐藏 3 秒 -> 整个销毁窗口。省得更多,
            代价是下次 ~180ms 重建 + 驱动重初始化。两者不叠加: 硬路径先触发
            时窗口已销毁, 软检查自然空转。

        ⚠️ **软裁剪不是免费的**: 被挤出的驱动 DLL 页在下次绘制时要缺页回填,
        实测首帧 7~8ms -> ~19ms (单帧慢一倍多)。所以 SOFT_TRIM_SEC 必须足够长
        (30 秒), 只在真正长时间待命时才裁 —— 否则合盖动画一开始就会卡一下。
        """
        if self._shutting_down or not self.glass_on or self.overlay is None:
            return
        since = self._overlay_hidden_since()
        if since is None:
            # 正在显示 (或窗口没了): 复位计时与"已裁过", 下次隐藏重新来
            self._soft_trimmed = False
            self._soft_hidden_since = None
            return
        if self._soft_trimmed:
            return                          # 本轮隐藏已经裁过, 不重复
        if time.time() - since < self.SOFT_TRIM_SEC:
            return
        # 注意走 _force_trim_working_set: reclaim_memory 有 "窗口不存在才裁"
        # 的守卫, 那是给硬释放 (低内存模式) 用的; 软路径窗口故意保留。
        if self._force_trim_working_set():
            self._soft_trimmed = True
            print("[glass] 待命 %.0f 秒: 已裁工作集 (窗口保留, 恢复时不重建窗口)"
                  % self.SOFT_TRIM_SEC)

    def start_glass(self, recalibrate=True):
        """开启玻璃层。

        `recalibrate`: 开启后要不要自动标定一次摄像头基准帧。**只有"用户主动
        开玻璃层"时才该标定** —— 那时上盖是展开的, 把当前帧当 pitch=0 的基准是
        对的。

        低内存模式下的"闲置释放 + 合盖重建"绝不能标定: 重建恰恰发生在**上盖正在
        合上** (level 涨过阈值) 的时候, 此时上盖并不展开。若照常在 1.6s 后标定,
        就把"合到一半"的画面当成了基准 → pitch 归零 → level 掉回 0 → 窗口又被
        隐藏并释放 —— 表现就是"低内存模式下玻璃层刚亮起来又被异常关掉"。
        """
        # 窗口可能已被低内存模式释放掉 (此时 glass_on 仍是 True) —— 那种情况
        # 必须继续往下走把窗口建回来, 不能因为 glass_on 就 return。
        if self.glass_on and self.overlay is not None:
            return
        self.hub.start_active()
        self._ensure_overlay().enabled = True
        self.capture.kick()
        # **等第一帧就绪再 show()。** 否则首次 paintGL 手上没有截图纹理
        # (_uploaded_seq == -1), 会把整屏 glClear 成黑色 —— 这一帧被合成器抓到,
        # 屏幕就"闪一下黑"。启动自动开玻璃层时截屏线程刚起步、还没产出第一帧,
        # 于是每次开程序后第一次真正显示玻璃层都会闪 (之后纹理已在, 不再闪)。
        # 等到有帧再显示: level≈0 时画的是桌面本身(恒等映射, 看不出来),
        # level>0 时画的是真实内容, 两种都不会出现黑场。
        self._show_overlay_when_ready()
        self.glass_on = True
        # 分级释放·软裁剪: 开着玻璃层的整个期间轮询"隐藏是否满 3 秒"。
        # 与低内存模式 (硬释放) 不冲突 —— 硬路径触发时窗口被销毁, 软检查自然空转。
        self._start_soft_poll()
        self.glassChanged.emit(True)
        # 角度源是**异步**打开的 (CameraAngleSource.start 里说明过原因), 这里
        # 还判断不出可用性, 过一会儿再回来看
        self._schedule_source_check()
        # 打开悬浮玻璃开关后自动标定一次基准帧 (仅限用户主动开启; 低内存重建不标定)
        if recalibrate and self.cfg.get("autocal_on_glass_open", True):
            # **不能盲等固定时间再标定**: 摄像头是异步打开的 (最坏要 ~20 秒),
            # 写死 1.6s 会在设备还没出图时就去标定 —— 基准帧是黑的/未稳定的,
            # 之后测角全错。改成轮询到"真的就绪"再标定。
            self._auto_calibrate_when_ready()
        print("[glass] 已开启")

    def _show_overlay_when_ready(self, tries=0):
        """截图就绪后再 show 玻璃层, 避免首帧空纹理闪黑 (见 start_glass 里的说明)。

        启动阶段截屏线程要花几十毫秒产出第一帧; 那之前先 kick + 轮询等待。
        用户主动开玻璃层时截屏一直在跑、latest() 已非空, 会立刻显示、无延迟。
        最多等 ~2 秒 (40×50ms) 兜底, 真等不到也照常显示 (退化回原行为)。
        """
        ov = self.overlay
        if ov is None or ov.isVisible():
            return
        if self.capture.latest() is None and tries < 40:
            self.capture.kick()
            QTimer.singleShot(50, lambda: self._show_overlay_when_ready(tries + 1))
            return
        ov.show()

    def _auto_calibrate_when_ready(self, tries=0):
        """等摄像头**真的就绪** (打开成功 + 已出图) 后再自动标定一次。

        为什么要等而不是定时: 设备异步打开, 最坏 ~20 秒。原来写死 1.6 秒, 摄像头
        还没出图就去标定, 基准帧是黑的/未稳定的 -> 整条测角都不对。
        这里每 250ms 查一次 `ready()`, 最多等 ~30 秒; 超时就放弃 (不硬标)。
        """
        if not self.glass_on or self.hub.active_name() != "camera":
            return                              # 已关玻璃层/换了源 -> 不必标了
        cam = self.hub.get("camera")
        if cam is None:
            return
        # 出错/不可用就别等了
        if getattr(cam, "available", lambda: True)() is False:
            print("[camera] 不可用, 跳过自动标定")
            return
        if not getattr(cam, "ready", lambda: False)():
            if tries < 120:                     # 120×250ms ≈ 30s 上限
                QTimer.singleShot(
                    250, lambda: self._auto_calibrate_when_ready(tries + 1))
            else:
                print("[camera] 等待就绪超时, 跳过自动标定 (可手动标定)")
            return
        # 摄像头源自己启动后也会自动标定一次 (~1 秒出图后)。如果它已经标好了,
        # 这里就别再标 —— 否则同一段启动里白标两次, 第二次纯粹多余。
        if getattr(cam, "calibrated", lambda: False)():
            print("[camera] 摄像头源已自动标定, 无需重复")
            return
        print("[camera] 设备已就绪, 自动标定基准帧")
        self.calibrate_camera()

    def _auto_calibrate_on_open(self):
        if self.glass_on and self.hub.active_name() == "camera":
            self.calibrate_camera()

    def screen_hz(self):
        """当前显示器的刷新率 —— 重截频率的最高可用值。"""
        try:
            return float(self.screen().refreshRate() or 60.0)
        except Exception:  # noqa: BLE001
            return 60.0

    def prewarm_overlay(self, tries=0):
        """提前把 GL 上下文建好、着色器编译好。

        QOpenGLWidget 的 initializeGL/paintGL 只有在窗口被 show 过之后才会跑,
        而**第一次要 ~680ms** (建 GL 上下文 + 编译链接着色器)。实测:
            第 1 次 start_glass = 676ms     第 2 次 = 3ms
        这一步迟早要花, 但绝不能花在用户点"开启玻璃层"的那一刻 —— 那就是
        用户感觉到的"卡一下"。所以启动后 (玻璃层还关着) 先把它做掉。

        等第一张截图就绪再做: 那时它画的就是桌面本身, 即使被合成器抓到一帧也
        看不出区别; 没有截图的话会闪一下黑屏。

        **低内存模式下不预热。** 预热的目的就是"让下次开启不卡", 而低内存模式
        的取舍恰恰相反 —— 宁愿下次多等 ~180ms, 也要把常驻的 ~127MB 省掉。
        两者目标冲突, 所以这里直接跳过 (否则预热完 30 秒又被释放, 白折腾)。
        """
        if self.cfg.get("low_memory_mode", False):
            return False
        if self.overlay is not None and getattr(self.overlay, "_gl_ready", False):
            return True
        if self.capture.latest() is None:
            if tries < 40:
                QTimer.singleShot(250, lambda: self.prewarm_overlay(tries + 1))
            return False

        ov = self._ensure_overlay()
        with_glass = self.glass_on
        ov.enabled = False          # 让 tick() 不会把它拉起来
        ov.show()                   # 这一下就把上下文建好、着色器编译完
        ov.hide()
        if with_glass:
            ov.enabled = True
            ov.show()
        print("[glass] 已预热 (GL 上下文 / 着色器), 首次开启不会再卡")
        return True

    def _schedule_source_check(self, delay=2500):
        QTimer.singleShot(delay, self._check_source_after_start)

    def _check_source_after_start(self):
        if not self.glass_on:
            return
        src = self.hub.get(self.hub.active_name())
        if src is not None and getattr(src, "opening", lambda: False)():
            QTimer.singleShot(1200, self._check_source_after_start)
            return
        msg = self._warn_if_unavailable()
        if msg:
            self.notified.emit(msg)

    def stop_glass(self):
        if not self.glass_on:
            return
        self.glass_on = False
        self._stop_soft_poll()          # 软裁剪只对"开着但隐藏"有意义
        if self.overlay is not None:
            try:
                self.overlay.close_debug()   # 调试窗也是 OpenCV 的独立窗口, 一并收掉
            except Exception:  # noqa: BLE001
                pass
            self.overlay.enabled = False
            self.overlay.hide()
        # 关掉就把设备还回去
        self.hub.stop_active()
        # 调试窗可能借过摄像头 (键盘模式下), 一并还掉
        self.hub.release_camera_if_idle()
        self.glassChanged.emit(False)
        # 低内存模式: 关闭后重新计时 (够久不可见就释放 GL 窗口)
        self._touch_idle_timer()
        print("[glass] 已关闭 (已释放摄像头/串口)")

    def toggle_glass(self):
        if self.glass_on:
            self.stop_glass()
        else:
            self.start_glass()

    def suppress_glass(self, on):
        """临时收起玻璃层 —— 托盘菜单弹出时用, 否则菜单会被全屏置顶窗盖住。"""
        if self.overlay is not None:
            self.overlay.suppress(on)

    def emergency_off(self):
        """紧急关闭 (Ctrl+Alt+Shift+Esc): 立刻关掉玻璃层**并退出程序**。

        为什么要连程序一起退: 只关玻璃层的话, 摄像头再动一下浓度又上去, 屏幕
        会被重新盖住 —— 那就还是"回不去"。退出才能保证屏幕一定恢复正常。
        """
        print("\n[hotkey] 紧急关闭 -> 关玻璃层 + 退出")
        try:
            self.stop_glass()
        except Exception:  # noqa: BLE001
            pass
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.quit()
        except Exception:  # noqa: BLE001
            pass

    def _warn_if_unavailable(self):
        """设备打不开时只提示, **不自动切换模式**。

        角度源由用户在托盘/设置里显式选择; 自动跳走会让人以为程序"自己变了",
        而且换回来还得再猜一次。
        """
        name = self.hub.active_name()
        if name == "manual":
            return None
        if self.hub.device_available(name) is not False:
            return None
        label = "摄像头" if name == "camera" else "串口"
        print("[!] %s不可用 (角度源保持 %s, 未自动切换)" % (label, name))
        return "%s打不开。角度源仍是「%s」, 请在设置里换设备或改成键盘模式。" % (
            label, label)

    # ------------------------------------------------------------ 角度源
    def set_source(self, name):
        # 切源前先关调试窗: 它可能正借着一个即将被停掉的摄像头
        if self.overlay is not None:
            try:
                self.overlay.close_debug()
            except Exception:  # noqa: BLE001
                pass
        self.cfg["source"] = name
        self.hub.set_active(name)
        if self.glass_on:
            self.hub.start_active()
            self._schedule_source_check()
        else:
            self.hub.stop_source(name)
        # 键盘模式专属的热键要跟着角度源注册/注销
        self.register_hotkeys()

    def set_camera(self, index, backend):
        self.cfg["camera_index"] = int(index)
        self.cfg["camera_backend"] = str(backend)
        # 必须把旧对象丢掉: 它还连着旧设备, 改 config 不会让它重新打开
        self.hub.invalidate("camera")
        if self.glass_on and self.hub.active_name() == "camera":
            self.hub.start_active()
            self._schedule_source_check()
            self.notified.emit("正在切换摄像头…")

    def set_serial(self, port, baud=None):
        self.cfg["port"] = str(port)
        if baud is not None:
            self.cfg["baud"] = int(baud)
        self.hub.invalidate("serial")
        if self.glass_on and self.hub.active_name() == "serial":
            self.hub.start_active()

    # ------------------------------------------------------------ 扫描摄像头
    # 扫描要真的去打开设备, 和正在跑的采集线程抢摄像头会失败, 所以先让出去
    def begin_scan(self):
        # 扫描要独占摄像头, 调试窗若正开着会拿不到帧 —— 先收掉
        if self.overlay is not None:
            try:
                self.overlay.close_debug()
            except Exception:  # noqa: BLE001
                pass
        self._scan_was_running = (self.glass_on
                                  and self.hub.active_name() == "camera"
                                  and self.hub.active_running())
        if self._scan_was_running:
            self.hub.stop_source("camera")
        self.hub.invalidate("camera")

    def end_scan(self):
        if self._scan_was_running and self.glass_on:
            self.hub.start_active()
        self._scan_was_running = False

    # ------------------------------------------------------------ 手动浓度
    def set_manual_level(self, v):
        self.control.set_auto(False)
        self.control.set_level(float(v))

    def manual_level(self):
        return self.control.level()

    def is_auto(self):
        return self.control.is_auto()

    def bump_level(self, delta):
        """浓度微调 —— GUI 上那排 −/+ 按钮就是它。"""
        self.control.set_auto(False)
        self.control.set_level(self.control.level() + float(delta))
        return self.control.level()

    # ------------------------------------------------------------ 快捷操作
    # 这些以前是控制台快捷键; 现在快捷方式放在设置窗口里, 控制台只在
    # 键盘手动模式下才响应功能键。
    def camera(self):
        return self.hub.get("camera")

    def calibrate_camera(self):
        cam = self.camera()
        if cam is None or cam.available() is False:
            self.notified.emit("摄像头不可用, 无法标定基准帧")
            return False
        cam.request_calibration()
        return True

    def flip_camera_sign(self):
        cam = self.camera()
        if cam is None:
            self.notified.emit("摄像头角度源还没启动")
            return False
        sign = cam.flip_sign()
        self.cfg["camera_sign"] = sign
        self.save()
        self.notified.emit("已翻转方向 (camera_sign = %+d)" % sign)
        return True

    def toggle_debug_window(self):
        """开关摄像头特征匹配调试窗。

        它要看摄像头画面, 而**键盘模式下摄像头平时是关着的** (不该为了一个
        可能不用的窗口占着独占设备、白吃约 34% 单核)。所以开窗时临时借一下,
        关窗时还回去。
        """
        if self.overlay is None:
            self.notified.emit("先开启玻璃层")
            return False
        if self.overlay._dbg_window_open:
            self.overlay.close_debug()
            self.hub.release_camera_if_idle()
            return True
        if self.hub.active_name() != "camera":
            if self.hub.borrow_camera():
                print("[debug] 为调试窗临时打开摄像头 (角度源仍是 %s)"
                      % self.hub.active_name())
                self.notified.emit("正在打开摄像头, 约 1 秒后出现匹配窗口")
        self.overlay.toggle_debug()
        return True

    # ------------------------------------------------------------ 效果参数
    def apply_effect_settings(self, reload_backdrop=False):
        """改完 config 里的渲染参数后让玻璃层/摄像头源重新读取它们。

        GlassOverlay 和摄像头源在 __init__ 里把参数缓存成了实例属性,
        光改 cfg 不会生效, 要主动同步。
        """
        # 摄像头灵敏度改了要推给正在跑的摄像头源 —— 它在循环里实时读 self.scale,
        # 所以设了就即时生效, 不用重启设备。
        cam = self.hub.get("camera")
        if cam is not None:
            try:
                cam.scale = float(self.cfg.get("camera_scale", cam.scale))
            except Exception:  # noqa: BLE001
                pass
        if self.overlay is None:
            return
        self.overlay.apply_config()
        if reload_backdrop:
            self.overlay.reload_backdrop()

    def set_backdrop(self, path):
        self.cfg["backdrop_path"] = str(path)
        if self.overlay is not None:
            self.overlay.reload_backdrop()

    # ------------------------------------------------------------ 持久化
    def save(self):
        clean = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        # **原子写**: 先写同目录临时文件, 再 os.replace 到目标。直接
        # `open(path,"w")` 覆写的话, 写到一半崩溃/断电会留下**截断的 JSON** ——
        # config 就废了 (main.load_config 里那套"备份 + 重建"的恢复逻辑正是为
        # 这种真实故障准备的)。os.replace 在 NTFS 上是原子的: 要么旧内容, 要么
        # 新内容, 不会出现半截文件。
        # encoding="utf-8" 不写 BOM; 加载端也用 utf-8-sig, 记事本改过也能读。
        tmp = self.cfg_path.with_name(self.cfg_path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())      # 落盘后再换名, 别只到 OS 缓存
            os.replace(tmp, self.cfg_path)
        except Exception:  # noqa: BLE001
            # 退路: 原子写失败 (权限/文件系统不支持) 就退回直接写, 别丢用户改动
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            with open(self.cfg_path, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        print("[config] 已保存 %s" % self.cfg_path)
        return clean

    # ------------------------------------------------------------ 退出
    def shutdown(self):
        self._shutting_down = True
        self._stop_release_poll()
        self._stop_soft_poll()
        try:
            self.release_hotkeys()
        except Exception:  # noqa: BLE001
            pass
        for fn in (self.stop_glass, self.hub.stop_all, self.capture.stop):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                print("[shutdown] %s" % exc)
