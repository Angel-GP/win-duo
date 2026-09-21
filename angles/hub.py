"""角度源调度: 注册 / 启动 / 轮换三个源, 对外只吐一个 level。

合并的关键就在这一层 -- 摄像头、ESP32、键盘三种测法在这里被抹平成同一个
接口, 渲染层完全不知道自己接的是哪一个传感器。
"""
from .serial_source import SerialAngleSource

ORDER = ["camera", "serial", "manual"]

LABELS = {
    "camera": "摄像头",
    "serial": "ESP32串口",
    "manual": "键盘手动",
}


class SourceHub:
    def __init__(self, cfg, control, autostart=True):
        """autostart=False 时不立刻打开设备 —— 托盘模式下玻璃层默认关着,
        没必要一开机就把摄像头占住、白白烧 CPU。等用户真的开启玻璃层再 start_active()。
        """
        self.cfg = cfg
        self.control = control
        self._sources = {}
        self._started = set()

        name = cfg.get("source", "camera")
        if name not in ORDER:
            print("[hub] 未知角度源 %r, 回退到 camera" % (name,))
            name = "camera"
        self.active = name
        # manual 源就是键盘线程, 不需要也不应该走"跟随传感器"分支
        self.control.set_auto(name != "manual")
        # 功能键只在键盘手动模式下生效
        self.control.set_enabled(name == "manual")
        if autostart:
            self._ensure(name)

    # ---------- 内部 ----------
    def _build(self, name):
        if name == "camera":
            # **懒导入** (不要提回模块顶层)。angles.camera 顶层就 import cv2,
            # 而 opencv 的 DLL 有 ~116MB —— 提到顶层的话, 只要 import hub
            # (串口/键盘源也逃不掉) 就把它加载进内存。摄像头源是唯一真正的
            # cv2 用户, 放到 _build 这个实际要用它的点上, 串口/键盘进程
            # 一辈子不碰 cv2。首次导入是主线程同步做的 (几百 ms), 不在采集
            # 线程热路径上, 也没有 import lock 撞车问题 (见 overlay 热路径
            # 注释的反例 —— 那是"绘制回调里 import", 这里是"启动源时 import")。
            from .camera import CameraAngleSource
            return CameraAngleSource(self.cfg)
        if name == "serial":
            return SerialAngleSource(self.cfg)
        return None

    def _ensure(self, name):
        """构造并按需启动某个源。"""
        if name == "manual":
            return self.control
        if name not in self._sources:
            self._sources[name] = self._build(name)
        if name not in self._started:
            self._sources[name].start()
            self._started.add(name)
        return self._sources[name]

    def get(self, name):
        return self._sources.get(name)

    def active_name(self):
        return self.active

    def active_label(self):
        return LABELS.get(self.active, self.active)

    def set_active(self, name):
        if name not in ORDER:
            return
        previous = self.active
        self.active = name
        # **把上一个源停掉。** 以前只启动新源、不停旧源, 于是从摄像头切到键盘
        # 之后摄像头还在后台跑着 —— 占着独占设备、白吃约 34% 单核, 而且键盘
        # 模式下根本不需要它。
        if previous != name:
            self.stop_source(previous)
        self._ensure(name)
        self.control.set_auto(name != "manual")
        self.control.set_enabled(name == "manual")
        print("\n[hub] 角度源 -> %s (%s)" % (name, self.active_label()))

    # ---------- 临时借用摄像头 ----------
    # 键盘模式下摄像头是关着的。但"匹配调试窗"要看摄像头画面, 所以开窗时
    # 临时借一下, 关窗时还回去 —— 而不是让它在键盘模式下常驻。
    def borrow_camera(self):
        """临时打开摄像头。返回 True 表示这次是它开的 (需要还)。"""
        if self.active == "camera":
            return False
        already = "camera" in self._started
        self._ensure("camera")
        return not already

    def camera_running(self):
        return "camera" in self._started

    def release_camera_if_idle(self):
        """摄像头不是当前角度源就关掉。"""
        if self.active != "camera" and "camera" in self._started:
            self.stop_source("camera")
            print("[camera] 已归还 (当前角度源是 %s)" % self.active)

    def next_source(self):
        idx = ORDER.index(self.active) if self.active in ORDER else 0
        self.set_active(ORDER[(idx + 1) % len(ORDER)])

    def start_active(self):
        """确保当前角度源已启动 (懒启动模式用)。"""
        return self._ensure(self.active)

    def active_running(self):
        return self.active == "manual" or self.active in self._started

    def invalidate(self, name):
        """丢掉某个源的对象, 但**不**启动它 —— 下次真要用时按新配置重建。

        改了摄像头 index / 后端 / 串口之后调用。配置是同一个 dict 对象,
        所以重建时自动带上新参数。
        """
        src = self._sources.pop(name, None)
        self._started.discard(name)
        if src is not None:
            try:
                src.stop()
            except Exception:  # noqa: BLE001
                pass

    def stop_source(self, name):
        """停掉某个源 (释放摄像头/串口), 但保留对象以便再启动。"""
        if name == "manual":
            return
        src = self._sources.get(name)
        if src is None or name not in self._started:
            return
        try:
            src.stop()
        except Exception:  # noqa: BLE001
            pass
        self._started.discard(name)

    def stop_active(self):
        self.stop_source(self.active)

    def device_available(self, name):
        """某个源当前是否真的可用 (摄像头打不开时返回 False)。"""
        src = self._sources.get(name)
        if src is None:
            return None
        fn = getattr(src, "available", None)
        return fn() if callable(fn) else True

    # ---------- 对外 ----------
    def resolve(self):
        """返回 (level, 源名, 状态, detail)。level 为 None 表示暂无数据。"""
        name = self.active
        if name == "manual" or not self.control.is_auto():
            return (self.control.level(), "manual",
                    self.control.status(), {})
        src = self._sources.get(name)
        if src is None:
            return None, name, "未启动", {}
        return src.level(), name, src.status(), dict(src.detail)

    def stop_all(self):
        for src in self._sources.values():
            try:
                src.stop()
            except Exception:  # noqa: BLE001
                pass
        self._sources.clear()
        self._started.clear()
        # manual 源 (KeyControl) 不在 _sources 里, 也要停 —— 否则它的键盘线程
        # 只能靠 daemon 随进程回收, 退出路径不干净。
        try:
            self.control.stop()
        except Exception:  # noqa: BLE001
            pass
