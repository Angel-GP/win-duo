"""角度源统一接口。"""


class AngleSource:
    """所有角度源的基类。

    子类只需实现 level() / status(); start()/stop() 可选。
    level() 返回 None 表示当前没有可用数据 (调用方应保持上一帧的值)。
    """

    name = "base"
    #: 供 HUD 显示的额外字段, 例如 {"pitch": 12.3, "matches": 240}
    detail = {}

    def start(self):
        """启动采集 (开摄像头 / 连串口 / 起线程)。失败不应抛异常。"""

    def stop(self):
        """停止采集并释放资源。"""

    def level(self):
        """返回 0..1 的浓度; 无数据返回 None。"""
        return None

    def status(self):
        """返回一行人类可读的状态, 用于 HUD。"""
        return ""
