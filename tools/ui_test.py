"""验证 GUI 逻辑 —— 不需要人点鼠标。

做法: 真的把 SettingsPanel 建出来, 然后**直接改控件的值**。
控件信号会照常触发面板的处理函数, 所以这测的是真实链路:
    控件改动 -> cfg 更新 -> 玻璃层沿用新参数 -> 落盘

为了避免弄乱你自己的配置, 全程用一份临时 config 副本。

用法:
    .venv\\Scripts\\python.exe tools\\ui_test.py
"""
import atexit
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 退出时一定要收掉的东西。测试中途抛异常时, 后面那句 panel.shutdown() 会被
#: 跳过, 于是 CameraScanThread 还在跑 -> Qt abort (0xC0000409), **把真正的报错
#: 掩盖成崩溃码**。注册到 atexit 就不会了。
_cleanup_hooks = []


@atexit.register
def _cleanup():
    for fn in _cleanup_hooks:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def main():
    from PyQt6.QtWidgets import QApplication

    import main as app_main
    from ui import AppController, SettingsPanel, autostart, monitors

    app_main.make_qsurface_format()
    app = QApplication([sys.argv[0]])

    failures = []

    def check(label, cond, extra=""):
        print("  [%s] %-46s %s" % ("OK" if cond else "FAIL", label, extra))
        if not cond:
            failures.append(label)

    # ---------- 临时配置副本 ----------
    tmpdir = Path(tempfile.mkdtemp(prefix="windo-ui-"))
    cfg_path = tmpdir / "config.json"
    shutil.copyfile(ROOT / "config.json", cfg_path)
    cfg = app_main.load_config(cfg_path)

    # ---------- 显示器 ----------
    print("显示器:")
    for i, s in enumerate(monitors.screens()):
        print("    [%d] %s" % (i, monitors.label(s, i)))
    check("至少识别到一块显示器", len(monitors.screens()) >= 1,
          "%d 块" % len(monitors.screens()))
    check("region 是物理像素", monitors.region_for(monitors.screens()[0])["width"] > 0)

    # ---------- 控制器 ----------
    print("\n控制器:")
    ctl = AppController(cfg, cfg_path)
    _cleanup_hooks.append(ctl.shutdown)
    check("玻璃层默认是关的", ctl.glass_on is False)
    # 别断言 active_running() —— 键盘源的定义就是"一直在跑"。真正要保证的是
    # **昂贵的那个设备**没有在启动时被提前打开。
    check("启动时没有提前打开摄像头", ctl.hub.camera_running() is False)

    # 真实配置的指纹: 全程都不许变。曾经因为"填充控件时信号已接上"把真实
    # config.json 写成了一堆极值, 这条就是防它再发生。
    real_cfg = ROOT / "config.json"
    real_before = real_cfg.read_bytes()

    panel = SettingsPanel(ctl)
    _cleanup_hooks.append(panel.shutdown)
    check("设置窗口能建出来", panel is not None)
    check("建窗口没动真实 config.json", real_cfg.read_bytes() == real_before)

    snap = {k: cfg[k] for k in ("max_tilt_deg", "eye_dist_h", "blur_spread",
                                "refresh_hz")}
    panel.refresh_all()
    check("refresh_all 不会改动配置值",
          all(abs(cfg[k] - v) < 1e-9 for k, v in snap.items()), str(snap))
    check("刷新后真实 config.json 仍未变", real_cfg.read_bytes() == real_before)

    ctl.start_glass()
    app.processEvents()
    check("开启玻璃层后 glass_on=True", ctl.glass_on is True)
    check("玻璃层窗口已创建", ctl.overlay is not None)
    check("角度源已被启动", ctl.hub.active_running() is True,
          ctl.hub.active_name())

    # ---------- 控件驱动 ----------
    print("\n设置窗口改参数:")
    check("高级设置默认是收起的", panel.adv_body.isHidden() is True)
    panel._toggle_advanced()
    check("点开后高级设置可见", panel.adv_body.isHidden() is False)
    check("再点一次又收起", (panel._toggle_advanced() or panel.adv_body.isHidden())
          is True)
    panel._toggle_advanced()

    def num(key):
        return panel._inputs[key]

    before = cfg.get("max_tilt_deg")
    num("max_tilt_deg").setValue(70)
    check("最大转角写进 cfg", abs(cfg["max_tilt_deg"] - 70.0) < 1e-6,
          "%s -> %s" % (before, cfg["max_tilt_deg"]))
    check("玻璃层已重新读取参数",
          abs(ctl.overlay.max_tilt - 70.0 * 3.14159265 / 180.0) < 1e-6,
          "%.4f rad" % ctl.overlay.max_tilt)

    panel.cmb_outside.setCurrentIndex(1)
    check("出界模式切到 backdrop", cfg["outside_mode"] == "backdrop",
          cfg["outside_mode"])

    num("refresh_hz").setValue(5)
    check("重截频率写进 cfg", abs(cfg["refresh_hz"] - 5.0) < 1e-6,
          str(cfg["refresh_hz"]))

    num("eye_dist_h").setValue(3.0)
    check("眼距小数正确", abs(cfg["eye_dist_h"] - 3.0) < 1e-6,
          str(cfg["eye_dist_h"]))

    panel.edt_port.setText("COM9")
    panel.edt_port.editingFinished.emit()
    check("串口写进 cfg", cfg["port"] == "COM9", cfg["port"])

    panel.sw_autocal.setChecked(False)
    check("自动标定写进 cfg", cfg["autocal_on_glass_open"] is False)
    panel.sw_autoglass.setChecked(True)
    check("自动启动写进 cfg", cfg["autostart_glass"] is True)

    # 低内存模式: 只验证**逻辑**。
    #
    # ⚠️ 两个坑, 都踩过:
    #  1. **不要在这里调 `ctl.stop_glass()`** —— 本测试后面还有几段依赖"玻璃层
    #     仍开着"的用例 (调试窗那段要借摄像头), 提前 stop 会把状态机搞乱。
    #  2. **开了这个开关就会给 controller 起一个真实的周期定时器**。放在测试
    #     中段的话它会真的触发, 把正在用的 GL 窗口拆掉 -> 原生崩溃
    #     (`0xC0000005` / `0xC0000374`)。所以把阈值临时设成很大, 验证完立刻
    #     关掉开关 (会停掉定时器)。
    check("低内存模式是个布尔配置项",
          isinstance(cfg.get("low_memory_mode"), bool),
          str(cfg.get("low_memory_mode")))
    saved_interval = ctl.IDLE_RELEASE_SEC
    ctl.IDLE_RELEASE_SEC = 99999.0        # 别让它在测试里真的触发
    saved_lowmem = cfg.get("low_memory_mode")
    try:
        panel.sw_lowmem.setChecked(True)
        check("低内存模式写进 cfg", cfg.get("low_memory_mode") is True)
        check("开启后周期检查已启动",
              ctl._release_timer is not None and ctl._release_timer.isActive())
        panel.sw_lowmem.setChecked(False)
        check("关闭后周期检查已停",
              not (ctl._release_timer and ctl._release_timer.isActive()))
    finally:
        ctl.IDLE_RELEASE_SEC = saved_interval
        # **还原用户原来的值** —— 测试拷的是真实 config.json, 不能把用户的
        # 设置改掉 (否则下一次跑时"默认值"就变了)。
        panel.sw_lowmem.setChecked(bool(saved_lowmem))

    # 构造时就要按 cfg 把模式应用上 —— 早先只在"拨开关"那一刻起计时器, 用户
    # 开了开关重启后配置虽是 true 却没人应用 (表现就是"开了没生效")。
    ctl2 = AppController(dict(cfg, low_memory_mode=True), tmpdir / "c2.json")
    check("低内存模式: 启动时会按配置应用（不只是拨开关时才生效）",
          ctl2._release_timer is not None and ctl2._release_timer.isActive())
    # 判据必须是"窗口此刻在不在屏幕上", 不能是 glass_on —— 默认
    # autostart_glass=true 时玻璃层是"开着待命"的, glass_on 恒为 True, 光看它
    # 会永远不释放 (那 127MB 就白占着)。
    check("释放判据看的是窗口可见性, 不是 glass_on",
          ctl2._overlay_visible() is False and ctl2.overlay is None,
          "没开玻璃层时应判定为不可见")
    ctl2.shutdown()

    # 浓度是纯键盘输入的百分比框
    panel.num_level.edit.setText("40")
    panel.num_level.edit.editingFinished.emit()
    check("键盘输入 40 -> 浓度 40%", abs(ctl.manual_level() - 0.40) < 1e-6,
          "%.2f" % ctl.manual_level())
    panel.num_level.edit.setText("250")
    panel.num_level.edit.editingFinished.emit()
    check("超范围输入被夹到 100", panel.num_level.value() == 100,
          str(panel.num_level.value()))
    check("夹紧后的值也生效了", abs(ctl.manual_level() - 1.0) < 1e-6,
          "%.2f" % ctl.manual_level())

    # 小数项
    num("eye_dist_h").edit.setText("3.5")
    num("eye_dist_h").edit.editingFinished.emit()
    check("小数项键盘输入 3.5", abs(cfg["eye_dist_h"] - 3.5) < 1e-6,
          str(cfg["eye_dist_h"]))
    num("eye_dist_h").edit.setText("99")          # 上限 6.0
    num("eye_dist_h").edit.editingFinished.emit()
    check("小数项超范围夹到 6.0", abs(cfg["eye_dist_h"] - 6.0) < 1e-6,
          str(cfg["eye_dist_h"]))

    from PyQt6.QtWidgets import QAbstractButton
    btn_texts = [b.text() for b in panel.findChildren(QAbstractButton)]
    pct_btns = [t for t in btn_texts if "%" in t and t.strip() != "%"]
    check("百分比快捷按钮已删除", not pct_btns, str(pct_btns))

    from ui.widgets import CaptionLabel as _Cap
    # 必须先真正走一遍布局再量宽度 —— 没显示过的控件 width() 是默认值 (640),
    # 量出来的数字没有意义。之前"单位标签被拉伸"就是这么漏过去的。
    panel.show()
    app.processEvents()
    units = panel.num_level.findChildren(_Cap)
    check("单位标签紧贴输入框（没被拉伸）",
          bool(units) and units[0].width() < 40,
          "/".join("%s=%dpx" % (u.text(), u.width()) for u in units))

    # 除浓度外都要标出允许范围 (输入框看不出能填多少, 超范围会被静默夹紧)
    tails = {}
    for key, box in panel._inputs.items():
        tails[key] = "".join(l.text() for l in box.findChildren(_Cap))
    check("高级参数都标了数值范围",
          all("~" in v for v in tails.values()), str(tails))
    check("浓度不标范围（百分比本身就清楚）",
          "~" not in "".join(l.text() for l in panel.num_level.findChildren(_Cap)),
          "".join(l.text() for l in panel.num_level.findChildren(_Cap)))
    # 重截频率的上限 = 显示器刷新率, **固定值**, 不做动态测量
    hz = ctl.screen_hz()
    check("重截频率上限 = 显示器刷新率",
          int(panel._inputs["refresh_hz"]._hi) == int(round(hz)),
          "上限 %g Hz (屏 %.0f Hz)" % (panel._inputs["refresh_hz"]._hi, hz))
    check("上限是个稳定的大值（不是 18~37 那种实测值）", hz >= 60,
          "%.0f Hz" % hz)
    check("上限不随负载浮动（同一秒内两次取值相同）",
          abs(ctl.capture.max_hz() - ctl.capture.max_hz()) < 1e-9,
          "%.0f Hz" % ctl.capture.max_hz())
    panel.hide()

    from render.overlay import DEBUG_WINDOW
    check("调试窗标题是纯 ASCII（否则 OpenCV 会乱码）",
          DEBUG_WINDOW.isascii(), DEBUG_WINDOW)

    import ui.widgets as _w
    check("界面用的是 Fluent 组件" if _w.FLUENT else "界面走普通 Qt 回退",
          True, "FLUENT=%s  无滑块/无 SpinBox=%s"
          % (_w.FLUENT, not hasattr(_w, "Slider") and not hasattr(_w, "SpinBox")))
    check("数值框是 NumberField (只能键盘输入)",
          type(panel.num_level).__name__ == "NumberField")

    # ---------- 快捷键门控 & 禁止自动换模式 ----------
    print("\n快捷键门控与模式锁定:")
    ctl.set_source("camera")
    check("摄像头模式下功能键停用", ctl.control._enabled is False)
    ctl.set_source("manual")
    check("键盘模式下功能键启用", ctl.control._enabled is True)
    ctl.set_source("serial")
    check("串口模式下功能键停用", ctl.control._enabled is False)

    # 把设备可用性强制成 False, 验证"打不开也不许自己换模式"
    ctl.set_source("camera")
    _orig_avail = ctl.hub.device_available
    ctl.hub.device_available = lambda name: False if name == "camera" else _orig_avail(name)
    ctl.set_source("camera")
    check("设备不可用时不自动切换角度源",
          ctl.hub.active_name() == "camera", ctl.hub.active_name())
    ctl.start_glass()
    check("开玻璃层时也不自动切换",
          ctl.hub.active_name() == "camera", ctl.hub.active_name())
    ctl.hub.device_available = _orig_avail
    ctl.set_source("camera")

    # ---------- 开关耗时 & 滞回 ----------
    print("\n开启玻璃层不卡顿 & 浓度抖动不闪烁:")

    # 死区边界必须**连续**: 老代码在 level 刚过 deadzone 时直接把控制量从 0
    # 跳到 deadzone (0.03), 边缘抖动时浓度跟着跳, 玻璃层就反复显隐 -> 光标闪。
    from angles.camera import angle_to_level
    p_dead = 0.03 * 180.0 / 1.1          # level 恰好等于 deadzone 时的 pitch
    l_after = angle_to_level(p_dead + 0.02, 1.1, -1, 0.03)[0]
    check("刚过死区时浓度接近 0（无硬跳变）", l_after < 0.002,
          "%.5f (修之前是 0.030)" % l_after)

    ctl.stop_glass()
    app.processEvents()
    # **先关掉低内存模式再测预热。** 用户可能已经把那个开关打开了 (测试拷的是
    # 真实 config.json), 而低内存模式下 `prewarm_overlay()` 是**故意**返回
    # False 的 (预热和它的目标相反)。不关掉的话这条断言会误报。
    # 还要**把周期定时器也停掉**: 光改 cfg 不会停掉已经在跑的定时器, 它会在
    # 这个测试期间把窗口释放掉, 让下一条计时断言变得不可靠。
    ctl.cfg["low_memory_mode"] = False
    ctl._stop_release_poll()
    ok = ctl.prewarm_overlay()
    check("预热成功（GL 上下文/着色器已建好）",
          bool(ok) and ctl.overlay is not None
          and getattr(ctl.overlay, "_gl_ready", False) is True)

    t0 = time.perf_counter()
    ctl.start_glass()
    dt = time.perf_counter() - t0
    # 阈值给 300ms。预热的价值是"不卡那 ~680ms 的首次建上下文", 而预热过之后
    # `start_glass` 只剩 `overlay.show()` —— 本机实测 2~70ms, 但整机负载高时
    # 偶发到 ~180ms。卡在 100ms 会让测试假红 (实测在负载下反复失败)。
    # 用 300ms 既远离 680ms 的真实退化, 又不会被负载波动误判。
    check("预热后开启玻璃层几乎无耗时 (<300ms)", dt < 0.30,
          "%.0f ms" % (dt * 1000))

    ov = ctl.overlay
    check("玻璃层已创建", ov is not None)
    if ov is not None:
        # 模拟测角死区边缘: level 在 0 和 0.031 之间逐帧跳
        ctl.control.set_auto(False)
        flips, prev = 0, ov._visible
        for i in range(40):
            ctl.control.set_level(0.0 if i % 2 else 0.031)
            ov.tick()
            if ov._visible != prev:
                flips += 1
                prev = ov._visible
        check("浓度在死区边缘抖动时不会反复显隐", flips <= 1,
              "翻转 %d 次" % flips)

        # 但浓度持续上去时必须正常显示, 别把功能修没了
        for _ in range(30):
            ctl.control.set_level(0.5)
            ov.tick()
        check("浓度稳定后正常显示", ov._visible is True)

        # 限速: 浓度连续变化时重绘不能超过 render_fps (全屏重绘太密会让
        # 叠在上面的鼠标光标闪)
        ctl.control.set_level(0.2)
        p0 = getattr(ov, "_paint_count", 0)
        t0 = time.perf_counter()
        for i in range(60):
            ctl.control.set_level(0.2 + 0.7 * i / 60)
            app.processEvents()
            time.sleep(1 / 60)
        wall = time.perf_counter() - t0
        rate = (getattr(ov, "_paint_count", 0) - p0) / max(wall, 1e-6)
        check("浓度连续变化时重绘被限速 (<=35/s)", rate <= 35.0,
              "%.0f/s" % rate)

        for _ in range(60):
            ctl.control.set_level(0.0)
            ov.tick()
        check("浓度归零后正常隐藏", ov._visible is False)

        # **浓度不动、只有桌面内容在变时也必须重绘。**
        # 这是之前漏掉的场景: 所有测试都只动浓度, 不动桌面 —— 于是"帧号推进
        # 却被判成没变"这个 bug 溜了过去, 用户看到的是"画面直接冻住"。
        # 这里**自己钉死 refresh_hz**, 不依赖用户 config 里的值 (用户可能设成
        # 5Hz, 那时 3 次/秒重绘是正确的, 断言会误报)。
        ctl.cfg["refresh_hz"] = 60.0
        ov.apply_config()
        ctl.control.set_level(0.5)
        for _ in range(30):
            ov.tick()
        from PyQt6.QtCore import Qt as _Qt
        from PyQt6.QtWidgets import QWidget as _QWidget
        tw = _QWidget()
        tw.setWindowFlags(_Qt.WindowType.FramelessWindowHint
                          | _Qt.WindowType.WindowStaysOnTopHint)
        tw.setGeometry(0, 0, 500, 400)
        tw.show()
        app.processEvents()
        time.sleep(0.4)                 # 等玻璃层真的显示出来
        p1 = getattr(ov, "_paint_count", 0)
        q1 = ctl.capture.pumps
        t0 = time.perf_counter()
        i = 0
        # 采样窗口 2.5 秒 (原来 1.5): 这个用例判的是**重绘率**, 窗口太短时
        # 起止两个渲染周期的相位差就能把结果拉偏 ±30%。实测 1.5 秒窗口在
        # 负载下会在 8~16/s 之间跳, 而阈值是 10/s —— 假红。
        # 另外 `time.sleep(1/120)` 在 Windows 上实际最少睡 ~15.6ms, 所以循环
        # 频率约 64Hz, 而 render_interval 是 33ms —— 采样点必须比闸门密,
        # 这里用 sleep(0) + 主动让出, 让轮询跟得上。
        while time.perf_counter() - t0 < 2.5:
            tw.setStyleSheet("background: %s;"
                             % ("#ff0000" if i % 2 else "#00ff00"))
            app.processEvents()
            ov.tick()
            i += 1
            time.sleep(0.004)
        wall = time.perf_counter() - t0
        p2 = getattr(ov, "_paint_count", 0)
        q2 = ctl.capture.pumps
        tw.close()
        app.processEvents()
        rate = (p2 - p1) / wall
        print("      桌面在变: 抓屏 %.0f/s, 重绘 %.0f/s (设定 refresh_hz=%.0f)"
              % ((q2 - q1) / wall, rate, ctl.cfg.get("refresh_hz", 0)))
        check("浓度不动但桌面在变时仍然重绘", rate >= 10.0,
              "重绘 %.0f/s = %d 次 / %.2fs" % (rate, p2 - p1, wall))

    # ---------- 截屏必须按时推进帧号（不能有"内容没变就不推进"的过滤） ----------
    # 这条是**回归防线**: 曾经加过一个全图平均差的静止检测 (阈值 1.5), 而日常
    # 操作只影响几万像素、摊到 400 万像素上平均差只有 0.2~0.3 —— 于是帧号永不
    # 推进, 玻璃层停在第一帧, 用户报"直接变成静态的了, 帧率似乎为 0"。
    print("\n截屏要按时推进帧号:")
    cap = ctl.capture
    s0, q0 = cap.latest()[3], cap.pumps
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.5:
        app.processEvents()
        time.sleep(0.02)
    wall = time.perf_counter() - t0
    s1, q1 = cap.latest()[3], cap.pumps
    print("      抓屏 %.0f/s, 帧号推进 %.0f/s (设定 refresh_hz=%.0f)"
          % ((q1 - q0) / wall, (s1 - s0) / wall, ctl.cfg.get("refresh_hz", 0)))
    check("桌面上只有小变化时帧号也必须推进", (s1 - s0) / wall >= 5.0,
          "%.0f/s" % ((s1 - s0) / wall))
    check("没有 changed()/suggested_hz() 这类内容过滤",
          not hasattr(cap, "suggested_hz") and not hasattr(cap, "changed"))

    # ---------- GUI 里要列出快捷键 ----------
    print("\nGUI 显示的快捷键:")
    original_source = ctl.hub.active_name()
    ctl.set_source("manual")
    panel._refresh_hotkeys()
    txt = panel.lbl_keys.text()
    for line in txt.splitlines():
        print("      " + line)
    check("键盘模式下列出了浓度快捷键", "浓度 +5%" in txt)
    check("键盘模式下列出了调试窗", "调试" in txt)
    check("键盘模式下不列摄像头专有键",
          not any(x in txt for x in ("标定", "翻转")))
    check("列出了紧急关闭", "紧急关闭" in txt)
    ctl.set_source("camera")
    panel._refresh_hotkeys()
    cam_txt = panel.lbl_keys.text()
    check("摄像头模式下只剩紧急关闭",
          "紧急关闭" in cam_txt and "浓度 +5%" not in cam_txt
          and "开关玻璃层" not in cam_txt, cam_txt.replace("\n", " | "))
    # 还原成 config 里的原始源(camera), 否则后面"关闭后角度源已停止"会误报
    ctl.set_source(original_source)

    # ---------- 设置窗口的托盘化行为 ----------
    # **不要在这里 show()。** showEvent 里有 `QTimer.singleShot(150,
    # start_scan)`, 一 show 就会去扫描摄像头; 而 open_camera 最坏要 20 秒
    # (逐个后端试 + 冻结帧检测), 测试结束时它还没跑完 -> Qt abort
    # (0xC0000409, "QThread: Destroyed while thread is still running")。
    # 试过用 `WA_DontShowOnScreen` 规避 —— **没用**, 它只让窗口不渲染,
    # showEvent 照常触发、扫描照常启动 (实测确认)。
    #
    # 这里验证的是**窗口状态逻辑**, 所以直接查代码行为, 不真去显示窗口:
    # 只要 changeEvent 里"最小化 -> hide()"这条分支还在, 就够了。
    print("\n设置窗口: 最小化收托盘 / 点 X 直接退出:")
    import inspect as _inspect
    src_change = _inspect.getsource(type(panel).changeEvent)
    check("最小化会收进托盘（changeEvent 里有 hide）",
          "WindowStateChange" in src_change and "isMinimized" in src_change
          and "hide()" in src_change)
    src_show = _inspect.getsource(type(panel).showEvent)
    check("从托盘唤出会清掉最小化状态",
          "isMinimized" in src_show and "WindowMinimized" in src_show)

    # 点 X = 真退出。必须调用 app.quit(), 不能只是 ignore/hide ——
    # 只 accept() 不 quit 的话窗口没了、事件循环还在跑 (setQuitOnLastWindowClosed
    # 是 False), 程序变成"看不见也没托盘"的僵尸进程, 比原来更糟。
    src_close = _inspect.getsource(type(panel).closeEvent)
    check("点 X 会真的退出程序（closeEvent 调 app.quit）",
          "accept()" in src_close and "quit()" in src_close,
          "不能是 ignore + hide")
    check("点 X 不再只是收回托盘（没有 ev.ignore）",
          "ignore()" not in src_close)

    # ---------- 键盘模式下不该占着摄像头 ----------
    print("\n键盘模式下的摄像头:")
    ctl.set_source("camera")
    app.processEvents()
    check("摄像头模式下摄像头在跑", ctl.hub.camera_running() is True)
    ctl.set_source("manual")
    app.processEvents()
    check("切到键盘后摄像头已停（旧源要停掉）",
          ctl.hub.camera_running() is False,
          "camera_running=%s" % ctl.hub.camera_running())
    check("键盘模式下 active 是 manual", ctl.hub.active_name() == "manual")

    kbd = [v for _, v in ctl.hotkey_lines()]
    check("键盘模式下列出了浓度键", any("+5%" in v for v in kbd))
    check("键盘模式下**没有**标定键（键盘模式没摄像头可标定）",
          not any("标定" in v for v in kbd), str(kbd))
    check("键盘模式下**没有**翻转方向键", not any("翻转" in v for v in kbd))
    check("键盘模式下列出了调试窗键", any("调试" in v for v in kbd))

    # 开调试窗时才临时打开摄像头
    ctl.toggle_debug_window()
    app.processEvents()
    check("开调试窗时临时借用了摄像头", ctl.hub.camera_running() is True)
    ctl.toggle_debug_window()
    app.processEvents()
    check("关调试窗后摄像头又还回去了", ctl.hub.camera_running() is False)

    cam = None
    ctl.set_source("camera")
    app.processEvents()
    camkeys = [v for _, v in ctl.hotkey_lines()]
    check("摄像头模式下只剩紧急关闭",
          len(camkeys) == 1 and "紧急关闭" in camkeys[0], str(camkeys))
    check("摄像头模式下没有浓度键（会和自动跟踪打架）",
          not any("+5%" in v for v in camkeys))
    # 还原成 config 里的原始源, 否则后面"关闭后角度源已停止"会误报
    ctl.set_source(original_source)
    panel._refresh_hotkeys()

    # ---------- 分辨率/显示器切换 ----------
    print("\n显示器切换:")
    screens = monitors.screens()
    if len(screens) >= 2:
        panel.cmb_screen.setCurrentIndex(1)
        check("切到第二块屏", ctl.cfg["screen_index"] == 1,
              ctl.cfg.get("screen_name", ""))
    else:
        print("  [--] 只有一块显示器, 跳过切换测试")

    # ---------- 开机自启 ----------
    print("\n开机自启:")
    was = autostart.is_enabled()
    try:
        autostart.enable()
        check("开启后注册表可读到", autostart.is_enabled() is True)
        check("命令行含 --tray 且用 pythonw",
              "--tray" in (autostart.current_value() or "")
              and "pythonw" in (autostart.current_value() or "").lower(),
              (autostart.current_value() or "")[:70])
        autostart.disable()
        check("关闭后注册表已清除", autostart.is_enabled() is False)
    finally:
        # 还原用户原来的状态, 别把测试的副作用留下
        autostart.set_enabled(was)
        check("已还原测试前的自启状态", autostart.is_enabled() is was,
              "原本 %s" % was)

    # ---------- 落盘 ----------
    print("\n配置落盘:")
    ctl.stop_glass()
    app.processEvents()
    check("关闭玻璃层后 glass_on=False", ctl.glass_on is False)
    check("关闭后角度源已停止", ctl.hub.active_running() is False)

    raw = cfg_path.read_bytes()
    check("写出的文件无 BOM", not raw.startswith(b"\xef\xbb\xbf"))
    saved = json.loads(cfg_path.read_text("utf-8-sig"))
    check("能被 utf-8-sig 解析", isinstance(saved, dict))
    check("改动的参数确实落盘",
          abs(saved["max_tilt_deg"] - 70.0) < 1e-6
          and saved["outside_mode"] == "backdrop"
          and saved["port"] == "COM9")
    check("原有键没有丢", "camera_scale" in saved and "blur_spread" in saved)

    ctl.shutdown()
    # 必须收掉面板的扫描线程: 上面 panel.show() 会启动 CameraScanThread,
    # 线程还在跑时对象被销毁 -> Qt 直接 abort (0xC0000409)。
    # `shutdown()` 只等 2 秒就先寄存它 (open_camera 最坏 20 秒, 不能干等);
    # `wait_orphans()` 再补一段窗口, 否则**解释器关闭阶段**会踩到 native 对象,
    # 表现为"测试全 PASS 之后进程以 0xC0000409 退出" —— 很容易误判成功能问题。
    panel.shutdown()
    panel.wait_orphans()
    check("全程没动真实 config.json", real_cfg.read_bytes() == real_before)
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("-" * 62)
    if failures:
        for f in failures:
            print("[FAIL] " + f)
        return 1
    print("[PASS] GUI 链路全部通过 (托盘/开关/参数/显示器/自启/落盘)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
