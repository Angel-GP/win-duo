# 第三方组件与许可证声明

win-duo 使用了以下开源软件。**分发本程序时必须一并保留本文件。**

本文件分三部分：

- [A. 运行时依赖](#a-运行时依赖) —— 程序跑起来需要的库
- [B. 可选依赖](#b-可选依赖) —— 缺了也能跑，只是某些功能退化
- [C. 上游项目](#c-上游项目) —— 代码来源

---

## A. 运行时依赖

| 组件 | 版本 | 许可证 | 在本项目里的用途 |
|---|---|---|---|
| [PyQt6](https://pypi.org/project/PyQt6/) | ≥6.5 | **GPL-3.0-only** | 窗口、托盘、事件循环、OpenGL 窗口 |
| [Qt 6](https://www.qt.io/) (PyQt6-Qt6) | 6.10.x | **LGPL-3.0** | Qt 运行时本体 |
| [PyQt6-sip](https://pypi.org/project/PyQt6-sip/) | ≥13.8 | BSD-2-Clause | PyQt6 的绑定生成器 |
| [PyQt6-Fluent-Widgets](https://qfluentwidgets.com/) | ≥1.7 | **GPL-3.0** | Fluent Design 界面控件 |
| [PyQt6-Frameless-Window](https://pypi.org/project/PyQt6-Frameless-Window/) | ≥0.8 | **GPL-3.0** | 无边框窗口（上者的依赖） |
| [darkdetect](https://github.com/albertosottile/darkdetect) | ≥0.8 | BSD-3-Clause | 跟随系统明暗主题（上者的依赖） |
| [PyOpenGL](https://pyopengl.sourceforge.net/) | ≥3.1.6 | BSD-3-Clause | GLSL 着色器与纹理上传 |
| [NumPy](https://numpy.org/) | ≥1.24 | BSD-3-Clause | 帧缓冲处理、位姿解算 |
| [opencv-python](https://github.com/opencv/opencv-python) | ≥4.8,<5 | Apache-2.0 | ORB 特征匹配、摄像头取流、图像处理 |
| [mss](https://github.com/BoboTiG/python-mss) | ≥9.0 | MIT | 桌面截屏（GDI BitBlt，兜底方案） |
| [bettercam](https://github.com/Blaze396/bettercam) | ≥1.0 | MIT | 桌面截屏（DXGI Desktop Duplication，首选） |
| [comtypes](https://github.com/enthought/comtypes) | ≥1.4 | MIT | bettercam 的 COM 依赖 |
| [Pillow](https://python-pillow.org/) | ≥10.0 | MIT-CMU | 读背景图、生成 `.ico`、测试脚本分析画面 |
| [pyserial](https://github.com/pyserial/pyserial) | ≥3.5 | BSD-3-Clause | ESP32 串口角度源 |

此外还有两个 Windows 平台上的传递依赖（本项目代码不直接调用）：

| 组件 | 许可证 | 说明 |
|---|---|---|
| [pywin32](https://github.com/mhammond/pywin32) | PSF-2.0 | `qfluentwidgets` 在 Windows 上依赖 |
| [setuptools](https://github.com/pypa/setuptools) | MIT | `pkg_resources` 运行时 |

## B. 可选依赖

缺了不会崩，只是对应功能退化。**本程序对二者都做了运行时降级**。

| 组件 | 版本 | 许可证 | 缺失时的行为 |
|---|---|---|---|
| [PyQt6-Fluent-Widgets](https://qfluentwidgets.com/) | ≥1.7 | GPL-3.0 | 退回普通 Qt 控件 + 自带 QSS（`WIN_DUO_NO_FLUENT=1` 可强制走这条路径） |
| [bettercam](https://github.com/Blaze396/bettercam) | ≥1.0 | MIT | 退回 [mss](https://github.com/BoboTiG/python-mss)（截屏上限从显示器刷新率降到约 37 Hz） |
| [dxcam](https://github.com/ra1nty/DXcam) | ≥0.0.5 | MIT | bettercam 的备选 DXGI 实现，两者都没有才退回 mss |

---

## C. 上游项目

### C.1 `windowsduo` —— **MIT** ✅

- 仓库：<https://github.com/KaedeharaKazuha1029/WindowsDuo>
- 许可：MIT License，Copyright (c) 2026 KaedeharaKazuha1029
- 用途：**动画效果的来源**。`render/shader.py`、`render/overlay.py`、
  `render/capture.py` 的逆投影"悬浮玻璃"渲染方式移植自它。

其原始版权声明已逐字保留在 [LICENSE](LICENSE) 中（MIT 要求保留）。

`windowsduo` 自己的着色器又是以下项目的融合复刻，**均为 MIT**：

| 仓库 | 许可证 |
|---|---|
| [elijah-semyonov/DuoLikeAnimation](https://github.com/elijah-semyonov/DuoLikeAnimation) | MIT |
| [chuspeeism/iphone-duo](https://github.com/chuspeeism/iphone-duo) | MIT |
| [DhananjayBhosale/MacDuo](https://github.com/DhananjayBhosale/MacDuo) | MIT |
| [askmaddyy/FrostFold](https://github.com/askmaddyy/FrostFold) | MIT |

### C.2 `angles/camera.py` —— **本项目独立实现**

摄像头测角（用笔记本上盖摄像头 ORB 特征匹配量开合角）是本项目独立实现的，
数学路线自成一套：

| 环节 | 本项目做法 |
|---|---|
| 求旋转 | 归一化平面 **Kabsch/SVD**（3 自由度） |
| 取角度 | 投影到**铰链轴**上 |
| 抗漂移 | 按角度采样 + **回访绝对校正** |
| 内点判据 | **角度**残差 |

精度：稳态误差 **0.02°**、往返归零 **0.02°**（`tools/tracker_test.py` 合成帧实测）。

`angles/serial_source.py`（ESP32 串口角度源）同为本项目自写。

---

## D. 本项目的许可证

由于依赖 **PyQt6（GPL-3.0-only）** 与 **PyQt6-Fluent-Widgets（GPL-3.0）**，
本项目作为衍生作品**整体以 GPL-3.0 发布**。详见 [LICENSE](LICENSE)。

如果你想改用更宽松的许可证（例如 MIT），需要同时满足：

1. 把 PyQt6 换成 [PySide6](https://www.qt.io/qt-for-python)（LGPL-3.0，可动态链接）；
2. 移除 `PyQt6-Fluent-Widgets` 依赖 —— 代码已经支持，设 `WIN_DUO_NO_FLUENT=1`
   即走普通 Qt + QSS 的界面路径，该路径有回归测试覆盖
   （`tools/ui_test.py`）；
3. 从 [LICENSE](LICENSE) 里删掉上游 WindowsDuo 的 MIT 版权声明段
   （MIT 允许这么做，但**只在不再包含其衍生代码时**才成立 —— 也就是说你得
   重写着色器，或用别的方式实现那个逆投影效果）。

---

## E. 代码来源的实际情况

本仓库**没有 vendored（内嵌）任何第三方库的源码** —— 所有第三方组件都通过
`requirements.txt` 以独立依赖形式引入。

但有两处是**衍生代码**，必须保留上游声明：

| 文件 | 衍生的上游 | 许可证 | 实测相似度 |
|---|---|---|---|
| `render/shader.py`（GLSL 部分） | WindowsDuo | MIT | **87%** |
| `render/overlay.py` | WindowsDuo | MIT | 17% |
| `render/capture.py` | 无（本项目自写） | — | 2% |
| `angles/camera.py` | 无（本项目自写） | — | — |
| `angles/serial_source.py` | 无（本项目自写） | — | — |

**只有 `render/shader.py` 是实质性衍生**（87%，最长连续相同 198 字符），必须
保留 MIT 声明。`render/overlay.py` 相似度 17%（最长 22 字符）——窗口标志、
GL 初始化这些必然写法占了大部分，属于"看过它之后自己写的"。

`render/capture.py`、`angles/serial_source.py` 与对应上游几乎没有重合，是本项目
为解决实际问题新写的（DXGI 抓屏、冻结帧检测、后端回退、串口角度源）。

前两行是 MIT，**允许**衍生，只要保留版权声明 —— 完整 MIT 原文在
[LICENSE](LICENSE) 末尾，请勿删除。

最后一列是实测的文本相似度，用 `difflib.SequenceMatcher` 算的。列出来是为了
让"哪些是衍生、哪些是重写"有据可查，而不是靠声称。

另外 `tools/offscreen_test.py` 里内嵌了 WindowsDuo 的**原版 GLSL 着色器**用于
逐像素比对（第 [6] 项断言），同属 MIT 代码，版权归 WindowsDuo 作者所有。
