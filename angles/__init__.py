"""角度源: 把「上盖开合角」的三种测法归一成同一个量。

    level ∈ [0, 1]      0 = 完全展开(清晰)   1 = 完全合上(最虚)

- camera : 笔记本上盖摄像头 ORB 特征匹配 (独立实现)
- serial : ESP32 + MPU6050 串口 JSON (移植自 windowsduo)
- manual : 键盘直接给浓度

渲染层只认 level, 所以换角度源不影响动画; 三者测的本来就是同一个物理量
(上盖相对底座的夹角), 只是传感器不同。
"""
from .base import AngleSource
from .hub import SourceHub

__all__ = ["AngleSource", "SourceHub"]
