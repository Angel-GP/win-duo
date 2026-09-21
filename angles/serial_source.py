"""ESP32 + MPU6050 串口角度源 -- 移植自 windowsduo/win/glass_overlay.py 的 AngleReader。

固件 100Hz 吐一行 JSON:  {"a": 主轴角, "b": 副轴角}
角度约定 (来自 windowsduo):  +10 度 ≈ 合盖,  -90 度 ≈ 屏幕垂直于桌面。

角度 -> level 的映射沿用原项目的 angle_open/angle_closed 与 glass_start 死区:
    ratio = (angle - angle_open) / (angle_closed - angle_open)
    level = 0 if ratio < glass_start else min(1, ratio)
所以 angle_open 时 level=0 (清晰), angle_closed 时 level=1 (最虚)。

pyserial 只在真正启用这个源时才导入, 所以在没有串口的机器上跑摄像头模式
不会因为缺 pyserial 而失败。
"""
import json
import re
import threading
import time

from .base import AngleSource


# ESP32 角度映射常量 —— 写死在源码, 不进 config.json。
# 这些是固件约定 (读哪个轴) 和角度->level 的端点标定, 普通用户几乎不动。
# config.json 只留 port / baud 两个用户换设备会改的。
SERIAL_AXIS = "a"              # 读固件 JSON 里的哪个轴 (主轴)
SERIAL_ANGLE_CLOSED = 10.0     # 合盖时的角度 -> level=1 (最虚)
SERIAL_ANGLE_OPEN = -90.0      # 展开时的角度 -> level=0 (清晰)
SERIAL_GLASS_START = 0.05      # 死区: ratio 低于它当 0


class SerialAngleSource(AngleSource):
    name = "serial"

    def __init__(self, cfg):
        self.port = cfg.get("port", "COM3")
        self.baud = int(cfg.get("baud", 115200))
        self.axis = SERIAL_AXIS
        self.angle_closed = SERIAL_ANGLE_CLOSED
        self.angle_open = SERIAL_ANGLE_OPEN
        self.glass_start = SERIAL_GLASS_START

        self.detail = {}
        self._thread = None
        #: stop() 超时后仍活着的旧采集线程 (卡在 serial 打开/读里)。start() 见到
        #: 它已死就清掉再起新的 —— 否则 `_thread` 永远非 None, 再也起不来。
        self._orphan = None
        self._stop = False
        self._lock = threading.Lock()

        self._angle = None
        self._other = 0.0
        self._level = None
        self._fps = 0.0
        self._status = "未启动"
        self._error = None

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread is not None:
            return
        # 上一轮 stop() 超时、把卡住的线程寄存到了 _orphan。它若已经死了就清掉
        # (可以重新开始); 还活着就等它 —— 绝不叠加第二个线程。
        # 少了这段, `_thread` 会永远非 None, **串口源将永久无法重启**。
        if self._orphan is not None:
            if self._orphan.is_alive():
                print("[serial] 上一个采集线程仍卡在设备里, 暂不重启")
                return
            self._orphan = None
        try:
            import serial  # noqa: F401
        except ImportError as exc:
            self._error = str(exc)
            self._status = "缺少 pyserial"
            print("[serial] 未安装 pyserial, 无法使用串口角度源")
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="serial-angle",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=2.0)
            # join 超时 (卡在 serial.Serial 打开/读里) 时旧线程仍活着 —— 寄存到
            # `_orphan`, 由 start() 复核 is_alive() 决定清掉还是等待。原来直接
            # 把这线程放回 `_thread`, 于是 `_thread` 永远非 None -> 再也起不来。
            if th.is_alive():
                print("[serial] 采集线程未在 2s 内退出 (设备阻塞), 已寄存待其结束")
                self._orphan = th

    # ---------- 对外读数 ----------
    def level(self):
        with self._lock:
            return self._level

    def status(self):
        with self._lock:
            return self._status

    def fps(self):
        with self._lock:
            return self._fps

    def _set_status(self, s):
        with self._lock:
            self._status = s

    # ---------- 主循环 ----------
    def _run(self):
        import serial

        pattern = re.compile(r"\{[^}]*\}")
        n, t0 = 0, time.time()

        while not self._stop:
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    self._set_status("已连接 " + self.port)
                    # 重连后重置 FPS 计数 —— 否则第一段的 (n, t0) 还带着重连前
                    # 的起点, 把停机时间算进分母, 报一次偏低的 FPS。
                    n, t0 = 0, time.time()
                    buf = b""
                    while not self._stop:
                        buf += ser.readline()
                        if b"}" not in buf:
                            buf = buf[-64:] if len(buf) > 256 else buf
                            continue
                        line, buf = buf.rsplit(b"}", 1)
                        line = (line + b"}").decode("ascii", "ignore")
                        m = pattern.search(line)
                        if not m:
                            continue
                        # **整行的解析 + 取值都包在这个 try 里。** 原来只包了
                        # json.loads, 而 `d[self.axis]` 在 try 外 —— 一行缺 "a"
                        # 键 (半行 / 固件抖动) 会抛 KeyError, 冒泡到最外层 except
                        # 被当成"串口错误" -> **重连整个端口**。100Hz 噪声链路上
                        # 这就是重连风暴。现在坏行只 `continue`(跳过这一行)。
                        try:
                            d = json.loads(m.group(0))
                            angle = float(d[self.axis])
                            other = float(d.get("b", 0.0))
                        except (ValueError, KeyError, TypeError):
                            continue

                        lo, hi = self.angle_open, self.angle_closed
                        ratio = (angle - lo) / (hi - lo) if hi != lo else 0.0
                        lvl = 0.0 if ratio < self.glass_start else min(1.0, ratio)

                        with self._lock:
                            self._angle = angle
                            self._other = other
                            self._level = lvl
                            self.detail = {
                                "angle": angle,
                                "other": other,
                                "fps": self._fps,
                            }

                        n += 1
                        now = time.time()
                        if now - t0 >= 1.0:
                            with self._lock:
                                self._fps = n / (now - t0)
                            n, t0 = 0, now
            except (serial.SerialException, OSError) as exc:
                with self._lock:                 # 和别的读写一样加锁, 别裸写
                    self._error = str(exc)
                self._set_status("等待 " + self.port)
                # 断线重连, 但不要在被要求停止时死等
                for _ in range(20):
                    if self._stop:
                        break
                    time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                with self._lock:                 # 同上: 加锁写
                    self._error = str(exc)
                self._set_status("串口错误")
                time.sleep(1.0)
