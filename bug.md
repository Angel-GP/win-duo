# win-duo 代码审计 — Bug 清单

审计范围：`win-duo/` 下的项目源码（不含 `.venv`、`build`、`dist`）。
所有源文件均通过 `py_compile`，无语法错误。

> 备注：本机 `.venv\Scripts\python.exe` 实际解析到系统 Python 3.9（`Python39`），
> 并非隔离的虚拟环境 —— 建议单独核实，但不影响下述源码分析。

按严重程度分组。

---

## 〇、已修复 — 选副屏却仍捕获主屏（本次修复）

**根因（`render/capture.py`）**：DXGI 后端在 `_DxgiSource._open_matching` 里
**只按分辨率**挑 output（`if not size or wh == tuple(size)`），命中第一块就返回。
分辨率相同的双屏永远命中第一块（通常是主屏）。更关键的是 `CaptureWorker.set_region`
**只改了 `self.region`**，而 DXGI 相机在打开时就绑定了某块 output —— 改 region 对它
毫无作用，于是"在设置里选了副屏，画面还是主屏"。（mss 后端用 region 直接抓，反而没这问题。）

**修复**：
1. `region_for` 现在带上桌面左上角坐标；`_DxgiSource` 接受 `origin`，在分辨率匹配的
   多块屏里选 origin **最接近**目标的那块（用"最近"容忍逻辑/物理坐标的缩放偏差）。
2. `CaptureWorker.set_region` 换屏时置 `_reopen_pending`，采集线程在两帧间的安全点
   （`_maybe_reopen`）**重挑 DXGI output** —— 不在主线程直接 release，避免踩到 native
   崩溃。挑不到就退回 mss。

已在本机双屏（2560x1600 主屏 + 1920x1080 副屏）验证：Qt[0]→output 0、Qt[1]→output 1；
反复切屏 6 次（含连续选同一屏）并每次真抓帧，均返回目标屏分辨率、未踩到 bettercam
单例已释放对象。

**本次修复的已知残留（可接受）：**
- dxcam 后端无法区分**同分辨率**双屏：`_cam_origin` 只从 bettercam 的
  `_output.desc.DesktopCoordinates` 取坐标，dxcam 结构不同会返回 `None`，`dist` 恒为 0，
  退化成"按分辨率选第一块"。bettercam 是首选后端，dxcam 只是退路，影响很小。
- 走 mss 回退时仍受下面 #1 影响（本次没动 mss 路径）。

---

## 一、功能性 bug（会导致行为错误）

### 1. `ui/monitors.py::region_for` — 多显示器 + DPI 缩放时截屏区域错位（mss 路径）

```python
return {"left": g.x(), "top": g.y(),                    # 逻辑坐标
        "width": max(1, int(g.width() * dpr)),          # 物理像素
        "height": max(1, int(g.height() * dpr))}
```

`left/top` 用的是 Qt 的**逻辑坐标**，而 `width/height` 乘了 `dpr` 变成**物理像素**，
两者坐标系不一致。mss 的 `grab(region)` 要求四个字段坐标系统一。主屏在原点 (0,0)
时看不出问题，但在**带缩放的副屏**上（left/top 非零），原点没换算成物理坐标，
截屏区域会整体偏移/错位。DXGI 路径正好绕过了 region（按 output 抓整屏），所以只有
退回 mss 时才暴露 —— 更难自查。

### 2. `ui/controller.py::_on_hotkey` — `calibrate`/`flip` 是死分支

```python
elif name == "calibrate":
    self.calibrate_camera()
elif name == "flip":
    self.flip_camera_sign()
```

`HOTKEY_DEFS` 里已经没有 `calibrate` / `flip` 这两个名字（注释说"标定和翻转改用
设置窗口按钮"），所以这两个分支永远不会被触发。不是崩溃，属于重构遗留的死代码，
容易误导后续维护者以为热键还在。

### 3. `ui/panel.py::_on_camera` — 忽略了 `camera_backend` 配置

```python
self.controller.set_camera(int(data), "auto")
```

用户在下拉框换摄像头 index 时，后端被**硬编码成 `"auto"`**，把 config.json 里
用户/扫描确定的 `camera_backend` 覆盖掉了。如果用户特意选了 `dshow`/`msmf`，
这里会把它抹回 auto。

---

## 二、资源/生命周期隐患

### 4. `main.py::_TeeLogger` — 日志文件只追加、从不轮转、从不关闭

`open(log_path, "a", ...)` 以追加模式打开，没有任何大小上限或轮转策略。
开机自启常驻场景下 `win_duo.log` 会**无限增长**。文件句柄也从没 `close()`
（虽然进程退出时系统回收，但缺少 flush/rotate 策略）。

### 5. `angles/hub.py::stop_all` 与 `main.run_direct` — KeyControl 线程没被停

`SourceHub.stop_all()` 只遍历 `self._sources`，而 `manual` 源（`self.control`）
不在其中。`main.run_direct` 的 `finally` 里也只调 `hub.stop_all()` + `cap.stop()`，
没调 `control.stop()`。键盘线程阻塞在 `msvcrt.getwch()` 上靠 daemon 随进程回收 ——
能用，但退出路径不干净，且与 `controller.shutdown()`（同样没停 control）不一致。

### 6. `angles/camera.py::stop` — join 超时后的重启竞态

```python
th, self._thread = self._thread, None
if th is not None:
    th.join(timeout=0.6)
if th is None or not th.is_alive():
    ... release tracker
```

设备还在"正在打开"阶段（`open_camera` 可能阻塞到 ~20s）时 join 会超时，此时
`_thread` 已被置 None 但旧 daemon 线程仍活着。若紧接着再 `start()`，会创建
**第二个线程、抢占第二次设备打开**。属于边缘竞态，正常操作难触发，但
invalidate/重建摄像头的快速连续操作有风险。

---

## 三、健壮性/一致性问题（低危）

### 7. `ui/panel.py::_refresh_status` — `_loading` 标志没有 try/finally 保护

```python
self._loading = True
self.sw_glass.setChecked(...)
self.num_level.setValue(...)
self._refresh_refresh_max()
self._loading = False
```

中间任一句抛异常，`_loading` 会**永久卡在 True**，之后所有用户交互
（`if self._loading: return`）都被静默吞掉，界面像"死了"。其它地方（`refresh_all`）
都用了 try/finally，唯独这里没有。而且这个函数每 500ms 跑一次，还重复做了一遍
`_refresh_glass` 里已经做过的三件事。

### 8. `angles/serial_source.py` — `stop()` 无条件置 None + `_error`/`_status` 无锁写

`stop()` 里 `self._thread = None` 不管 `join` 是否真的成功；异常处理里
`self._error = ...` 和 `_set_status` 外的直接赋值没走 `self._lock`
（其它读写都加了锁）。都是低概率数据竞争。

### 9. `main.py::apply_args` — `--tray` 参数无效果

`ap.add_argument("--tray", ...)` 定义了但 `apply_args`/`main` 从不读 `args.tray`。
托盘是默认行为所以没坏，但这个 flag 是纯装饰。

### 10. `render/capture.py::_DxgiSource.grab` — docstring 与实现不符

docstring 写"返回 ... RGBA ndarray"，但 `COLOR = "BGRA"`，实际全链路走的是 BGRA
（`paintGL` 里 `fmt != "RGBA"` 分支、`frame_bgr` 的切片都按 BGRA 处理）。
**行为是对的**（颜色不会反），只是注释过时，会误导。

### 11. `render/overlay.py::pick_backdrop` — finally 里无条件 `self.show()`

选背景图时先 `hide()`，`finally` 里 `self.show()`。如果此刻玻璃层本应是
`suppressed` 或 `enabled=False` 状态，这里会把它强行显示出来，与 tick 的显隐逻辑
短暂打架（下一 tick 会纠正，但有一帧闪现）。

### 12. `ui/controller.py::set_screen` — 换屏后没同步刷新率（二次审计新增）

`set_screen` 更新了截屏区域和 overlay 几何，但没更新 `self.capture.display_hz`，
overlay 的 `capture_hz` 也仍是旧屏的值。两块屏刷新率不同时（如 165Hz 主屏 + 60Hz
副屏），重截频率上限不会跟着变。只影响性能调度，不影响画面正确性。
建议：`set_screen` 里更新 `capture.display_hz` 并调 `overlay.apply_config()`。

### 13. `ui/controller.py::set_screen` — 换屏瞬间可能闪一帧黑（二次审计新增）

`capture.set_region` 会把 `frame=None`、`overlay.set_screen` 把 `_uploaded_seq=-1`，
在下一帧到达前 `paintGL` 会 `glClear` 成黑。换屏没走 `start_glass` 里"等首帧就绪再
显示"（`_show_overlay_when_ready`）的逻辑，合成器抓到那一帧会闪一下黑。
建议：换屏后同样延迟到首帧就绪再重绘。

---

## 四、说明（非 bug）

- `LOCK_AT_CLOSE`（overlay.py）常量恒为 `False`，相关锁屏代码不可达 —— 设计上的
  开关，不算 bug。
- `angles/camera.py` 里 `_cal_request` / `_want_debug` 等跨线程布尔标志无锁读写 ——
  在 CPython 下单个布尔赋值是原子的，实践上安全。

---

## 优先级建议

建议优先处理用户能实际感知到的：

1. ~~选副屏仍捕获主屏~~ —— 已修复（见〇）
2. **#1** mss 路径截屏区域错位（多显示器 + 缩放）
3. **#3** 摄像头后端被硬编码覆盖
4. **#4** 日志无限增长
5. **#7** `_loading` 异常后卡死界面
6. **#13** 换屏闪黑（用户可感知，但只在切换那一瞬）
