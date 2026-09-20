# win-duo

折叠屏「悬浮玻璃」开合动画。合上笔记本上盖，屏幕内容随开合沉入磨砂玻璃；打开则由虚到实。

- **测角**：用笔记本上盖摄像头做 ORB 特征匹配实时量开合角，不需要额外硬件（独立实现）。
  也支持 ESP32 串口和键盘手动两种角度源。
- **动画**：PyQt6 + GLSL 逆投影「悬浮玻璃」——铰链在屏幕底边，间隙越大越模糊、越暗。
  动画效果衍生自 [WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo)。

三种角度源都归一到同一个浓度 `level ∈ [0,1]`（0 = 完全展开清晰，1 = 完全合上最虚），
渲染层不关心背后是哪种传感器。

## 下载

从 [Releases](https://github.com/Angel-GP/win-duo/releases) 下载 `win-duo.exe`，双击即用，
不需要装 Python。程序默认只驻留系统托盘，不弹窗。

`config.json` 会在 exe 旁边自动生成；换机器用把 `win-duo.exe` 和 `config.json` 一起拷走即可。

## 从源码运行

```powershell
# 建 venv 装依赖（工作区内，不污染系统 Python）
powershell -ExecutionPolicy Bypass -File scripts\setup_env.ps1

# 默认进托盘（玻璃层按配置自动开）
.venv\Scripts\python.exe main.py

# 立刻开玻璃层 / 打开设置窗口 / 钉住某个浓度看效果
.venv\Scripts\python.exe main.py --glass
.venv\Scripts\python.exe main.py --panel
.venv\Scripts\python.exe main.py --level 0.5
```

## 界面与托盘

程序启动后只驻留托盘。**双击托盘图标**或**右键 → 设置...** 打开设置窗口：

| 托盘菜单 | 作用 |
|---|---|
| 开启 / 关闭玻璃层 | 主开关，关闭时释放摄像头/串口 |
| 设置... | 打开设置窗口 |
| 角度源 ▸ | 摄像头 / ESP32 串口 / 键盘手动 |
| 开机自启 | 写 HKCU Run 键，用 `pythonw.exe` 启动 |
| 退出 | 关掉程序 |

设置窗口走 Fluent Design，常用项一眼可见，其余收进「▸ 高级设置」。**改动即时生效并
自动保存**，没有保存按钮。**最小化**收进托盘，**点 X** 直接退出程序。

> 没装 `PyQt6-Fluent-Widgets` 也能跑：`ui/widgets.py` 会自动退回普通 Qt 控件 + QSS。

## 屏幕被玻璃层盖住怎么办

**按 `Ctrl + Alt + Shift + Esc`** —— 立刻关掉玻璃层并退出程序。这是系统级热键，
不依赖焦点、托盘或控制台，任何情况下都有效。

日常开关玻璃层用 **`Ctrl + Alt + D`**。

## 热键

热键**只在「键盘」角度源下生效**（摄像头模式是全自动跟手的，只保留紧急关闭键，
避免误触和自动跟踪打架）。

| 热键 | 键盘模式 | 摄像头模式 |
|---|---|---|
| `Ctrl+Alt+↑` / `↓`　浓度 ±5% | ✅ | — |
| `Ctrl+Alt+→` / `←`　100% / 0% | ✅ | — |
| `Ctrl+Alt+G`　匹配调试窗 | ✅ | — |
| `Ctrl+Alt+D`　开关玻璃层 | ✅ | — |
| `Ctrl+Alt+Shift+Esc`　紧急关闭 | ✅ | ✅ |

热键都能在 `config.json` 里改（`ctrl+alt+d` 这种写法）。切换角度源时会自动重新
注册/注销，设置窗口里显示的永远是当前生效的键。

## 摄像头模式

1. 摄像头必须是**上盖自带、会随开合转动**的那颗，外接固定摄像头测不到角度。
2. 让摄像头对着**有纹理的静止场景**（书架、墙），别对着纯白墙或自己。
3. 上盖**完全展开**时标定基准帧（设置窗口里有按钮，启动后也会自动标定一次）。
4. 慢慢开合上盖，画面就会跟着折叠；方向反了用「翻转方向」，幅度不合适调 `camera_scale`。

打不开摄像头时程序**只提示、不自动切换角度源**（`auto` 后端会自动跳过虚拟摄像头的
冻结帧）。可在设置窗口点「扫描」逐个探测可用的摄像头。

## 性能说明

- 玻璃层是全屏置顶窗，画的是每秒截若干次的桌面快照。所以浓度≈0 时会**整个隐藏**，
  让真实桌面露出来，避免"打开后电脑变卡"。
- 关掉玻璃层会**停掉角度源**（摄像头常驻要吃约 1/3 个核）。
- 抓屏优先走 DXGI（`bettercam`），装不上自动退回 mss（帧率上限会降低，功能不受影响）。
- 可选**低内存模式**（设置 → 高级 → 调试，或 `--low-memory`）：待命时不建 GL 窗口并裁剪
  工作集，把常驻内存降到几 MB；代价是合盖时要现建窗口，有约 0.4 秒延迟。默认关。

## 常用配置

`config.json` 是唯一参数入口。常用项：

| 键 | 含义 |
|---|---|
| `source` | 默认角度源 `camera` / `serial` / `manual` |
| `autostart_glass` | 启动时自动开玻璃层，默认 `true` |
| `camera_index` / `camera_backend` | 摄像头序号 / 取流后端（`auto` 会跳过冻结帧） |
| `camera_scale` / `camera_sign` | 灵敏度 / 方向 |
| `max_tilt_deg` / `eye_dist_h` / `blur_spread` | 玻璃最大转角 / 眼距 / 模糊强度 |
| `outside_mode` | `black`（纯黑）/ `backdrop`（背景图兜底，无黑场） |
| `refresh_hz` / `render_fps` | 重截频率上限（≤ 显示器刷新率）/ 玻璃层重绘上限 |
| `low_memory_mode` | 低内存模式，默认 `false` |
| `hotkey_toggle` / `hotkey_off` 等 | 各热键绑定 |

常用命令行参数：`--glass` / `--panel` / `--source` / `--level N` / `--screen N` /
`--camera N` / `--outside black|backdrop` / `--low-memory` / `--selftest`。

## 打包成 exe

推送到 GitHub 后由 [Actions 工作流](.github/workflows/build.yml) 在 Windows runner 上自动打包，
产物是名为 `win-duo` 的 artifact；打 `v*` tag 时还会自动发布到 Releases。

本地打包：

```powershell
.venv\Scripts\python.exe scripts\build_exe.py            # 单文件（约 94 MB）
.venv\Scripts\python.exe scripts\build_exe.py --onedir   # 目录版，启动更快
```

无窗口自检（角度源 + 截屏链路）：

```powershell
.venv\Scripts\python.exe main.py --selftest
```

## 许可

**GPL-3.0**，见 [LICENSE](LICENSE)（含完整许可证正文）。因为依赖 PyQt6（GPL-3.0-only）
与 PyQt6-Fluent-Widgets（GPL-3.0），整个衍生作品必须跟着 GPL-3.0。

动画渲染衍生自 [WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo)（MIT），
其版权声明与完整第三方依赖清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
