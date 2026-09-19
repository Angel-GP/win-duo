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


class SerialAngleSource(AngleSource):
    name = "serial"

    def __init__(self, cfg):
        self.port = cfg.get("port", "COM3")
        self.baud = int(cfg.get("baud", 115200))
        self.axis = cfg.get("axis", "a")
        self.angle_closed = float(cfg.get("angle_closed", 10.0))
        self.angle_open = float(cfg.get("angle_open", -90.0))
        self.glass_start = float(cfg.get("glass_start", 0.05))

        self.detail = {}
        self._thread = None
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
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

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
                        try:
                            d = json.loads(m.group(0))
                        except ValueError:
                            continue

                        angle = float(d[self.axis])
                        other = float(d.get("b", 0.0))
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
                self._error = str(exc)
                self._set_status("等待 " + self.port)
                # 断线重连, 但不要在被要求停止时死等
                for _ in range(20):
                    if self._stop:
                        break
                    time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)
                self._set_status("串口错误")
                time.sleep(1.0)
