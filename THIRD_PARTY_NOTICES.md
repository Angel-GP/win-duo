# 第三方组件与许可证声明

win-duo 以 **GPL-3.0** 发布（正文见 [LICENSE](LICENSE)）。**分发本程序时必须一并保留本文件。**

## 依赖

| 组件 | 许可证 | 用途 |
|---|---|---|
| [PyQt6](https://pypi.org/project/PyQt6/) | **GPL-3.0-only** | 窗口、托盘、事件循环、OpenGL 窗口 |
| [Qt 6](https://www.qt.io/) | LGPL-3.0 | Qt 运行时（动态链接） |
| [PyQt6-Fluent-Widgets](https://qfluentwidgets.com/) | **GPL-3.0** | Fluent 界面控件（缺失时退回普通 Qt + QSS） |
| [PyQt6-Frameless-Window](https://pypi.org/project/PyQt6-Frameless-Window/) | GPL-3.0 | 无边框窗口（上者依赖） |
| [PyOpenGL](https://pyopengl.sourceforge.net/) | BSD-3-Clause | GLSL 着色器、纹理上传 |
| [NumPy](https://numpy.org/) | BSD-3-Clause | 帧缓冲处理、位姿解算 |
| [opencv-python](https://github.com/opencv/opencv-python) | Apache-2.0 | ORB 匹配、摄像头取流、图像处理 |
| [mss](https://github.com/BoboTiG/python-mss) | MIT | 桌面截屏（GDI 兜底） |
| [bettercam](https://github.com/Blaze396/bettercam) / [dxcam](https://github.com/ra1nty/DXcam) | MIT | 桌面截屏（DXGI 首选，缺失退回 mss） |
| [comtypes](https://github.com/enthought/comtypes) | MIT | bettercam 的 COM 依赖 |
| [windows-capture](https://github.com/qlands/python-windows-capture) | MIT | 桌面截屏（WGC 后端，优先于 DXGI） |
| [pyserial](https://github.com/pyserial/pyserial) | BSD-3-Clause | ESP32 串口角度源 |
| [darkdetect](https://github.com/albertosottile/darkdetect) | BSD-3-Clause | 跟随系统主题（传递依赖） |
| [pywin32](https://github.com/mhammond/pywin32) | PSF-2.0 | qfluentwidgets 的 Windows 依赖 |

> 因为依赖 GPL-3.0 的 PyQt6 与 PyQt6-Fluent-Widgets（强传染性 copyleft），
> 本项目作为衍生作品必须整体以 GPL-3.0 发布。

## 上游代码

`render/shader.py`、`render/overlay.py` 的逆投影「悬浮玻璃」渲染衍生自
[WindowsDuo](https://github.com/KaedeharaKazuha1029/WindowsDuo)（MIT）。
MIT 要求保留版权与许可声明，故其原文逐字保留如下：

```
MIT License

Copyright (c) 2026 KaedeharaKazuha1029

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

其余代码（摄像头 ORB 测角、DXGI 截屏、界面、串口角度源等）为本项目自写。
