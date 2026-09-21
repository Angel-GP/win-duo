"""全屏玻璃 Overlay -- 合并 windowsduo 的 GL 窗口与两个项目的渲染细节。

窗口是"透明输入 + 置顶 + 无边框"的常驻悬浮层, 每 16ms 一次 tick:
    取 level -> 缓动 -> 上传截图纹理 -> 跑 Duo 折叠着色器。

两个必须保留的历史坑 (来自 windowsduo/AGENTS.md, 都踩过):
  1) 必须继承 QOpenGLWidget。继承普通 QWidget 时 initializeGL/paintGL 永远不
     会被调用, 窗口只剩默认底色 (白屏)。
  2) 必须 SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) 把自己排除出捕获,
     否则 mss 会截到自己的上一帧, 反馈几帧后收敛成一片纯色。
"""
import ctypes
import os
import time

import numpy as np
from OpenGL import GL
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtWidgets import QApplication, QFileDialog

from angles.hub import LABELS

import paths

from .capture import frame_bgr
from .shader import FS_DUO, VS

WDA_EXCLUDEFROMCAPTURE = 0x11


def _cv2():
    """按需加载 cv2 并缓存到模块级单例。

    **不要提回模块顶层 `import cv2`。** opencv 的 DLL 有 ~116MB, 而本模块在
    controller 的导入链上 —— 提回顶层等于"进程一起来就扛着 116MB", 哪怕
    用户从不用摄像头调试窗、不设背景兜底。cv2 在本文件只服务四个低频场景:
    背景兜底图、背景图读取、摄像头匹配调试窗、`_resize_fill` (都在用户动作
    或首次建背景时触发, 不在每帧热路径上)。

    线程安全: 所有调用点都在主线程 (Qt 绘制/定时器回调), 不存在并发首导。
    千万别学"绘制回调里 import"的反例 —— 这里第一次 import 发生在首次调用
    `_cv2()` 时, 同样是主线程, 无 import lock 撞车风险。
    """
    global _cv2_mod
    if _cv2_mod is None:
        import cv2 as _m
        _cv2_mod = _m
    return _cv2_mod


_cv2_mod = None

#: 调试窗标题**必须是纯 ASCII**。OpenCV 的 HighGUI 在 Windows 上按本地代码页
#: 解释窗口标题, 中文一定会变成乱码 (试过 "win-duo 特征匹配 (x 关闭)" -> 乱码)。
DEBUG_WINDOW = "win-duo match debug"

# ═══════════════════════════════════════════════════════════════════
# 渲染微调 / 显隐阈值 常量
# ═══════════════════════════════════════════════════════════════════
# 这些参数普通用户几乎不动, 所以**写死在源码里**, 不再进 config.json。
# config.json 只留 refresh_hz / render_fps / max_tilt_deg / eye_dist_h /
# blur_spread / outside_mode / backdrop_path 这几个用户会调的。
SMOOTHING = 0.22          # 浓度缓动系数, 越小越跟手
DARKENING = 0.001         # 吸光: 间隙越大越暗
MAX_TAPS = 32             # 模糊盘式采样数上限
BACKDROP_BLUR = 1.0       # 背景兜底图相对前景的模糊比例
LOCK_AT_CLOSE = False     # 合盖到底 (浓度接近满) 时锁屏
#: 玻璃层显隐的三个阈值 (双阈值 + 最短驻留, 防止死区边缘反复闪)
IDLE_HIDE_BELOW = 0.004   # 浓度低于它 -> 隐藏整个玻璃层 (露出真实桌面)
IDLE_SHOW_ABOVE = 0.02    # 浓度高于它 -> 显示
IDLE_DWELL_SEC = 0.35     # 两次显隐最短间隔


def _resize_fill(img, w, h):
    """等比缩放 + 居中裁剪到 w x h, 铺满且不拉伸、不留黑边。

    背景图必须"铺满整屏"而不是被拉伸变形, 否则背景兜底时会看出几何失真。
    """
    ih, iw = img.shape[:2]
    if iw == 0 or ih == 0:
        return img
    scale = max(w / iw, h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    cv2 = _cv2()
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    x = (nw - w) // 2
    y = (nh - h) // 2
    return resized[y:y + h, x:x + w]


def load_image(path):
    """读图。用 np.fromfile + imdecode 规避中文路径下 cv2.imread 打不开的问题。"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    cv2 = _cv2()
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


class GlassOverlay(QOpenGLWidget):
    def __init__(self, screen, hub, capturer, control, cfg):
        super().__init__()
        self.hub = hub
        self.capturer = capturer
        self.control = control
        self.cfg = cfg

        self.g = 0.0
        self.target = 0.0
        self.enabled = True          # 总开关: 托盘里"关闭玻璃层"时置 False
        self.suppressed = False      # 临时收起 (托盘菜单弹出时, 免得菜单被盖住)
        self._visible = False        # 玻璃层当前是否真的显示着
        self._uploaded_seq = -1
        self._uploaded_size = (1, 1)  # 已上传纹理的尺寸, 与 frame 解耦
        self._last_drawn_g = -1.0    # 上次真正重绘时的浓度/帧号, 用于跳过无谓重绘
        self._last_drawn_seq = -1
        self._last_kick = 0.0
        self._last_print = 0.0
        self._locked = False

        # 渲染参数全部收进 apply_config(), 这样设置窗口改完能就地生效
        self.idle_hide = 0.004
        self.idle_show = 0.02
        self.idle_dwell = 0.35
        self._last_vis_change = 0.0
        self.render_interval = 1.0 / 30.0
        self._last_draw_req = 0.0
        self.apply_config()
        self._backdrop_ready = False
        self._dbg_seq = -1
        self._dbg_window_open = False
        self._dbg_shown = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.setWindowTitle("win-duo glass")
        # 玻璃层是无边框全屏窗, 平时看不到图标; 但 Alt-Tab / 任务视图里会出现
        # 它的条目, 不设就会显示成默认的白色方块。
        try:
            from ui.widgets import make_icon
            self.setWindowIcon(make_icon())
        except Exception:  # noqa: BLE001
            pass

    def apply_config(self):
        """从 cfg 重新读一遍渲染参数。

        __init__ 里这些值被缓存成了实例属性, 所以设置窗口改完 cfg 之后必须
        调一次这里, 否则改动不会生效。
        """
        cfg = self.cfg
        # 用户会调的留在 config.json
        self.refresh_hz = float(cfg.get("refresh_hz", 3))
        self.max_tilt = float(cfg.get("max_tilt_deg", 88.0)) * 3.14159265 / 180.0
        self.eye_h = float(cfg.get("eye_dist_h", 2.0))
        self.spread = float(cfg.get("blur_spread", 0.42))
        # 下面这些是渲染微调/显隐阈值, 写死为源码常量 (见文件顶部)
        self.smoothing = SMOOTHING
        self.dark = DARKENING
        self.max_taps = MAX_TAPS
        self.bg_blur = BACKDROP_BLUR
        self.idle_hide = IDLE_HIDE_BELOW
        self.idle_show = IDLE_SHOW_ABOVE
        self.idle_dwell = IDLE_DWELL_SEC
        fps = float(cfg.get("render_fps", 30))
        self.render_interval = (1.0 / fps) if fps > 0 else 0.0
        self.render_fps = fps
        # 采集频率的**有效上限**: 玻璃层最多每秒重绘 render_fps 次 (paintGL 被
        # render_interval 限速), 所以抓得比这更勤的帧**在上屏前就被下一帧覆盖
        # 掉了** —— 纯属白抓。而每抓一帧都要把 16MB 的桌面 .copy() 一份 (DXGI
        # 缓冲不能直接留用, 见 capture._store), 这份拷贝就是玻璃层显示时 CPU 的
        # 大头。实测 (2560x1600, level=0.5 静止):
        #     抓屏 140/s -> 单核 85%      抓屏 60/s -> 单核 53%      抓屏 30/s -> 34%
        # 而重绘率始终被 render_fps 钉在 ~27/s, 三者肉眼无差别。
        #
        # 取 render_fps 的 **2 倍**留一点相位余量: 保证每个重绘 tick 手上都有一帧
        # 够新的 (正好 1 倍时, tick 和抓屏错相位会偶尔抓空, 重绘掉到 ~20/s)。
        # refresh_hz 仍然照旧驱动 tick 周期 (_tick_ms) —— tick 本身很便宜, 让它
        # 跑快点能把重绘时机卡得更准, 不受这个上限影响。
        # 用户把 refresh_hz 设得比这还低时, 尊重用户 (min)。
        #
        # **render_fps=0 表示"不限速"** (见 README 的参数表), 那种情况下重绘没有
        # 天花板, 抓屏也就不该被压 —— 直接用 refresh_hz, 别拿 2*0 去卡它。
        if self.render_fps > 0:
            self.capture_hz = min(self.refresh_hz,
                                  max(2.0 * self.render_fps, 30.0))
        else:
            self.capture_hz = self.refresh_hz
        self.outside = 0 if str(cfg.get("outside_mode", "black")) == "black" else 1
        self._retune_timer()

    def _tick_ms(self):
        """tick 周期必须跟得上设定的重截频率。

        以前写死 16ms -> 每秒最多 62 次 tick, 所以 refresh_hz 填 137 也只会
        跑到 62 —— 用户当然会问"实际上频率没那么高啊"。
        现在按设定值算, 下限 4ms (防手滑填太大把 CPU 烧掉)。
        """
        want = max(self.refresh_hz, self.render_fps, 60.0)
        return int(max(4, min(16, round(1000.0 / want))))

    def _retune_timer(self):
        """按当前设置重设定时器周期 (apply_config 会调, 改设置就地生效)。"""
        if not hasattr(self, "timer"):
            return
        ms = self._tick_ms()
        if self.timer.interval() != ms and self.timer.isActive():
            self.timer.start(ms)

    def suppress(self, on):
        """临时收起玻璃层 (不是关闭)。

        用途: 托盘右键菜单是普通窗口, 会被全屏置顶的玻璃层盖住 —— 不收起的话
        用户看不见菜单, 只能盲点, 感觉像"点托盘没反应"。
        """
        self.suppressed = bool(on)
        if on:
            if self._visible:
                self._visible = False
                self.hide()
        elif self.enabled and self.g >= self.idle_show:
            self._visible = True
            self._last_vis_change = time.time()
            self.show()
            self.capture_kick_safe()

    def capture_kick_safe(self):
        try:
            self.capturer.kick()
        except Exception:  # noqa: BLE001
            pass

    def set_screen(self, screen):
        """把玻璃层挪到另一块显示器上。截屏区域由 controller 同步改。"""
        self.setGeometry(screen.geometry())
        # 尺寸变了, 之前那帧的尺寸不再对应, 强制重新上传
        self._uploaded_seq = -1
        self._last_drawn_seq = -1
        self._backdrop_ready = False
        if getattr(self, "_gl_ready", False):
            self.update()
        print("[glass] 已移到显示器 %s %dx%d"
              % (screen.name(), screen.geometry().width(),
                 screen.geometry().height()))

    # ------------------------------------------------------------ 窗口生命周期
    def showEvent(self, _ev):
        # 让 _visible 跟着真实可见性走: main 里已经 show() 过一次, 若初值仍为
        # False, 首次 tick 就不会把浓度 0 的玻璃层收起来, 卡顿依旧。
        self._visible = True

        # 坑 2: 把自己从屏幕捕获中排除, 否则截图会抓到上一次的渲染结果
        if not getattr(self, "_no_exclude", False):
            try:
                r = ctypes.windll.user32.SetWindowDisplayAffinity(
                    int(self.winId()), WDA_EXCLUDEFROMCAPTURE)
                if not r:
                    print("[警告] SetWindowDisplayAffinity 失败, 截图可能包含自身")
            except Exception as exc:  # noqa: BLE001
                print("[警告] 显示排除设置异常:", exc)

        # show() 可能被调用多次 (例如选背景图后重新显示), 定时器只能建一次,
        # 否则每显示一次就多一个 16ms 定时器, tick 会被重复触发
        if not hasattr(self, "timer"):
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
        self.timer.start(self._tick_ms())

    def closeEvent(self, ev):
        try:
            self.close_debug()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(ev)

    def shutdown_gl(self):
        """停掉 tick 定时器, 准备销毁窗口 (低内存模式用)。

        **不做 GL 资源清理** —— 上下文随窗口一起销毁, 显存由驱动回收。这里
        只保证定时器不会再回调 (否则窗口销毁后 tick 还会被触发, 访问到半死的
        对象)。`deleteLater` 由调用方 (controller) 负责。
        """
        try:
            if getattr(self, "timer", None) is not None:
                self.timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self._visible = False
        self._gl_ready = False

    # ------------------------------------------------------------ GL
    def initializeGL(self):
        # 打印驱动信息 —— 排查"某类显卡上玻璃层黑屏"时, 这一行能直接说明
        # 拿到的是什么上下文 (版本/厂商/prof ile), 不用再猜。
        try:
            print("[GL] %s | %s | %s | GLSL %s" % (
                GL.glGetString(GL.GL_VERSION).decode("latin-1"),
                GL.glGetString(GL.GL_VENDOR).decode("latin-1"),
                GL.glGetString(GL.GL_RENDERER).decode("latin-1"),
                GL.glGetString(GL.GL_SHADING_LANGUAGE_VERSION).decode("latin-1")))
        except Exception:  # noqa: BLE001
            pass
        print("[GL] initializeGL, context valid =", self.context().isValid(),
              self.context().format().majorVersion(),
              self.context().format().minorVersion())

        self.prog = QOpenGLShaderProgram(self)
        ok_v = self.prog.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex, VS)
        ok_f = self.prog.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, FS_DUO)
        ok_l = self.prog.link()
        if not (ok_v and ok_f and ok_l):
            print("[GL] 着色器编译/链接失败: vs=%s fs=%s link=%s\n%s"
                  % (ok_v, ok_f, ok_l, self.prog.log()))
        else:
            print("[GL] 着色器编译链接 OK")
        self.prog.bind()

        # uniform 位置**在这里查一次就好**。`uniformLocation` 每次都要拿字符串
        # 去驱动里查表, 而 paintGL 每帧要设 9 个 uniform —— 每帧 9 次字符串查表
        # 纯属浪费 (虽然 GL 调用本身不贵, 但没必要)。链接之后位置就不会变了,
        # 缓存进 dict, paintGL 里只做一次字典取值。
        self._uloc = {
            name: self.prog.uniformLocation(name)
            for name in ("uTex", "uBackdrop", "uRes", "uTilt", "uEyeZ",
                         "uSpread", "uDark", "uMaxTaps", "uOutside", "uBgBlur")
        }
        missing = [k for k, v in self._uloc.items() if v < 0]
        if missing:
            print("[GL] 警告: 这些 uniform 没找到 (驱动可能优化掉了): %s" % missing)

        self.cap_tex = self._new_tex(swizzle_bgra=True)   # BGRA 帧, 采样时硬件换通道
        # bd_tex 同样走 swizzle: _upload_backdrop 手上的是 cv2 的 BGR 数据,
        # 灌成 RGBA 字节序再让采样器按 (B,G,R,1) 换回来, 省一次 CPU 换通道。
        self.bd_tex = self._new_tex(swizzle_bgra=True)
        # 背景上传时 alpha 恒为 1 (见 _upload_backdrop), swizzle 通道 4 填 1
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.bd_tex)
        GL.glTexParameteriv(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_SWIZZLE_RGBA,
                            (GL.GL_BLUE, GL.GL_GREEN, GL.GL_RED, GL.GL_ONE))
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

        # **core profile 必须有一个已绑定的 VAO 才能发起 draw call**, 哪怕不用
        # 顶点属性 (我们的全屏三角形由顶点着色器用 gl_VertexID 生成)。空 VAO 就够。
        self.vao = GL.glGenVertexArrays(1)
        if not self.vao:
            print("[GL] 警告: glGenVertexArrays 失败 (core 下会画不出东西)")

        self._gl_ready = True

    @staticmethod
    def _gl_err_where(tag):
        """打印并清掉当前 GL 错误旗标 (调试辅助, 平时无错时零输出)。"""
        err = GL.glGetError()
        if err != 0:
            print("[GL] %s 处 glError=0x%X" % (tag, err))

    @staticmethod
    def _new_tex(swizzle_bgra=False):
        """建一张纹理。

        `swizzle_bgra=True` 时设 **GL_TEXTURE_SWIZZLE_RGBA = (B,G,R,A)**:
        上传 BGRA 数据(内部格式仍是 GL_RGBA8,规范安全),采样时由硬件把
        R/B 换回来。

        为什么有这个开关:DXGI 桌面复制的原生格式就是 BGRA。以前为了用
        GL_RGBA 外部格式,每帧在 CPU 侧把 16MB 的 BGRA 搬成 RGBA
        (`frame_bgra_to_rgba`);swizzle 把这一步挪进纹理采样硬件 —— 上传
        省一次全像素搬运,每帧少分配 16MB。GL_TEXTURE_SWIZZLE_RGBA 是
        OpenGL 3.0 核心特性(本项目就是 3.3),无兼容性问题。
        """
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        if swizzle_bgra:
            # **必须用 glTexParameteriv (数组版), 不能用 glTexParameteri。**
            # swizzle 是 4 个整型的查询/设置, glTexParameteri 的 params 只收
            # 一个 GLint —— PyOpenGL 把 tuple 塞给标量版会按错误的方式传给
            # 驱动, 直接 native 崩溃 (0xC0000409, 实测踩过)。
            # 顺带: GL_TRUE/GL_FALSE 之类是 uint 常量, 而 GL_TEXTURE_SWIZZLE_*
            # 的分量值是普通 GLenum int, 直接传 python int 列表即可。
            GL.glTexParameteriv(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_SWIZZLE_RGBA,
                                (GL.GL_BLUE, GL.GL_GREEN, GL.GL_RED, GL.GL_ALPHA))
            GlassOverlay._gl_err_where("纹理 %s 的 swizzle glTexParameteriv" % tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        return tex

    def paintGL(self):
        self._paint_count = getattr(self, "_paint_count", 0) + 1
        if not getattr(self, "_gl_ready", False):
            return
        dpr = self.devicePixelRatioF()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))

        frame = self.capturer.latest()
        if frame and frame[3] != self._uploaded_seq:
            raw, fw, fh, seq, fmt = frame
            # **cap_tex 上传走 BGRA + swizzle 采样换通道**。注意区分两个问题:
            #   - `GL_BGRA` 作为 glTexImage2D 的**外部格式**不是 3.3 core 的
            #     核心保证 (GL_EXT_bgra),Intel 核显会拒绝 -> 以前因此全黑;
            #   - 外部格式仍可用 GL_RGBA 吗?不行 —— 数据是 BGRA,RGBA 会让
            #     红/蓝互换。真正的解法是 **GL_TEXTURE_SWIZZLE_RGBA**:外部
            #     格式老老实实给 GL_RGBA 按字节灌进去,纹理内部按 (B,G,R,A)
            #     swizzle,采样时硬件换回 R/B —— 见 _new_tex 的说明。
            # 于是每帧的 CPU 换通道 (frame_bgra_to_rgba, 16MB 搬运) 整个删掉,
            # GL 调用本身对任何驱动都规范安全。
            #
            # **fmt 契约**: 上传无条件按 BGRA 字节序灌 (swizzle 负责换通道)。
            # 现存三个后端 (wgc/dda/mss) 都以 "BGRA" 登记, 这里 assert 钉死
            # —— 未来若有人加 RGBA 后端而忘了改这里, 会在开发期立刻炸出来,
            # 而不是上线后整屏红蓝反转还查不到原因。
            assert fmt == "BGRA", "paintGL 假定帧是 BGRA, 实际 fmt=%r" % (fmt,)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, fw, fh, 0,
                            GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, raw)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self._uploaded_seq = seq
            self._uploaded_size = (fw, fh)
            if not self._backdrop_ready:
                self._build_backdrop(frame)

        if self._uploaded_seq == -1:
            GL.glClearColor(0, 0, 0, 1)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
            return

        # 用上传时记下的尺寸, 不要用 frame[]: 换显示器后 capturer 会把缓存的
        # 帧丢掉 (frame 变成 None), 那时 frame[1] 会直接抛 TypeError。
        tex_w, tex_h = self._uploaded_size

        self.prog.bind()
        GL.glViewport(0, 0, w, h)

        loc = self._uloc
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.cap_tex)
        GL.glUniform1i(loc["uTex"], 0)

        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.bd_tex)
        GL.glUniform1i(loc["uBackdrop"], 1)

        GL.glUniform2f(loc["uRes"], float(tex_w), float(tex_h))
        GL.glUniform1f(loc["uTilt"], self.g * self.max_tilt)
        GL.glUniform1f(loc["uEyeZ"], self.eye_h * tex_h)
        GL.glUniform1f(loc["uSpread"], self.spread)
        GL.glUniform1f(loc["uDark"], self.dark)
        GL.glUniform1i(loc["uMaxTaps"], self.max_taps)
        GL.glUniform1i(loc["uOutside"], self.outside)
        GL.glUniform1f(loc["uBgBlur"], self.bg_blur)

        self._draw_quad()

    def _draw_quad(self):
        # core profile: 不用即时模式 (glBegin/glVertex 已被移除)。绑定空 VAO,
        # 画 3 个顶点的全屏三角形 —— 顶点位置和 UV 全由顶点着色器按 gl_VertexID
        # 生成 (见 shader.VS)。
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)

    # ------------------------------------------------------------ 背景纹理
    def _backdrop_path(self):
        p = str(self.cfg.get("backdrop_path", "desk_bg.png"))
        if os.path.isabs(p):
            return p
        # 相对路径按**数据目录**解析 (打包后是 exe 旁边) —— 用户换的背景图是
        # 自己的文件, 不该指望它被打进 exe 里。
        #
        # ⚠️ **不要在函数里 `from paths import ...`。** 这个函数会被
        # `paintGL` -> `_build_backdrop` 调用, 也就是**在 Qt 绘制/定时器回调
        # 的热路径里**; 在其中执行 import 会去抢 Python 的 **import lock**,
        # 而此时采集线程可能正好在首次导入 `render.capture` (它要 import
        # bettercam/comtypes) —— 两边一撞, 进程直接 native 崩溃
        # (0xC0000409, Python 侧抓不到任何异常)。
        # 我为了打包路径把 import 写进这个函数里, 就是这么崩的。
        # `paths` 现在在模块顶部导入, 热路径上没有任何 import。
        return str(paths.data_dir() / p)

    def _fallback_backdrop(self, frame):
        """没有背景图时, 用模糊压暗的桌面兜底。

        用 capture.frame_bgr() 统一通道顺序 —— DXGI 给 RGBA、mss 给 BGRA,
        这里直接按 BGRA 切前三个通道的话, 用 DXGI 时红蓝会反。
        """
        fw, fh = frame[1], frame[2]
        bgr = frame_bgr(frame)
        cv2 = _cv2()
        small = cv2.resize(bgr, (max(1, fw // 8), max(1, fh // 8)),
                           interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), 20)
        up = cv2.resize(small, (fw, fh), interpolation=cv2.INTER_LINEAR)
        return (up * 0.5).astype(np.uint8)

    def _build_backdrop(self, frame):
        """优先用配置里的背景图, 否则用当前桌面的模糊压暗版。"""
        fw, fh = frame[1], frame[2]
        path = self._backdrop_path()
        img = load_image(path) if os.path.exists(path) else None
        if img is not None:
            print("[backdrop] 已加载 %s" % path)
        else:
            if os.path.exists(path):
                print("[backdrop] 无法读取 %s, 回退到模糊桌面" % path)
            img = self._fallback_backdrop(frame)
        self._upload_backdrop(_resize_fill(img, fw, fh))
        self._backdrop_ready = True

    def reload_backdrop(self):
        frame = self.capturer.latest()
        if not frame:
            print("[backdrop] 还没有截图, 稍后再试")
            return
        self._backdrop_ready = False
        self._build_backdrop(frame)

    def pick_backdrop(self):
        """Qt 原生文件选择框 (不用 tkinter, 避免和 Qt 事件循环打架)。

        玻璃层是置顶且不透明输入的全屏窗, 直接弹框会被它盖住, 所以先把窗口藏起来,
        选完再显示。
        """
        self.timer.stop()
        # 记下藏起来之前是否本就该显示 —— 不要无条件 show(): 若此时总开关
        # 关着、或被临时收起 (托盘菜单弹出), 强行 show 会有一帧闪现。
        was_visible = self._visible and self.enabled and not self.suppressed
        self.hide()
        try:
            path, _ = QFileDialog.getOpenFileName(
                None, "选择背景图", "",
                "图片 (*.png *.jpg *.jpeg *.bmp);;所有文件 (*)")
        finally:
            if was_visible:
                self.show()
            self.timer.start(self._tick_ms())
        if not path:
            return
        img = load_image(path)
        if img is None:
            print("\n[backdrop] 读不出这张图: %s" % path)
            return
        frame = self.capturer.latest()
        if not frame:
            return
        self.cfg["backdrop_path"] = path
        self._upload_backdrop(_resize_fill(img, frame[1], frame[2]))
        self._backdrop_ready = True
        print("\n[backdrop] 已切换 %s" % path)

    def _upload_backdrop(self, bgr):
        h, w = bgr.shape[:2]
        # bd_tex 已设 swizzle (B,G,R,1) —— cv2 的 BGR 数据直接按字节灌进去,
        # 采样时硬件换通道, 不再在 CPU 上做 BGR->RGB 搬运。外部格式用 GL_RGBA:
        # 每行末尾补一个 alpha 字节 (值任意, swizzle 会把它弃成常数 1)。
        # 灌数用 GL_RED 指定逐字节平面?不 —— 这里直接把 HxWx3 reshape 成
        # Hx(W*3)x1 一样要拷贝;干脆 ascontiguousarray 补齐成 4 通道, 一次性成本。
        rgba = np.empty((h, w, 4), dtype=np.uint8)
        rgba[:, :, :3] = bgr
        rgba[:, :, 3] = 255
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.bd_tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, w, h, 0,
                        GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, rgba)
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    # ------------------------------------------------------------ 主循环
    def _handle_commands(self):
        for cmd in self.control.pop_commands():
            if cmd == "cycle_source":
                self.hub.next_source()
            elif cmd == "toggle_outside":
                self.outside = 0 if self.outside else 1
                print("\n[渲染] 出界处理 -> %s"
                      % ("纯黑(原版)" if self.outside == 0 else "背景兜底(无黑场)"))
            elif cmd == "toggle_debug":
                self.toggle_debug()
            elif cmd == "pick_backdrop":
                self.pick_backdrop()
            elif cmd == "reload_backdrop":
                self.reload_backdrop()
            elif cmd in ("scale_up", "scale_down", "flip_sign", "calibrate"):
                self._apply_camera_cmd(cmd)

    def _apply_camera_cmd(self, cmd):
        cam = self.hub.get("camera")
        if cam is None:
            print("\n[键] 摄像头角度源尚未启动")
            return
        if cmd == "calibrate":
            cam.request_calibration()
            print("\n[键] 请求标定 (上盖完全展开时按才有意义)")
        elif cmd == "scale_up":
            print("\n[键] camera_scale = %.2f" % cam.adjust_scale(+0.1))
        elif cmd == "scale_down":
            print("\n[键] camera_scale = %.2f" % cam.adjust_scale(-0.1))
        elif cmd == "flip_sign":
            print("\n[键] camera_sign = %+d" % cam.flip_sign())

    def toggle_debug(self):
        """开关摄像头特征匹配调试窗 (设置窗口里也有对应按钮)。"""
        cam = self.hub.get("camera")
        if cam is None:
            print("\n[debug] 摄像头角度源尚未启动, 无法显示匹配窗口")
            return
        if self._dbg_window_open:
            self.close_debug()
        else:
            self._dbg_window_open = True
            self._dbg_seq = -1
            self._dbg_shown = False
            cam.set_debug(True)
            print("\n[debug] 匹配窗口 开 (置顶小窗; 再点一次按钮或按 x 关闭)")

    def close_debug(self):
        """关掉调试窗。

        关键: OpenCV 的 HighGUI 是"消息靠 waitKey 驱动"的 —— destroyWindow()
        只是把销毁请求排进队列, **不接着调 waitKey 就永远不会被处理**, 窗口会
        一直留在屏幕上。这就是"调试窗打开就关不上"的原因。所以销毁后必须继续
        pump 几次 waitKey。
        """
        if not self._dbg_window_open and not self._dbg_shown:
            return
        self._dbg_window_open = False
        self._dbg_shown = False
        cam = self.hub.get("camera")
        if cam is not None:
            cam.set_debug(False)
        cv2 = _cv2()
        for _ in range(8):
            try:
                cv2.destroyWindow(DEBUG_WINDOW)
            except Exception:  # noqa: BLE001
                pass
            try:
                cv2.waitKey(20)
            except Exception:  # noqa: BLE001
                break
        try:
            cv2.destroyAllWindows()
            cv2.waitKey(1)
        except Exception:  # noqa: BLE001
            pass
        print("\n[debug] 匹配窗口 关")

    def _pump_debug_window(self):
        if not self._dbg_window_open:
            return
        cam = self.hub.get("camera")
        if cam is None:
            return

        # 用户自己点了窗口的 X -> 同步状态, 别再往一个已经没了的窗口 imshow
        if self._dbg_shown:
            try:
                visible = cv2.getWindowProperty(DEBUG_WINDOW,
                                                cv2.WND_PROP_VISIBLE)
                if visible < 1:
                    self.close_debug()
                    return
            except Exception:  # noqa: BLE001  窗口已不存在
                self.close_debug()
                return

        img, seq = cam.pop_debug()
        if img is None or seq == self._dbg_seq:
            return
        self._dbg_seq = seq
        cv2 = _cv2()
        try:
            cv2.imshow(DEBUG_WINDOW, img)
            if not self._dbg_shown:
                # 玻璃层是置顶的, 调试窗也得置顶才看得见; 设一次就够
                cv2.setWindowProperty(DEBUG_WINDOW, cv2.WND_PROP_TOPMOST, 1)
                self._dbg_shown = True
            cv2.waitKey(1)
        except Exception as exc:  # noqa: BLE001
            print("[debug] 显示失败:", exc)
            self._dbg_window_open = False
            self._dbg_shown = False

    def _idle_capture(self):
        """告诉采集线程"现在不用抓了"。

        采集线程在 target_hz > 0 时是**自己连续跑**的, 少了这一句它就会一直
        满速抓下去 —— 玻璃层关着也白烧 CPU。
        """
        try:
            self.capturer.target_hz = 0.0
        except Exception:  # noqa: BLE001
            pass

    def tick(self):
        # 总开关: 托盘里"关闭玻璃层"后立刻收起来并停掉截屏, 不再占资源
        if not self.enabled or self.suppressed:
            if self._visible:
                self._visible = False
                self.hide()
            self._idle_capture()
            self._handle_commands()
            return

        self._handle_commands()
        if self.control.quit_flag:
            QApplication.quit()
            return

        level, name, status, detail = self.hub.resolve()
        if level is not None:
            self.target = level
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        self.g += (self.target - self.g) * self.smoothing

        # ---- 空闲隐藏 (带滞回) --------------------------------------------
        # 关键: 玻璃层画的是"每秒只截 refresh_hz 次的桌面快照", 而它是全屏
        # 置顶的。只要它常驻显示, 用户看到的整个桌面就只有几 FPS, 打字、拖窗口
        # 全是卡顿感 —— 这与玻璃效果好不好无关, 是快照刷新率的问题。
        # 所以浓度≈0 时直接 hide(), 让真实桌面露出来: 零开销、零卡顿。
        #
        # **必须用两个阈值 + 最短驻留**: 测角有个死区 (level 要么 0 要么 >=0.03),
        # 在死区边缘 level 会逐帧在 0 和 0.03 之间跳, 于是 g 反复穿越单一阈值,
        # 玻璃层就快速 show/hide —— 表现是屏幕/鼠标光标一闪一闪。
        now = time.time()
        if self._visible:
            if self.g < self.idle_hide:
                self._visible = False
                self._last_vis_change = now
                self.hide()
                print("\n[glass] 浓度≈0, 玻璃层已隐藏 -> 直接看真实桌面, 不再卡顿")
                self._idle_capture()
                self._pump_debug_window()
                self._print_status(name, status, detail)
                return
        else:
            too_soon = (now - self._last_vis_change) < self.idle_dwell
            if self.g < self.idle_show or too_soon:
                self._idle_capture()
                self._pump_debug_window()
                self._print_status(name, status, detail)
                return
            self._visible = True
            self._last_vis_change = now
            self.show()
            self._last_drawn_g = -1.0
            self._last_drawn_seq = -1
            self.capturer.kick()
            print("\n[glass] 玻璃层显示")

        # 截屏频率: 交给采集线程**自己连续跑**, 但只跑到 capture_hz ——
        # 抓得比重绘还勤的帧上屏前就被覆盖了, 白白多做 16MB 拷贝 (见 apply_config
        # 里 capture_hz 的说明)。不再做"画面静止就降频"—— 那个判断曾经把画面
        # 整体冻死 (见 capture.py 顶部说明)。kick() 仍保留, 用于"立刻要一帧"。
        hz = self.capture_hz
        try:
            self.capturer.target_hz = hz
        except Exception:  # noqa: BLE001
            pass
        if hz > 0 and now - self._last_kick >= 1.0 / hz:
            self._last_kick = now
            self.capturer.kick()

        frame = self.capturer.latest()
        seq = frame[3] if frame else -1
        new_frame = seq != self._last_drawn_seq
        level_moved = abs(self.g - self._last_drawn_g) > 0.0005

        # 有新帧或浓度变了就重绘, 并由 render_fps 限速。
        # **别在这里再问一次"内容变了吗"** —— 帧号本身就只在后端给出新帧时
        # 才推进, 多一层平均差过滤会把日常的小变化全吞掉, 画面直接冻死。
        if (level_moved or new_frame) and (now - self._last_draw_req) >= self.render_interval:
            self._last_draw_req = now
            self._last_drawn_g = self.g
            self._last_drawn_seq = seq
            self.update()

        if LOCK_AT_CLOSE and self.g > 0.985 and not self._locked:
            self._locked = True
            ctypes.windll.user32.LockWorkStation()
        if self.g < 0.9:
            self._locked = False

        self._pump_debug_window()
        self._print_status(name, status, detail)

    def _paint_rate(self):
        """每秒真正重绘了几次 —— 用来判断是不是在无谓地满帧空转。"""
        now = time.time()
        cnt = getattr(self, "_paint_count", 0)
        last_t = getattr(self, "_rate_t", None)
        if last_t is None:
            self._rate_t, self._rate_n = now, cnt
            return 0.0
        dt = now - last_t
        if dt < 0.5:
            return getattr(self, "_rate_v", 0.0)
        rate = (cnt - getattr(self, "_rate_n", cnt)) / dt
        self._rate_t, self._rate_n, self._rate_v = now, cnt, rate
        return rate

    def _print_status(self, name, status, detail):
        now = time.time()
        if now - self._last_print < 0.1:
            return
        self._last_print = now

        bits = ["[%s]" % LABELS.get(name, name)]
        if detail:
            if "pitch" in detail:
                bits.append("pitch=%+6.2f" % detail["pitch"])
                bits.append("fold=%5.1f" % detail["fold"])
                bits.append("match=%d" % detail["matches"])
                bits.append("SCALE=%.1f" % detail["scale"])
                bits.append("SIGN=%+d" % detail["sign"])
            if "angle" in detail:
                bits.append("a=%7.2f" % detail["angle"])
                bits.append("%3.0fHz" % detail["fps"])
        bits.append("浓度=%5.1f%%" % (self.g * 100))
        bits.append("%s" % status)
        bits.append("重绘=%.0f/s" % self._paint_rate())
        # 把**真实**截屏速率也打出来: 光有一个"上限"数字会骗人 ——
        # 实际速率还受 tick 周期、以及"桌面没变就降频轮询"影响。
        try:
            bits.append("截屏=%.0f/s" % self.capturer.rate_hz)
        except Exception:  # noqa: BLE001
            pass
        bits.append("出界=%s" % ("黑" if self.outside == 0 else "背景"))
        bits.append("m=换源 c=标定 x=调试 v=出界 b=选背景 +/-=灵敏度")
        print("\r" + "  ".join(bits) + "   ", end="", flush=True)
