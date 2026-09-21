"""WGC affinity 验证: 测试窗标了 WDA_EXCLUDEFROMCAPTURE 后, 各采集后端还能不能截到它。

截得到 = 该后端会把玻璃层截进自己的截图里 = 正反馈回路 (Intel Arc DDA 的病根)。
截不到 = 该后端可以替代 DDA 做采集。

三个阶段, 各跑 PROBE_SEC 秒:
  baseline   测试窗**不设** affinity, 用 WGC 采 —— 验证"检测逻辑本身能看见窗口"
  dda        设 affinity, bettercam (DXGI Desktop Duplication) 采 —— 预期在本机看到
             窗口 (那就是要修的病), 在 Intel 机器上同样会看到 (API 收下、驱动不执行)
  wgc        设 affinity, Windows.Graphics.Capture 采 —— 预期**看不到**窗口

判定: 每帧在测试窗中心采样, 统计"品红命中率"。baseline 命中率必须高 (否则脚本
本身不可信); dda/wgc 的命中率就是反馈风险的直接证据。

用法: uv run python scripts/wgc_affinity_probe.py
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

import numpy as np

PROBE_SEC = 2.0
WIN_SIZE = (400, 300)          # 测试窗尺寸 (物理像素, 脚本自己设 DPI aware)
MAGENTA = (255, 0, 255)        # BGR; 纯品红几乎不会出现在正常桌面里
TOL = 30                       # 每通道容差 (压缩/色彩管理留余量)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
HWND_TOPMOST = -1
SWP_NOMOVE_NOSIZE = 0x1 | 0x2
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_SHOWNOACTIVATE = 4
COLOR_WINDOW = 11
SM_CXSCREEN, SM_CYSCREEN = 0, 1


def _rect_center(hwnd):
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return ((r.left + r.right) // 2, (r.top + r.bottom) // 2)


class TestWindow:
    """纯色置顶窗。用 GDI 画一次品红, 不跑消息循环也保持得住 (WS_POPUP 静态窗)。"""

    def __init__(self):
        # 物理像素坐标, 避免 DPI 虚拟化把 rect 和采集帧对不上
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            user32.SetProcessDPIAware()

        self.w, self.h = WIN_SIZE
        sw = user32.GetSystemMetrics(SM_CXSCREEN)
        sh = user32.GetSystemMetrics(SM_CYSCREEN)
        x, y = (sw - self.w) // 2, (sh - self.h) // 2

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self.hbr = gdi32.CreateSolidBrush(
            MAGENTA[0] | (MAGENTA[1] << 8) | (MAGENTA[2] << 16))

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.WINFUNCTYPE(
                ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        # DefWindowProcW 默认按 32 位 int 转参, LPARAM 有符号 64 位会溢出,
        # 必须显式声明 argtypes/restype
        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = wt.LPARAM

        self.wndproc = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)(
            lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        wc = WNDCLASS(0, self.wndproc, 0, 0, hinst, None, None,
                      self.hbr, None, "wgc-probe-win")
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise RuntimeError("RegisterClass 失败: %d" % ctypes.GetLastError())

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "wgc-probe-win", "probe",
            WS_POPUP | WS_VISIBLE, x, y, self.w, self.h, None, None, hinst, None)
        if not self.hwnd:
            raise RuntimeError("CreateWindowEx 失败: %d" % ctypes.GetLastError())
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE_NOSIZE)
        # 挡住测试窗位置的普通窗口全部让开 (置顶即可, 别真最小化用户窗口)
        time.sleep(0.3)
        self.center = _rect_center(self.hwnd)

    def set_affinity(self, on):
        ok = user32.SetWindowDisplayAffinity(
            self.hwnd, WDA_EXCLUDEFROMCAPTURE if on else 0)
        got = wt.ULONG(0)
        read = user32.GetWindowDisplayAffinity(self.hwnd, ctypes.byref(got))
        flag = bool(got.value & WDA_EXCLUDEFROMCAPTURE) if read else False
        return ok, flag

    def close(self):
        user32.DestroyWindow(self.hwnd)
        gdi32.DeleteObject(self.hbr)


# ---------------------------------------------------------------- 后端
def frames_wgc():
    """yield (h, w, 3) BGR ndarray, 直到生成器被关闭。"""
    from windows_capture import WindowsCapture
    cap = WindowsCapture(cursor_capture=False, draw_border=False)
    latest = {"frame": None}

    @cap.event
    def on_closed():
        pass

    @cap.event
    def on_frame_arrived(frame, _ctx):
        latest["frame"] = frame

    ctrl = cap.start_free_threaded()
    try:
        t_end = time.time() + PROBE_SEC
        while time.time() < t_end:
            f = latest["frame"]
            if f is None:
                time.sleep(0.01)
                continue
            arr = f.frame_buffer          # numpy, BGRA
            yield arr[:, :, :3]
            time.sleep(0.01)
    finally:
        ctrl.stop()


def frames_dda():
    import bettercam
    cam = bettercam.create(output_idx=0, output_color="BGRA")
    t_end = time.time() + PROBE_SEC
    try:
        while time.time() < t_end:
            arr = cam.grab()
            if arr is not None:
                yield arr[:, :, :3]
            time.sleep(0.01)
    finally:
        cam.release()


BACKENDS = {"wgc": frames_wgc, "dda": frames_dda}


def probe(name, frame_iter, center, label):
    hits = total = 0
    sample = None
    for bgr in frame_iter:
        cx, cy = center
        if cy >= bgr.shape[0] or cx >= bgr.shape[1]:
            continue
        px = bgr[cy, cx].astype(int)
        ok = all(abs(int(px[c]) - MAGENTA[c]) <= TOL for c in range(3))
        total += 1
        hits += ok
        if sample is None:
            sample = px
    rate = hits / total * 100 if total else 0
    verdict = ("命中 %.0f%% -> 会截到 -> 有反馈风险" % rate) if rate > 5 \
        else ("命中 %.0f%% -> 看不到窗口" % rate)
    print("  [%-8s] 采到 %4d 帧, 测试窗中心命中 %4d (%5.1f%%), 首帧像素=%s"
          % (label, total, hits, rate, tuple(sample) if sample is not None else "-"))
    print("            判定: %s" % verdict)
    return rate


def main():
    win = TestWindow()
    print("测试窗 %dx%d 于屏幕中心, 中心点=%s, 颜色=BGR%s"
          % (win.w, win.h, win.center, MAGENTA))
    try:
        print("\n[1/3] baseline: 不设 affinity, WGC 采集 (检测逻辑自检)")
        win.set_affinity(False)
        probe("wgc", frames_wgc(), win.center, "baseline")

        print("\n[2/3] dda: 设 affinity, DXGI Desktop Duplication 采集")
        ok, flag = win.set_affinity(True)
        print("  SetWindowDisplayAffinity: API=%s, 读回=%s" % (ok, flag))
        probe("dda", frames_dda(), win.center, "dda")

        print("\n[3/3] wgc: 设 affinity, Windows.Graphics.Capture 采集")
        probe("wgc", frames_wgc(), win.center, "wgc")
    finally:
        win.close()
    print("\n结论: baseline 命中率必须高; dda/wgc 命中率低 = 该后端可替代 DDA 采集。")


if __name__ == "__main__":
    sys.exit(main())
