# AGENTS.md — AI 协作指南

面向在本仓库工作的 AI 编码代理（与人类贡献者）。**改动前请先读完本文件。**
这里记的每一条都是实际踩过的坑，不是理论建议。

## 项目是什么

- **实现方式**：笔记本上盖摄像头 ORB 特征匹配测开合角（独立实现）。
- **动画效果**：来自 `windowsduo`（KaedeharaKazuha1029）——PyQt6 + GLSL 逆投影「悬浮玻璃」。

设计上唯一重要的决定：**三个角度源归一到同一个 `level ∈ [0,1]`**，
渲染层完全不知道背后是摄像头、ESP32 还是键盘。换角度源不影响动画。
如果你要动角度相关的逻辑，先确认你改的是「源」还是「level 映射」，
不要把某个源的特有概念泄漏到渲染层。

## 分工边界

- `angles/` —— 只负责产出 `level`。不要在这里碰 GL、窗口、纹理。
- `render/` —— 只认 `level`。不要在这里直接读串口/摄像头/键盘。
- `angles/hub.py` —— 唯一的粘合层。跨层需求放这里。
- `main.py` —— 组装与生命周期。不放算法。

## 构建与运行

```powershell
powershell -ExecutionPolicy Bypass -File tools\setup_env.ps1   # 建 .venv 装依赖
.venv\Scripts\python.exe main.py --source manual
.venv\Scripts\python.exe main.py                                # 摄像头 (默认)
```

## 改代码前必读的坑

1. **GL 窗口必须继承 `QOpenGLWidget`**（`PyQt6.QtOpenGLWidgets`）。
   继承普通 `QWidget` 时 `initializeGL/paintGL` 永远不会被调用，窗口只剩默认
   底色（白屏）—— 这是 windowsduo 历史上最大的 bug。

2. **必须 `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE=0x11)`**（见
   `overlay.py::showEvent`）。Overlay 是不透明置顶全屏窗，mss 截屏会抓到自己
   上一帧，反馈几帧后收敛成纯色。改截图逻辑时别动它。

3. **`.bat` 里禁止中文。** cmd 按 GBK 解码，乱码会被当命令执行。同理
   **`.ps1` 注释也只写 ASCII** —— PS 5.1 对无 BOM 的 `.ps1` 按 ANSI 解码。

4. **本机没有 `pwsh`，只有 Windows PowerShell 5.1。** 调脚本用
   `powershell -ExecutionPolicy Bypass -File`。

5. **GLSL 用 `#version 330 compatibility`**，Qt 要请求 3.3 Compatibility profile
   （`textureLod` 依赖 3.3）。实测拿到的会是更新的兼容上下文（如 4.6），没问题。

6. **`config.json` 是唯一参数入口。** 不要在代码里另加硬编码默认值去覆盖它。

7. **`--smoke` 的浓度必须设在角度源上，不能只设 `widget.g`。**
   `tick()` 每帧都会用 `hub.resolve()` 覆盖 `target`；只设 `widget.g` 会被
   手动源的 0.0 拉回去，2 秒后截到的是一张摊平的普通桌面，等于什么都没验证。
   （已经踩过一次。）

8. **摄像头调试窗口必须在采集线程生成图像，主线程只负责 `imshow`。**
   跨线程调 `cv2` 有风险；`pop_debug()` 只传已成图的 ndarray。
   另外因为玻璃层是置顶的，调试窗必须 `cv2.setWindowProperty(..., WND_PROP_TOPMOST, 1)`
   才看得见。

9. **玻璃层不能常驻显示（最重要的一条）。** 它画的是"每秒只截 `refresh_hz` 次的
   桌面快照"，而且是全屏置顶窗。常驻显示 = 用户看到的整个桌面只有几 FPS，
   打字、拖窗口全是卡顿感 —— 用户会报"打开后电脑卡顿"，而这**与着色器性能无关**。
   所以 `overlay.tick()` 在浓度低于 `idle_hide_below` 时必须 `hide()` 掉整个窗口。
   改这一块时务必保证：
   - `_visible` 要和真实可见性同步（`showEvent` 里置 True），否则首次 tick
     不会把浓度 0 的窗口收起来，卡顿依旧；
   - 隐藏时不要 kick 截屏、不要 `update()`；
   - 只在浓度或帧号变化时才 `update()`，别 60Hz 空转。

   实测（2560×1600, RTX 4060 Laptop, 强制 manual 源）：
   托盘待机 **0%** / 空闲隐藏 3.6% / 玻璃层显示 ~20% / 摄像头跟踪另加 ~34%。
   注意摄像头那一份是**独立于玻璃层**的，所以关掉玻璃层必须停掉角度源。

10. **别靠直觉优化性能, 先量。** 着色器**从来不是瓶颈** (2560x1600 单帧仅
    1.5~3.3ms, 用 `tools/bench_shader.py` 复现)。抓屏才是 —— 但也已经换成
    DXGI 了, 见第 20 条。历史教训: mss 的 GDI BitBlt 一帧 29.8ms, 当时
    `refresh_hz=30` 要吃掉 89% 单核; 而用户报的"卡顿"其实是另一回事
    (玻璃层常驻显示低帧率快照), 我因此误判过一次。**先分组件量, 别猜。**

11. **界面层在 `ui/`，不要把它和 `angles/`、`render/` 混在一起。**
    托盘和设置窗口只跟 `ui/controller.py::AppController` 打交道。
    几条硬约束：
    - 玻璃层默认**关着**。关着时角度源必须停掉（摄像头常驻 = 34% 单核）。
    - 换显示器要**同时**改 `capture.set_region()`（mss 用物理像素，乘
      `devicePixelRatio`）和 `overlay.set_screen()`，漏一个就采样错位。
    - 改了摄像头 index/后端之后必须 `hub.invalidate("camera")` 重建对象，
      光改 cfg 不会让已打开的设备换过去。
    - `GlassOverlay` 在 `__init__` 里把渲染参数缓存成了实例属性，改完 cfg
      要调 `apply_config()`，否则设置窗口改了没反应。

12. **PyQt 的槽函数里未捕获的异常会直接 abort 进程**（`0xC0000409`），
    不是普通报错。我已经踩过一次：`_paint_rate()` 用了 `time` 但只在
    `tick()`/`_print_status()` 里做过局部 `import time`，于是每次刷新状态行
    都崩。`render/overlay.py` 现在统一在模块级 `import time` —— 别再退回
    局部导入。

13. **QThread 还在跑时对象被销毁, Qt 会直接 abort 整个进程**
    （`QThread: Destroyed while thread is still running`，退出码 `0xC0000409`）。
    和上一条一样, 它不是异常、捕获不到。已经踩过一次: 打开设置窗口会启动
    `CameraScanThread` 扫描摄像头, 扫描没结束就退出程序 → 崩。
    规矩: 任何 QThread 都必须有 `stop()` + 在退出路径上 `wait()`。
    `run_tray()` 的 finally 里顺序是 `panel.shutdown()` -> `controller.shutdown()`,
    先收界面再停角度源。注意 `open_camera()` 阻塞在打开设备上没法中途打断,
    所以 `stop()` 只能在两个 index 之间生效, 最坏多等一个设备超时。

14. **qfluentwidgets 的控件签名和 Qt 不完全一样，必须走 `ui/widgets.py` 兼容层。**
    已经踩过一次: `ComboBox.addItem(text, icon, userData)` —— 而 Qt 是
    `addItem(text, userData)`。直接按 Qt 的写法调, userData 会被当成 icon 吃掉,
    `currentData()` 全返回 `None`, **下拉选择静默失效**（不报错、只是没反应）。
    同类差异还有 `SwitchButton` 只发 `checkedChanged` 不发 `toggled`。
    规矩: 界面代码一律 `from .widgets import ...`, 不要在 panel 里直接 import
    qfluentwidgets。兼容层里已经把这两个坑抹平, 并且回退路径（`WIN_DUO_NO_FLUENT=1`）
    有测试覆盖 —— 改动后两条路径都要跑 `tools/ui_test.py`。

15. **界面里的信号连接必须写在填充之后（`ui/panel.py::_wire`）。**
    这就是我写坏过 config.json 的原因：控件建好就接信号，然后 `refresh_all()`
    用真实配置去 `setValue/setChecked` 填充 —— 每一次填充都会被当成"用户改动"
    触发 `_on_effect`，把**还没填好的控件值**写回配置。结果 `max_tilt_deg`
    变成 89、`eye_dist_h` 变成 0.5、`blur_spread` 变成 0、`refresh_hz` 变成 1。
    现在的顺序是固定的：`_build()` → `refresh_all()` → `_wire()`。
    另外 `_on_effect` 之类仍然有 `_loading` 互斥兜底。
    `tools/ui_test.py` 里加了"真实 config.json 哈希不许变"的检查, 别删。

16. **快捷键只在「键盘」角度源下生效**（`SourceHub` 在切源时调
    `control.set_enabled`）。`Esc`/`q` 退出键不受门控 —— 否则没有托盘时会被困住。

17. **键盘模式必须用全局热键, 不能用控制台按键。**
    启动器是 `pythonw.exe`, **没有控制台**, `msvcrt.getwch()` 读不到任何键
    —— 用户报的"手动键盘模式没生效"就是这个原因 (控制台版本一直是好的,
    所以很容易误判成功能没写)。
    现在 `AppController` 持有 `HotkeyManager`, 键盘模式的键 **随角度源自动
    注册/注销** (模式分组见第 19 条): 切到摄像头时若不注销, 会白白吃掉
    `Ctrl+Alt+方向键`。
    `hotkey_lines()` 只返回**注册成功**的键, 设置窗口显示它 —— 注册失败
    (被别的程序占用 / 同时开了两个实例) 时用户能立刻看出来, 不会对着一个
    按了没反应的键发懵。
    控制台按键路径保留, 供 `python.exe` 启动时使用。

18. **切换角度源必须停掉旧源。** `SourceHub.set_active()` 原来只启动新源、
    **不停旧源** —— 于是从摄像头切到键盘之后, 摄像头还在后台跑着 (占着独占
    设备、白吃约 34% 单核), 而且键盘模式下根本不需要它。现在 `set_active`
    会 `stop_source(previous)`。
    另外键盘模式下摄像头应当是**完全关着**的; 只有"匹配调试窗"需要画面时
    才用 `hub.borrow_camera()` 临时借, 关窗时 `release_camera_if_idle()` 还回去。
    改角度源相关代码时先想清楚: 这个设备现在该不该开着。

19. **热键要按模式分组。** `HOTKEY_DEFS` 的第 4 个字段是生效模式
    (`None` = 一直 / `"manual"` = 只在键盘模式):
      - **摄像头模式下只留紧急关闭。** 摄像头模式是全自动跟手的, 多一个键只会
        误触; 浓度键走手动覆盖, 会和自动跟踪打架。标定/翻转方向改用设置窗口
        里的按钮 (用户明确要求过"删除逻辑冲突的键", 别再合回去)。
      - 键盘模式: 开关玻璃层 / 调试窗 / 浓度 ×4 + 紧急关闭 = 7 个。
    两组的交集只有紧急关闭。改完记得同步改 `tools/hotkey_test.py` 和
    `tools/ui_test.py` 里那几条断言。

20. **抓屏必须优先 DXGI, 这是"重截频率上限"的关键。**
    mss 走 GDI BitBlt, 本机 2560x1600 实测 **27~29ms/帧 -> 上限 37Hz**;
    DXGI (bettercam) **0.09~0.23ms/帧 -> 上限 = 显示器刷新率 (本机 165Hz)**。
    快 ~300 倍。`render/capture.py` 依次试 bettercam -> dxcam -> mss, 装不上
    也能跑。几个必须记住的点:
      - **DXGI 报的是物理像素, 但只在进程 DPI-aware 时才准**。否则 Windows
        按缩放比虚拟化 (本机 2560x1600 会变成 1707x1067), 分辨率匹配直接失败。
        `ensure_dpi_aware()` 兜底。裸 Python 脚本里测会得到错的值。
      - **多显示器不能假定 output 0 就是目标屏** —— 本机 output 0 是 2560x1600,
        output 1 是 1920x1080。要按分辨率匹配。
      - **bettercam 的 camera 是"每 output 单例"。** 先遍历探测、再 create 的
        写法会踩坑: 探测时 `release()` 掉的正是后面要用的那个对象, 拿到手就是
        已销毁的 (`'NoneType' has no attribute 'AcquireNextFrame'`)。
        必须"边探测边保留"。
      - **必须用 `output_color="BGRA"`。** DXGI 原生就是 BGRA, 请求 RGBA 会多
        做一次逐像素换 R/B: 单次调用 **1.12ms -> 4.42ms**, 而 4ms 直接把采集
        线程忙住, 频率就上不去了。顺带好处是和 mss 统一, `frame_bgr()` 只剩
        一条路径。
      - **通道顺序要卡死。** 搞反了红蓝互换, 肉眼看图不容易发现。
        `tools/capture_backend_test.py` 里用**纯蓝窗口**判定 (纯色窗口 -> BGR
        里 B 必须是最高)。别用"通道均值"当判据: 壁纸接近无彩色时 R/B 互换
        只让均值动 2.0, 那是假的安全感。
      - **一次瞬时 DXGI 错误不要立刻掉回 mss** —— 掉回去就是每帧 30ms。
        容忍几次 (`_DxgiSource.MAX_ERRORS`)。
      - **别在 worker 线程跑着的时候直接调 `_dxgi.grab()`**: 两个线程并发
        `AcquireNextFrame` 会直接 `DXGI_ERROR_INVALID_CALL`。测试里也要走
        `kick()` + `done.wait()`。

21. **"DXGI 没变就不给帧"是错的 —— 但结论是"照单全收", 不是"自己算签名"。**
    我一开始假设 DXGI 只在桌面变了才给帧, 实测被推翻: **165Hz 的屏上合成器
    每 6ms 产一帧, 内容一模一样也照给**。
    当时我由此推出"那得自己做内容比对", 于是有了 `_signature_changed` ——
    **那是错的, 它把画面整个冻死了** (见第 40 条)。
    正确结论: 后端给帧就收, **不做任何内容比较**。渲染层按 `render_fps` 限速
    重绘, 浓度≈0 时整个窗口 `hide()`, 这两条才是该停的地方。

22. **（历史）采样比对的坑 —— 现在没有采样代码了, 但教训要记住。**
    如果将来真要写"像素采样做判断", 两条:
      - **采样必须按整像素, 不能用固定字节步长。** 曾经的 `buf[::4096]` 里
        4096 是 4 的倍数, 于是每个采样点永远落在同一通道 (BGRA 的 B),
        **等于只看蓝色分量**: 纯红<->纯绿的切换完全检测不到。
        要写成 `buf.reshape(-1, 4)[::step].ravel()`。
      - **测试判据必须够锐利。** 同上那个 bug, 用"通道均值"当判据时测试显示
        "40/40 帧全过", 换成抖红绿窗口才暴露。**判据不锐利的测试比没有更危险。**

23. **"上限"是纸面数字, 实测速率要另算, 而且在界面上显示出来。**
    设了 `refresh_hz=165` 不等于跑到 165。用户回过一句"实际上频率没那么高啊",
    查下来是独立的天花板叠加 (详细数据见 README):
      - `tick` 定时器原来写死 16ms -> 每秒最多 62 次 tick, 设 137 也没用。
        现在 `_tick_ms()` 按 `max(refresh_hz, render_fps, 60)` 算, 下限 4ms。
      - "主线程 kick -> 唤醒线程 -> 抓 -> 回信"一个来回约 **7ms**, 最多
        ~107 次/秒。现在玻璃层显示时采集线程**自己连续跑** (`target_hz`),
        隐藏/收起时置 0 回到等待。
    状态行里的 `截屏=x/s` 是**实测值**, 别再拿配置值当结论。
    测这类东西**一定要看"帧号推进率"**, 光看"抓了几次"会被过滤掉的那一层骗到。

    **补充 (抓屏要主动压到"要画的帧"以下):** 上面说的"跑到 165"是**能力**上限,
    但**需求**上限远低于此 —— 玻璃层被 `render_fps`(默认30) 限速, 每秒最多重绘
    30 次, 抓得更勤的帧上屏前就被覆盖了。而每抓一帧要 `.copy()` 16MB, 抓 143/s
    就是 2.3GB/s 的纯内存拷贝。实测 (level=0.5 静止):
        抓屏 143/s -> 单核 85.5%      抓屏 56/s -> 单核 56.6%    重绘两者都 ~28/s
    所以 `overlay.apply_config()` 现在算一个 `capture_hz = min(refresh_hz,
    2*render_fps)`, tick 里用**它**去设 `capturer.target_hz`, 不用 refresh_hz。
    - 取 **2 倍**不是 1 倍: 正好等于 render_fps 时抓屏和 tick 错相位会偶尔抓空,
      重绘掉到 ~20/s; 2 倍稳定在 28~30/s (这就是"保留性能"的关键)。
    - `refresh_hz` 仍然驱动 `_tick_ms()` (tick 本身很便宜, 跑快点能把重绘时机
      卡得更准) —— **别把 tick 也一起压下去**, 那样重绘会跟着掉。

24. **`CaptureWorker.stop()` 必须先 `join` 再释放设备。** 连续模式下采集线程
    一直在 native 层跑 `grab()`, 从主线程直接 `release()` 会让它踩到已释放的
    对象 —— 实测 **`access violation reading 0x...168`**, native 崩溃,
    Python 侧什么都抓不到。规矩: 先 `_halt=True` + 设事件, `join(timeout)`,
    然后才 release。

25. **别把属性命名成 `_stop` —— 会遮蔽 `threading.Thread._stop()`。**
    `threading.Thread` 内部有个 `_stop()` 方法, 用 `self._stop = True` 覆盖它
    之后, `join()` 里一调就 `TypeError: 'bool' object is not callable`。
    现在叫 `self._halt`。

26. **测试里等 worker 别用 `done.wait()`, 直接盯帧号。**
    两个坑, 我都踩过:
      - `done` 是"跑完过一次"的**持久标记**, 不清就直接 `wait()` 立刻返回 ——
        循环里每次拿到的都是同一帧, 计数看着漂亮 ("40/40 帧") 其实什么都没
        验证。清了也有竞态: worker 正忙时 `kick()` 被 `busy` 挡掉, 我们等到的
        却是上一次的 `done`。
      - 等待期间**必须持续 `app.processEvents()`**。否则用来制造画面变化的
        那个测试窗口根本不会重绘, 桌面没变, DXGI 自然不给新帧, 于是死等超时
        (踩过: 只 sleep 不 processEvents, 0/40 帧)。
    现在统一用 `pump_until_new(worker, app)`: 记下帧号, kick, 然后一边
    processEvents 一边等帧号变。

27. **禁止自动切换角度源。** 设备打不开只提示（`AppController._warn_if_unavailable`），
    绝不 `set_active("manual")` 之类自己跳走。模式一律由用户在托盘/设置里选。
    这条是用户明确要求的, 别"好心"改回自动回退。

28. **必须做单实例保护 (`main.already_running()`, 命名互斥体)。**
    两个实例叠在一起会同时产生两个极难自查的假故障:
      - 两层**全屏置顶**的玻璃层叠着, 用户看到的那层可能是**旧实例**的
        (旧代码/旧帧), 于是"画面静止不动, 怎么改配置都没用" —— 我为此
        白查了好几轮, 用户也反复反馈"依旧静止";
      - 后启动的实例注册不了全局热键 (`RegisterHotKey` 撞车), 于是
        "热键也没反应"。
    实现要点: `ctypes.WinDLL("kernel32", use_last_error=True)` + 句柄存进
    模块级列表 (句柄一没, 互斥体就还回去了)。
    **`ctypes` 必须在 main.py 顶部 import** —— `already_running()` 里的
    `except Exception: return False` 会把 `NameError` 吞掉, 表现为"保护完全
    没生效却没有任何报错" (踩过一次)。
    另外别用 `windll.kernel32.GetLastError()` 直接读: 中间夹了别的调用就永远
    拿到 0, 必须走 `use_last_error=True` + `ctypes.get_last_error()`。

29. **bettercam 的帧数组不拥有自己的内存 (`OWNDATA == False`)。**
    它只维护几个缓冲轮换 (实测 3 个 id 交替)。把 `arr` 直接存进 `frame` 的话,
    渲染层可能在它被下一次 grab 覆盖之后才去上传 —— 轻则撕裂, 重则一直看到
    同一张旧图。`_store()` 里对非 `OWNDATA` 的数组做一次 **`arr.copy()`**
    (16MB 约 1.5ms)。
    **别用 `np.ascontiguousarray`** —— 它对已连续的数组原样返回、不拷贝,
    等于没保护 (详见第 53 条)。
    **调试提示**: 想验证"两帧到底差多少"时, 必须 `.copy()` 一份再留作
    对比基准, 否则比的是同一块内存, 永远是 0 —— 我因此先误判成"画面没变"。

30. **启动器一律用 `pythonw.exe`**（`start "" "%PYW%" main.py`），不留命令行窗口。
    `main.py` 在 `sys.stdout is None` 时会把输出重定向到 `win_duo.log`。
    注意 venv 的 `pythonw.exe` 是转发器，会再拉起基础安装的 `pythonw.exe`，
    所以任务管理器里看到两个进程是正常的。

31. **数值输入一律用 `ui/widgets.py::NumberField`（纯键盘），不要用
    Slider / QSpinBox / DoubleSpinBox。** 用户明确要求过：滑块和带上下箭头的
    数字框都不好用。`NumberField` 的要点：
    - 校验器只限制**字符**（`^\d{0,4}(\.\d{0,3})?$`），**不限制数值范围**。
      用 `QDoubleValidator(lo, hi, dec)` 会在打字途中就拒掉中间态 —— 范围
      30~89 时想输 50，第一个字符 `5` 就被拒，根本没法用。范围在提交时夹。
    - 回车/失焦（`editingFinished`）才提交，值真的变了才发 `changed`。
    - `setValue(v, emit=False)` 用于程序化填充，避免无谓触发写盘。

32. **OpenCV 调试窗有两个必踩的坑**（都在 `render/overlay.py`）：
    - **标题必须是纯 ASCII。** HighGUI 在 Windows 上按本地代码页解释窗口标题，
      写中文必然乱码。现在固定为 `"win-duo match debug"`。日志可以写中文，
      窗口标题不行。
    - **`destroyWindow()` 不会立刻生效。** HighGUI 的消息是靠 `waitKey` 驱动的,
      destroy 只是排进队列; 不再 pump 的话窗口会**一直留在屏幕上**, 表现为
      "调试窗打开就关不上"。`close_debug()` 里销毁后连 pump 8 次 `waitKey`。
      另外用户点窗口的 X 关掉时, 要靠 `getWindowProperty(WND_PROP_VISIBLE) < 1`
      把 `_dbg_window_open` 同步过来, 否则会继续往一个不存在的窗口 imshow。
    回归测试: `tools/debug_window_test.py`（开→关→再开→手动关→状态同步）。

33. **玻璃层会盖住托盘菜单 —— 弹出期间必须先收起来。** 玻璃层是全屏置顶窗,
   Qt 的托盘右键菜单是普通窗口, 会被它压在下面: 菜单其实弹出来了也能点, 但
   **用户看不见**, 表现就是"点托盘没反应 / 屏幕回不去了"。
   `ui/tray.py` 里接了 `menu.aboutToShow/aboutToHide` -> `suppress_glass()`。
   任何新加的弹出窗口 (对话框、菜单) 都要考虑这个问题。

34. **全局热键不要用 QWidget + 重写 `nativeEvent` 实现。** 实测在 `winId()`
   触发原生窗口创建时, Qt 会回调 `nativeEvent`, 直接以 `0xC000041D`
   (STATUS_FATAL_USER_CALLBACK_EXCEPTION) **崩掉整个进程**, 连 Python 异常都
   抓不到 —— 排查时只能靠最小复现。
   正确做法见 `ui/hotkey.py`: `RegisterHotKey(hwnd=NULL, ...)` +
   `QAbstractNativeEventFilter` 收 `WM_HOTKEY`。WM_HOTKEY 会投递到线程消息
   队列, Qt 的事件派发器会交给 native event filter。
   回归测试 `tools/hotkey_test.py` 会**真的模拟按键**验证整条链路。

35. **打开设备一定要异步。** `CameraAngleSource.start()` 以前在主线程上同步
    `open_camera()`, 而它要逐个后端试 + 抓几帧做冻结帧检测, 实测约 **1 秒**
    —— 用户点"开启玻璃层"就会僵住一下。现在设备在采集线程里打开, `start()`
    立即返回。
    副作用: `available()` 在打开完成前给不出结论, 所以可用性提示要**延后**
    再查 (`AppController._schedule_source_check`), 直接查会漏报。
    另外 `stop()` 的 `join` 超时要短 (0.6s): 它同样在主线程上跑。
    采集循环必须包 `try/finally` 释放摄像头, 否则泄漏设备会导致下次打不开。

36. **GL 上下文必须预热, 否则首次 show() 要 ~680ms。**
    `QOpenGLWidget` 的 `initializeGL`(建上下文 + 编译链接着色器) 只在窗口
    `show()` 之后才跑, 实测第一次 `start_glass()` = **676ms**, 第二次 = 3ms。
    光把设备打开异步化还不够 —— 剩下这 680ms 就是用户感觉到的"卡一下"。
    `AppController.prewarm_overlay()` 在启动后 (玻璃层还关着、**第一张截图
    已就绪**) `show()` 一帧再 `hide()`。等截图就绪是必须的: 否则画的是空纹理,
    会闪一下黑屏。
    改 `shader.py` 或换 GL 资源之后, 预热照样有效, 别删。

37. **玻璃层的显隐必须用双阈值 + 最短驻留。** 测角有死区, 在死区边缘 level
    会逐帧跳, 缓动后的浓度于是反复穿越单一阈值 -> 玻璃层快速 show/hide ->
    **屏幕闪、鼠标光标看起来在眨**。现在用 `idle_hide_below` /
    `idle_show_above` / `idle_dwell_sec` 三个键。
    回归检查在 `tools/ui_test.py` 里 (模拟抖动 40 次, 要求翻转 ≤1 次),
    **同时也要验证浓度稳定时仍能正常显隐**, 别把功能一起修没了。

38. **死区要做成"减掉再拉伸", 不能直接归零。** 老代码在 level 刚过 deadzone
    时返回**原始 level** (0.03), 而低于时返回 0 —— 这是一个人为的 0→0.03
    台阶, 是光标闪烁的第二个成因。现在 `(lvl - deadzone) / (1 - deadzone)`,
    边界两侧都收敛到 0。
    `tools/ui_test.py` 里有断言: 刚过死区时 level 必须 < 0.002
    (修之前是 0.030), 这条能真的抓到回归。

39. **全屏重绘要限速 (`render_fps`, 默认 30)。** 浓度连续变化时原来以
    **52~57 次/秒**重绘 2560x1600 全屏置顶窗, 每次都触发整屏合成 —— 鼠标
    光标叠在上面就会闪。限速后 20~23/s。
    光"只在变化时重绘"是不够的: 合盖过程中浓度一直在变, 等于没限。

40. **绝对不要加"内容没变就不推进帧号"的过滤 —— 它会把画面整个冻死。**
    我加过一个全图平均差的静止检测 (采样 4000 个像素, 阈值 `static_tol=1.5`),
    看起来很有道理, 实际是灾难:
      - 打字/光标闪: 影响几百像素, 摊到 400 万像素上平均差 ~0
      - 小窗口刷新: 几万像素 -> **0.2~0.3**
      - 滚动一屏:   几十万像素 -> ~1.2
      - 整屏视频:   全部     -> ~5
    日常操作全在 **0.2~0.3**, 永远够不到 1.5 —— **帧号永不推进, 玻璃层停在
    第一帧**。用户报"直接变成静态的了, 帧率似乎为 0", 我查了好几轮才定位到。
    现在 `render/capture.py` 里**没有任何内容比较**: 后端给了帧就推进。
    防空转只靠两条: `render_fps` 限制重绘节奏 + 浓度≈0 时整个窗口 `hide()`。
    **回归防线**: `tools/capture_backend_test.py` 的"帧号必须持续推进"和
    `tools/ui_test.py` 的"没有 changed()/suggested_hz() 这类内容过滤"
    (直接断言这两个方法不存在)。别把它们删掉。

41. **测试桌面变化的用例不能只动浓度。**
    第 40 条那个"内容过滤冻死画面"的 bug 之所以能溜过去, 是因为**所有用例都
    只改浓度、不动桌面内容** —— 而浓度变化会绕过那层过滤, 于是测试全绿。
    现在 `tools/ui_test.py` 里有一条专门抖一个红绿窗口、**浓度不动**、要求
    重绘 >= 10/s。
    写这类测试时**别依赖用户 config 里的 refresh_hz** —— 用户设成 5Hz 时
    3 次/秒重绘是**正确**行为, 断言会误报。测试自己钉死一个值。

42. **扫描线程必须有总时间预算, 否则退出时会 ACCESS_VIOLATION。**
    程序退出时如果 `CameraScanThread` 还跑在原生 cv2/MediaFoundation 调用里,
    进程拆除会直接 **0xC0000005**(访问违例), 偶发且抓不到 (实测 4 次中 2 次)。
    这跟 QThread 的 abort(`0xC0000409`) 是**两回事**, 别混。
    现在: 扫描封顶 `budget_sec`(6s) + 退出时 `wait(budget+3s)`。
    任何"会阻塞在原生库里"的线程都要照这个办 —— `stop()` 打断不了原生调用,
    所以只能靠预算保证它一定会结束。

43. **重截频率的上限就是一个固定值: 显示器刷新率。** `CaptureWorker.max_hz()`
    返回 `max(60, display_hz)`, 设置窗口拿它当输入上限。
    **不要改成"实测单帧耗时算上限"** —— 我试过, 那个数字随负载在 18~165 之间
    跳, 用户直接反馈"你这个频率怎么是动态的"。上限就该稳定: 合成器每秒最多产
    那么多帧, 更高没意义。
    (耗时仍然记录在 `last_ms`, 但只用于诊断, 不参与上限计算。)

44. **摄像头是独占设备, 回归测试之间必须清干净。** 批量跑时 `debugwin` 和
    `hotkey` 出现过偶发失败, 单独跑都通过 —— 是上一个测试的进程还没退干净、
    还占着摄像头。跑回归时每步之间 `Stop-Process` + 等 1 秒。
    这类失败**不要**当成产品 bug 去改代码。
    **补充 (ui_test 有 ~1/6 的固有崩溃率, 别被骗):** `tools/ui_test.py` 即使
    在**完全没改过**的版本上, 也会以大约 **1/6** 的概率在收尾时以原生错误退出
    (`0xC0000374` 堆损坏 / `0xC0000005` 访问违例, 崩点在"调试窗借摄像头"那段
    之后)。实测对照: 原版 6 次崩 1 次、带新断言 6 次崩 1 次 —— **一样的比率**。
    判断是不是自己改坏了, 必须**同样本量对照** (各跑 5~6 次), 不能拿"改前跑
    1 次过了"当基线。只看单次结果很容易把自己的改动误判成罪魁 (我为此白查了
    两轮: 先怀疑新增断言, 又怀疑低内存定时器, 最后发现原版本来就崩)。
    写测试用例时另外注意: **别在测试中段开"会排真实定时器"的开关** —— 低内存
    模式一开就给 controller 起了周期检查定时器, 放在中段会在后面的用例里真的
    触发 (把正在用的 GL 窗口拆掉)。测试里把它改成 `ctl.IDLE_RELEASE_SEC =
    99999`, 或者验证完立刻把开关关掉。

45. **测试脚本里的清理要挂 `atexit`。** `tools/ui_test.py` 中途抛异常时, 末尾
    那句 `panel.shutdown()` 会被跳过, `CameraScanThread` 还在跑 -> Qt abort
    (`0xC0000409`), **把真正的报错掩盖成崩溃码**, 排查时会被带偏。

46. **二分定位性能问题时，先确认没有被别的东西污染。** 我第一次做组件二分
    得出"空转的 Qt 事件循环吃 34%"，其实是 `config.json` 里 `source` 是
    `camera`，每个模式都顺带把摄像头跟踪跑起来了。二分前先把变量钉死
    （`cfg["source"]="manual"`），否则会追着假象改代码。

47. **图标只有一个入口 `ui/widgets.py::make_icon()`, 别再往别处塞画图代码。**
    它优先加载 `win-duo.ico` (tools/make_icon.py 生成, 9 档尺寸), 文件不在就
    退回运行时的 `_draw_icon()` —— 少一个资源文件不会崩, 也不会显示空白图标。
    已经接好的地方: `apply_theme()` 里的 `QApplication.setWindowIcon`
    (任务栏/Alt-Tab/通知) + 设置窗 + 日志窗 + 玻璃层 + 托盘。
    **`--selftest` 和 `run_direct` 这两条路径也必须调 `apply_theme()`** ——
    我漏过一次, 那两条路径下任务栏图标是 Python 解释器自己的。
    另外 Windows 任务栏按"AppUserModelID"归组, `apply_theme()` 里顺带设了
    `SetCurrentProcessExplicitAppUserModelID`, 不设的话任务栏会显示解释器图标。

48. **`.ico` 的每一档都要单独重绘, 不能只画 256 往下缩。**
    16px 下按比例算出来的线宽不足 1 像素, 缩回后细节糊成一团。我第一版就
    栽在这里: 16x16 的铰链亮和不亮的面板糊在一起 (实测中列亮度 147, 左面板
    148, **等于白画**)。小尺寸必须主动简化 (去高光、加粗关键元素)。
    验证方式见 `tools/icon_test.py`: 不靠肉眼, 而是量"右半比左半暗、中列最亮"。
    (我这个模型看不了图, 只能这么验。)

49. **`.bat` 文件没法设图标。** Windows 按扩展名给 .bat 一个通用图标, 文件
    内容里写什么都改不了。要自定义图标只能用**快捷方式**(.lnk), 由它引用
    .ico —— 见 `tools/make_shortcut.py` (借 PowerShell 的 WScript.Shell COM
    建 .lnk, 不需要额外 Python 依赖)。

50. **`angles/camera.py` 是独立实现。**
    数学路线是自成一套的：
      - 求旋转用**归一化平面上的 Kabsch/SVD**（`solve_rotation`，3 自由度），
        不是 `findHomography`(8 自由度) + `Rodrigues`。理由写在文件头：
        让数据拟合 8 自由度模型时，参数之间会互相吸收误差，解出的角度会漂。
      - 取角度用**把旋转向量投影到铰链轴**（`angle_about_axis`），不是取欧拉角
        分量 —— 侧向分量会被显式丢掉，不和主角度耦合。
      - 抗漂移用**按角度均匀采样 + 回访绝对校正**（`_visit_correction`），
        不是按间距打固定锚点。
      - RANSAC 的内点判据用**角度残差**，不是像素重投影。
    改这块前先看文件头的长注释，那里解释了每个选择的原因。

51. **改了测角算法就必须跑 `tools/tracker_test.py`，看数值别看感觉。**
    它用合成帧给出四个硬指标，重写时的基线是：
        稳态最大误差 0.02 度 / 往返归零 0.02 度 / 连续爬升滞后 ~4.2 度
    容差是稳态 2 度、滞后 6 度，所以有足够余量 —— 但如果稳态误差突然涨到
    1 度以上，就是退化了。
    真机另跑 `tools/camera_track_test.py`（静止时 pitch 标准差应 < 0.05 度；
    实测 0.003 度）。

52. **打包后不能用 `__file__` 定位数据文件 —— 用 `paths.py`。**
    onefile 模式下 `__file__` 指向临时解包目录 (`%TEMP%\_MEIxxxxxx\`),
    每次启动路径都不同、退出就删。往里写 config/日志 = **用户设置存不住**。
    统一入口:
      - `paths.data_dir()` —— 可写数据 (config.json / win_duo.log / 用户背景图)
      - `paths.resource_dir()` —— 只读资源 (win-duo.ico), 打包后在解包目录
      - `paths.writable_data_dir()` —— 探测可写性, 不行退 `%LOCALAPPDATA%`
    **⚠️ 别在热路径 (paintGL / tick / 定时器回调) 里做 import。**
    我为了打包把 `from paths import data_dir` 写进了 `overlay._backdrop_path()`,
    而那个函数会被 `paintGL -> _build_backdrop` 调到; 后来编辑时又把模块级的
    `import os` 删了, 结果 `paintGL` 抛 `NameError: name 'os' is not defined`
    —— **PyQt 槽函数里的未捕获异常会直接 abort 进程** (0xC0000409), 见第 12 条。
    表现是"玻璃层一显示就整个崩掉, 日志里什么都没有"。`paths` 现在在模块顶部导入。

53. **`np.ascontiguousarray` 对已连续的数组不做拷贝 —— 它不是 `copy()`。**
    想拿独立副本必须用 `arr.copy()`。实测 bettercam 给的帧是
    `C_CONTIGUOUS=True` 且 `OWNDATA=False`, 于是
    `np.ascontiguousarray(arr) is arr` → **True**, data 指针完全相同。
    我原来在 `_store()` 里用它"防缓冲被复用", 那个保护**从来没生效过**。

54. **交给 OpenCV 的数组必须"连续且可写"。**
    `arr[:, :, :3]` 这种跨步切片内存不连续, OpenCV 在 native 层按连续三通道读
    → **直接崩**, Python 侧看不到异常 (`Unhandled Python exception`)。
    `frame_bgr()` 现在统一 `np.ascontiguousarray()` 拷一份。
    这个 bug 的触发路径很隐蔽: `paintGL` 上传纹理 → `_build_backdrop`
    → `_fallback_backdrop` → `frame_bgr` → `cv2.resize`。

55. **打包成 exe 时三个必须显式指定的东西** (见 `tools/build_exe.py`):
      - `--add-data win-duo.ico` —— 图标是数据文件, 静态分析发现不了;
      - `--add-data config.json` —— 当**模板**用, 首次启动复制到 exe 旁边
        (exe 里的 config 用户改不了, 不能直接用它当配置);
      - `--hidden-import bettercam --hidden-import dxcam` —— 它们是
        `__import__` 动态导入的, 漏了就直接失去 DXGI (165Hz → 37Hz)。
    验证 exe 不能只看"能启动", 至少要跑:
        dist\win-duo.exe --selftest --source camera    # 摄像头 + 抓屏
        dist\win-duo.exe --level 0.5 --glass --seconds 10   # 玻璃层真渲染
    然后翻它写出来的 `win_duo.log` 确认**异常条数为 0**。

56. **别在数据结构里存"没人读"的大图 —— 那是纯浪费的常驻内存。**
    `OrbTracker._samples` 的回访采样点原来存了 `{"angle", "des", "kp",
    "gray"}`, 那个 `gray` 是 `prepared.copy()` 的一份完整灰度帧。但
    `_visit_correction()` 只读 `des`/`kp`/`angle` —— **gray 从头到尾没人读过**。
    `MAX_SAMPLES=72`, 1280x720 灰度 = 0.88MB/帧, 于是**常驻白占 63MB**。
    现在采样点只存 `angle`/`des`/`kp` (共约 2.6MB)。
    教训: 往长期存活的容器里放东西之前, 先 grep 一下**有没有人读它**;
    "写进去但从不读"的字段会安静地占着内存, 跑久了才被发现。

57. **死字段/死方法要定期扫一遍。** 本次清掉的 (都已确认全项目无读取/调用):
      - `angles/camera.py::CameraAngleSource._last_gray` —— 只赋值, 从不读,
        还额外持有一帧灰度图;
      - `render/capture.py::CaptureWorker._misses` —— 只 `=0` / `+=1`, 从不读;
      - `render/overlay.py::GlassOverlay._backdrop_path_used` —— 写 3 处, 0 读取;
      - `ui/log_dialog.py::record_log()` + `_MEMORY_LOGS` —— 函数从没被调用,
        内存日志缓冲永远是空的, `get_all_logs()` 每次都得走磁盘那一路;
      - `angles/hub.py::SourceHub.restart()` —— 从没被调用 (controller 用的是
        `invalidate()` + `start_active()`);
      - 一批未使用的 import (`hub.KeyControl`、`main.os`、`overlay.QSurfaceFormat`
        等)。
    扫描办法: 用 `ast` 把每个模块的 import 名和 `FunctionDef` 名抽出来, 再在
    **全项目拼起来的文本**里数出现次数 (<=1 就是只有定义处)。注意排除
    `__init__.py` (可能是有意 re-export) 和 Qt 虚函数 (`paintGL`/`showEvent`
    等, 名字只出现一次是正常的 —— 靠框架回调)。

58. **config.json 必须能"凭空重建" —— 模板不能依赖一个会和它同归于尽的文件。**
    我踩的坑: `_seed_config_if_missing()` 原来只从 `resource_file("config.json")`
    复制模板。**源码模式下"模板"和"活配置"是同一个文件** (`<项目根>/config.json`)
    —— 用户把它删了, 模板也就没了, 于是 `if not src.exists(): return` 什么也没
    生成, 接着 `load_config` 打开不存在的文件直接崩:
        FileNotFoundError: ...\config.json
    表现是"双击后闪一个命令行窗口, 然后就没了" (pythonw 模式下连报错都看不到,
    因为 stdout 被重定向到日志, 而崩溃发生在日志建立之前/同时)。
    打包成 exe 时**不暴露** —— 模板在解包目录、活配置在 exe 旁边, 是两个文件。
    所以这个 bug 只在源码模式出现, 很容易漏掉。
    现在 `main.DEFAULT_CFG` 里**内置**一份出厂默认值当兜底, 并且 `load_config`
    对三种坏情况都做了处理:
      - 文件不存在      -> 用内置默认值生成 (生成不了就在内存里直接用);
      - JSON 解析失败   -> 备份成 `config.json.bad` 再重建 (用户能找回内容);
      - 缺键 (老版本)   -> `setdefault` 补齐**内存里的** cfg, **不重写文件**
        (不替用户动他的文件; 但缺键补齐是必须的, 否则 `cfg["新键"]` 会 KeyError)。
    底线: **任何情况下都要能启动**。配置坏了只是"恢复默认", 不该让程序打不开。

59. **默认热键要先确认没被别的程序占用。** `Ctrl+Alt+X` 当默认"匹配调试窗"键,
    实测在本机**被别的程序占着** (`RegisterHotKey` 返回 0), 于是键盘模式下那个键
    按了没反应, `tools/hotkey_test.py` / `tools/ui_test.py` 直接 FAIL。
    排查办法 (**`GetLastError` 必须 `use_last_error=True` 才准**, 见第 28 条):
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        r = u32.RegisterHotKey(None, id, MOD_CONTROL|MOD_ALT, vk)
        # r==0 时 ctypes.get_last_error()==1409 => ERROR_HOTKEY_ALREADY_REGISTERED
    实测 X / Z 常被截图或输入法类软件抢走, 现在默认用 **`Ctrl+Alt+G`**。
    注意 `hotkey_lines()` **只列注册成功的键**是刻意的 —— 注册失败时用户能立刻
    看出是"被占用", 而不是对着一个按了没反应的键发懵 (见第 17 条)。
    这类失败**先确认不是环境占用**, 别急着当代码 bug 改。

60. **优化内存先量"是谁占的", 别猜 —— 而且要先分清 WS 和私有。**
    用户报"内存 300 多 MB"。实测拆开 (逐步 import + 建对象, 用标记文件让外部
    按阶段采样; 注意 **venv 的 `python.exe` 是转发器**, 测到的 5MB 是空壳, 要
    取真正那个子进程):
        import main 24MB -> QApplication 37 -> apply_theme 76 (+39, qfluent 主题)
        -> AppController 111 (+35) -> SettingsPanel 180 (+69!) -> GL 窗口 +127
    两个真凶:
      - **`SettingsPanel` 启动就无条件创建, 白占 69MB** —— 托盘用户多数时候根本
        不打开设置窗口。改成**惰性创建** (`main.run_tray` 里 `_state["panel"]`,
        第一次 `show_settings()` 才建), 收尾时判 `is not None`。待机 180->110MB。
      - **BLAS 线程池。** 本机 32 逻辑核, `import numpy` 让 OpenBLAS 按核数开线程
        (线程 4->27, **私有内存 +740MB** 的保留栈)。本项目只算 3x3 矩阵 (Kabsch)
        和几百个特征点, 多线程纯属白占。在 `main.py` **import numpy 之前**
        `os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")` (+OMP/MKL/NUMEXPR/
        VECLIB)。**必须在那之前** —— BLAS 只在库加载时读一次。私有 1099->359MB。
    教训: **WS (工作集) 和私有内存是两回事**, 用户看的是 WS。降低私有内存不一定
    降 WS (保留未提交的地址空间本来就不算 WS)。报数字前先说清是哪个。

61. **GL 用过的内存要"销毁窗口 + 裁剪工作集"两招一起才能还回去。**
    实测 (2560x1600, tasklist 读数):
        只建 QApplication        39MB
        建 GL 窗口              133MB   (+94: NVIDIA 驱动 DLL 的文件映射)
        销毁窗口                132MB   (**几乎不降!**)
        再裁工作集                8MB   <- 关键
    那 94MB 是 `nvwgf2umx.dll`(81MB) / `nvgpucomp64.dll`(77MB) /
    `nvoglv64.dll`(41MB) 这些**驱动 DLL 的映射页** —— GL 上下文一加载就驻留,
    **销毁窗口不会卸载**。只有裁工作集
    (`SetProcessWorkingSetSize(-1,-1)` + `EmptyWorkingSet`) 才把它们挤出去。
    真实 app 里实测: **215MB -> 5MB**, 且稳定。
    三个坑, 我都踩了:
    - ⚠️ **必须用 `OpenProcess` 的真句柄, 不能用 `GetCurrentProcess()` 的伪
      句柄** (-1)。伪句柄在本机调这两个 API 直接失败 (`err=6`), 我因此一度
      以为"回收根本没用"。
    - ⚠️ **必须声明 `argtypes`/`restype`。** 不声明的话 `c_size_t(-1)` 会按默认
      int 传、在 64 位被截断 —— 调用直接崩, 而它在 `try/except` 里被吞掉, 只
      表现为"回收静默无效" (我为此白查了一轮)。
    - ⚠️ **回收要延迟 ~400ms 再做**, 不能在 `deleteLater()` 之后立刻调: 那时
      窗口/上下文还没销毁完, 而 `SetProcessWorkingSetSize` 是异步的 (只设目标),
      结果工作集几乎不动。用 `QTimer.singleShot(400, ...)`。
    低内存模式 (`low_memory_mode`, 默认关) = **要用才建 + 销毁后裁内存**:
    - **核心是"不建", 不是"建了再放"。** 待命 (浓度≈0) 时压根不建 GL 窗口,
      那 ~120MB 就不会被加载 —— 比"建-释放-再建"稳得多也省得多。
    - ⚠️ **建窗口必须防抖, 否则会反复创建/销毁 GL 上下文。** 我第一版"level
      一过阈值就建", 而摄像头噪声会偶尔冒尖峰 (level 0.005 这种) —— 实测
      25 秒内建了 **10 次**, 用户报"用一会儿就不正常了/像被自动关了"。
      现在要求 level **连续 `_RECREATE_CONFIRM`(2) 个周期**都过阈值才建
      (真实合盖持续 1 秒以上, 不会被误挡)。
    - ⚠️ **裁内存不能在玻璃层正用时做。** `reclaim_memory()` 会把驱动 DLL 的
      页从工作集挤出去, 紧接着要用就得缺页调回 —— 实测重绘从 ~28/s 掉到
      **4~10/s**。(我踩的坑: 释放窗口后 `QTimer.singleShot(400, ...)` 延迟回收,
      结果 400ms 后用户已经在合盖了, 回收正好插进来。) 现在 `reclaim_memory()`
      开头先判 `if self.overlay is not None: return False`。
    - **删除/重命名方法后要复查引用。** 我把 `_maybe_recreate_overlay` 并进
      `_maybe_release_overlay` 时, 编辑留下了一行**重复的
      `QTimer.singleShot(400, self.reclaim_memory)`** —— 回收被排两次。
      改完扫一遍重复行 (`Counter` 数长行) 能抓到。
    - 判据是"窗口在不在屏幕上", 不是 `glass_on` —— 默认 `autostart_glass=true`
      时玻璃层是"**开着待命**"的, 浓度 0 只是窗口被 `hide()` 了, `glass_on`
      恒为 True。拿它当判据就永远不释放。
    - **构造函数里就要应用** (`if cfg["low_memory_mode"]: _start_release_poll()`):
      只在"拨开关"那一刻起计时器的话, 用户开了开关**重启**后就没人应用了。
    - **释放后 `glass_on` 仍是 True**, 所以 `start_glass()` 开头
      `if self.glass_on: return` 会把重建挡掉 —— 要改成
      `if self.glass_on and self.overlay is not None: return`。
    - `prewarm_overlay()` 在这个模式下直接返回 False: 预热的目的就是"下次开启
      不卡", 和低内存模式目标相反。
    - **`tools/` 里的测试要显式关掉这个开关。** `ui_test` / `debug_window_test`
      都拷用户真实的 `config.json`, 用户开了这个模式后那些测试会踩空
      (比如 debug_window_test 要等摄像头 40 秒, 期间窗口被释放 -> `overlay`
      变 None -> 后面全崩)。它们测的不是内存策略, 所以 `cfg["low_memory_mode"]
      = False` (+ `ctl._stop_release_poll()` 停掉已在跑的定时器)。

62. **`_labeled()` 的标签列宽是统一常量 `_LABEL_W`, 不是各算各的。**
    它原来是写死的 `label_w=64`。而 4 字标签实测 56px 刚好放得下, **5 字的
    "低内存模式"是 70px** —— 在 64px 的框里右对齐时文字**从左边溢出去**,
    表现就是"这几个字怎么偏左了"。
    要点: (1) 宽度要够 (现在 78px, 覆盖最宽的 5 字标签); (2) 必须**所有行用
    同一个宽度**, 否则每行控件左边缘参差不齐。改字面量标签时先量一下
    `QFontMetrics(font).horizontalAdvance(text)`。

## 本机环境坑（会浪费你很多时间）

1. **Windows schannel 拿不到凭证。** 一切走 schannel 的 TLS 客户端都失败：
   - `curl.exe` → `curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`
   - `git.exe` → 同样报错，必须 `git -c http.sslBackend=openssl ...`
   **所以诊断 PyPI/网络连通性绝对不能用 curl**，要用 `tools/net_probe.py`
   （Python 的 ssl 走自带 OpenSSL，才是 pip 真实使用的栈）。
   它会让你误判成「网络不通」，实际网络是好的。

2. **注册表代理没有协议前缀**（`ProxyServer=127.0.0.1:10808`）。
   Python 3.9 的 `getproxies_registry()` 把它展开成
   `{'https': 'https://127.0.0.1:10808'}`，即谎称代理是 TLS 代理；
   pip 自带的 urllib3 1.26 在 TLS-in-TLS 上崩：
   `ValueError: check_hostname requires server_hostname`。
   `tools/setup_env.ps1` 显式设置 `HTTP_PROXY=http://127.0.0.1:10808`
   让 `getproxies_environment()` 压过注册表值。国内用清华源。

3. **摄像头不是"不存在"，是代理进程没有权限。** 别再重复我这个误判：
   - `Get-PnpDevice -Class Camera` 返回空 → **这个查询本身被拒绝了**
     （`WBEM_E_ACCESS_DENIED`），不是没有设备。
   - 用 `pnputil /enum-devices /class Camera` 才能查到：
     `USB\VID_3277&PID_0029` = "USB2.0 HD UVC WebCam"，`Started`。
   - 根因：代理进程的令牌被大幅削弱 —— `whoami /priv` 只有 **1 个特权**，
     `BUILTIN\Administrators` 是 *deny only*，DirectShow/MSMF 枚举到 0 个设备。
   - 所以**代理进程里测摄像头可能永远失败**，这不能用来判断用户机器。
     让用户自己跑 `tools/camera_probe.py`。
   - 后来会话权限放宽（令牌恢复到正常特权数）后摄像头立刻可用，进一步证实
     这就是纯权限问题。判断硬件有无, 永远不要用代理进程的枚举结果。

4. **Windows 上 opencv 的摄像头后端要回退。** `cv2.VideoCapture(0)` 默认走 MSMF，
   对通用 UVC 摄像头经常打不开或读不到帧。`angles/camera.py::open_camera` 按
   `dshow → msmf → any` 依次试。

5. **虚拟摄像头会给出冻结帧，必须检测（本机实测踩到）。** 取流成功的条件不只是
   `isOpened()` + 读到帧，还必须**画面真的在变**（`_is_live`）。本机 index=0
   配合 DSHOW 能打开、能读到 1280x720 的帧，但相邻帧平均差恒为 0.0000 ——
   是摩托罗拉 "Smart Connect Camera" 虚拟摄像头。测角算法拿冻结帧永远返回 0，
   表现是"开合上盖毫无反应"，只看 `isOpened()` 完全发现不了。
   真实传感器即使静止也有噪点（实测差异 5~9），冻结帧恒为 0，判据很干净。

   而且 **index↔设备的对应关系随后端而变**：本机 index=0+dshow 是冻结的虚拟
   摄像头，index=0+msmf 才是真摄像头；index=1 则反过来。所以不要"固定某个
   index+后端"就以为稳了，`auto` + 冻结帧检测才是对的。

6. **务必限制 OpenCV 线程数（`opencv_threads`，默认 2）。** 本机 OpenCV 默认开到
   **32 个线程**，而它的并行 for 是**自旋等待**的，多出的线程纯属空转烧 CPU。
   实测（90 帧，nfeatures=1200）：
   - 32 线程 → 墙钟 2.94s, CPU **267%**
   - 2 线程  → 墙钟 3.01s, CPU **86%**
   耗时只多 2%，省下约 1.8 个核。摄像头模式整体 CPU 从 257% 降到 70%。

7. **别被 `backend is generally available but can't be used to capture by index`
   误导。** 看 cap.cpp 源码，这句是在**所有后端都打开失败之后**才打印的兜底信息
   （`if (apiPreference != CAP_ANY) { if (isBackendBuiltIn(...)) CV_LOG_WARNING(...) }`），
   它并不表示"该后端不支持按序号取流"。真实含义是"全试过了，都不行"。

8. **`config.json` 用 `utf-8-sig` 读。** PowerShell 5.1 的 `Set-Content -Encoding UTF8`
   和记事本都会写 **BOM**，用 `utf-8` 读会抛 `Unexpected UTF-8 BOM`。
   已经踩过一次（我用 Set-Content 改配置，把 config.json 写坏了）。
   改 JSON 一律用 write/edit 工具，别用 PowerShell 的 Set-Content。


## 验证规范（重要）

**不要靠肉眼看图判断渲染对不对。** 本仓库所有验证工具都输出数值结论 +
PASS/FAIL 退出码：

```powershell
.venv\Scripts\python.exe tools\capture_backend_test.py     # 抓屏后端: 速度/通道/帧号推进
.venv\Scripts\python.exe tools\offscreen_test.py           # 着色器 6 项
.venv\Scripts\python.exe tools\tracker_test.py             # 测角算法 (合成帧)
.venv\Scripts\python.exe tools\fov_probe.py                # 合成视场的适用上限
.venv\Scripts\python.exe tools\camera_probe.py             # 摄像头: index×后端 + 冻结帧
.venv\Scripts\python.exe tools\camera_track_test.py        # 真机静止稳定性
.venv\Scripts\python.exe tools\camera_frame_test.py        # 画面是否真的在动
.venv\Scripts\python.exe tools\bench_shader.py             # 着色器真实分辨率性能
.venv\Scripts\python.exe tools\ui_test.py                  # GUI 链路 (托盘/开关/参数/自启/落盘)
.venv\Scripts\python.exe tools\debug_window_test.py        # 调试窗能开、能关、状态同步
.venv\Scripts\python.exe tools\icon_test.py                # 图标: 应用级/窗口/托盘/兜底
.venv\Scripts\python.exe tools\hotkey_test.py              # 全局热键 (会真的模拟按键)
.venv\Scripts\python.exe main.py --selftest                # 角度源 + 截屏
.venv\Scripts\python.exe main.py --seconds 5               # 托盘模式跑 5 秒
.venv\Scripts\python.exe main.py --smoke --source manual   # 真窗口跑 2 秒
.venv\Scripts\python.exe tools\check_smoke.py              # 分析上面那帧
```

**批量回归时每步之间要 `Stop-Process` + 等 1 秒** (摄像头是独占设备),
不然会出现"单独跑都过、批量跑有一两个失败"的假故障。

**测性能别靠直觉。** 用户报"卡顿"时先跑 `bench_shader.py` 和 `--seconds N`
（会打印进程 CPU 占比）定位，别猜。已经因此误判过一次：
卡顿的真因是玻璃层常驻显示 3Hz 桌面快照，而不是着色器。

**改着色器后必须跑 `offscreen_test.py`，尤其第 [6] 项。** 它把 windowsduo 的
**原版着色器逐字内嵌**做对照，要求 `outside=black` 时输出与原版**逐像素一致**
（最大差 0）。这是「合并没有偷偷改坏原本动画」的唯一证据。
如果你确实要改原版效果，那就同时更新 `ORIGINAL_FS` 并在 README 里说明。

## 提交约定

- `.gitignore` 排除 `.venv/`、`__pycache__/`、`smoke_*.png`、`desk_bg.png`。
  不要把这些加回来。
- 提交信息用中文，说明改动动机。
