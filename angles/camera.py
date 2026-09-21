"""摄像头角度源 —— 用普通网络摄像头量笔记本上盖的开合角。

设计目标只有一个: **把上盖的俯仰角测出来, 归一化成 level ∈ [0,1]**。
渲染层完全不知道这里发生了什么。

═══════════════════════════════════════════════════════════════════════════
怎么测: 相机绕铰链转, 画面就做一个"平面射影变换"
═══════════════════════════════════════════════════════════════════════════

上盖带摄像头、对着房间。合盖时相机跟着转, 房间在画面里的变化是:

    x' = H·x,   H = K · R · K⁻¹

其中 R 是绕铰链轴的旋转, K 是内参。**没有平移** —— 相机是"原地转", 不是
"平移"。这是本方案的基本假设, 也是它能成立的原因: 纯旋转下场景深度不影响
成像, 所以不需要知道房间有多远。

于是问题变成: 给两组匹配好的特征点, 求把一组映射到另一组的**旋转角**。

═══════════════════════════════════════════════════════════════════════════
本实现的做法 (与常见开源实现不同的几处)
═══════════════════════════════════════════════════════════════════════════

1. **不做单应矩阵分解, 直接在归一化平面上解旋转。**
   常见做法是 `findHomography` 求出 3x3 的 H, 再 `Rodrigues` 取旋转向量。
   但 H 有 8 个自由度, 而我们的运动只有 **1 个**(绕铰链轴)。让数据去拟合一
   个 8 自由度模型, 参数之间会互相"吸收"误差 (尤其画面里有一面墙这种大平面
   时), 解出来的角度会漂。
   这里改成: 先把匹配点用 K⁻¹ 投到**归一化像平面**(方向向量), 再用
   **正交 Procrustes / Kabsch** 求把一组方向转到另一组的最优旋转 R
   (这就是 Wahba 问题, 1965)。R 只有 3 个自由度, 且天然是正交矩阵 ——
   不需要再投影回 SO(3), 也不会出现"解出来的 H 不是旋转"这种事。

2. **只取绕铰链轴的那一个角, 显式地取。**
   求出 R 之后, 不是笼统地取欧拉角, 而是把旋转向量投影到**铰链轴方向**上。
   铰链轴默认取图像 x 轴 (笔记本上盖开合就是绕屏幕水平中线转), 可以用
   `camera_axis` 配成 y 轴。投影比取欧拉角稳健: 侧向的分量被显式丢掉,
   不会和主角度耦合。

3. **累积用"参考帧 + 短程增量", 参考帧按需重建。**
   单一参考帧在大角度下会失配 (重叠区域太小), 所以参考帧必须跟着走。
   这里用一个**滞后重建**策略: 只有当"当前帧相对参考帧的旋转"超过阈值时才
   把参考帧换成当前帧, 并把已累积的角度结算进去。
   重建时**不重置累积角**, 所以不会像"每帧重建"那样丢掉绝对角度。

4. **漂移用"回访检测"修, 而不是靠定期打锚点。**
   增量累积必然随机游走。但有个更强的事实可用: **同一个角度看到的画面应该
   长一样**。所以这里记录若干个"经过采样点"(角度 + 该处的特征描述子),
   当前帧如果和某个采样点匹配得很好, 就直接采用那个采样点的绝对角度 ——
   这是一次**绝对校正**, 而不是靠积分。
   与打锚点相比, 采样点是**按角度均匀**铺开的, 不需要额外的间距参数。

5. **滤波放在最后, 且用"角速度自适应"的一欧拉滤波。**
   慢动作时强平滑 (抑制传感器噪声), 快速开合时弱平滑 (不拖尾)。

═══════════════════════════════════════════════════════════════════════════
实现说明
═══════════════════════════════════════════════════════════════════════════

**本文件是独立实现。** 用 Kabsch 直接解旋转 + 按角度均匀采样回访, 而不是
`findHomography` + `Rodrigues` + 定期打锚点 —— 求法、状态机、参数体系都是自成
一套。角度 -> level 的映射 (angle_to_level) 是**产品参数** (SCALE/SIGN 的手感),
不是算法核心。
"""
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np

import wdlog
from .base import AngleSource

try:
    import paths  # main.py 已把项目根放进 sys.path
except ImportError:  # pragma: no cover - 脱离 main 单独跑时退到 angles/.. :
    paths = None

# ═══════════════════════════════════════════════════════════════════
# 取流
# ═══════════════════════════════════════════════════════════════════

#: 取流后端。Windows 上 OpenCV 默认走 MSMF, 但它对通用 UVC 摄像头
#: (例如 "USB2.0 HD UVC WebCam") 经常打不开或读不到帧, DSHOW 通常更可靠。
#: "auto" 依次尝试, 用第一个**能真正读到帧**的。
BACKEND_CHOICES = ("auto", "dshow", "msmf", "any")
_BACKEND_APIS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}

#: OpenCV 内部线程上限。默认它会开到 32 个线程, 而 ORB 的并行 for 是
#: **自旋等待**的 —— 多出来的纯属空转烧 CPU。实测 (90 帧, nfeatures=1200):
#:     32 线程 -> 墙钟 2.94s, CPU 267%
#:      2 线程 -> 墙钟 3.01s, CPU  86%
#: 耗时只多 2%, 省下约 1.8 个核。
DEFAULT_OPENCV_THREADS = 2

# ═══════════════════════════════════════════════════════════════════
# 摄像头算法/滤波常量
# ═══════════════════════════════════════════════════════════════════
# 这些是内部调校参数, 普通用户几乎不会动, 所以**写死在源码里**, 不再进
# config.json (配置文件只留 index/scale/sign/backend 这几个用户会调的)。
# 真要改就改这里。
CAMERA_NFEATURES = 1200        # ORB 每帧提取的特征点数
CAMERA_DEADZONE = 0.03         # 角度死区: 小于它当 0, 抑制静止抖动
CAMERA_AUTOCAL = True          # 摄像头源启动后自动标定一次基准帧
CAMERA_FPS = 30.0              # 摄像头目标帧率
CAMERA_MIN_CUTOFF = 1.0        # One Euro 滤波: 最小截止频率
CAMERA_BETA = 0.05             # One Euro 滤波: 速度系数 (调大更跟手但更抖)
CAMERA_D_CUTOFF = 1.0          # One Euro 滤波: 导数截止频率
CAMERA_OPENCV_THREADS = 2      # OpenCV 内部线程上限 (默认 32 会空转烧 CPU)
#: 打开后先丢弃这么多帧再做冻结帧检测 —— 传感器刚打开的前若干帧还不稳定
#: (虚拟摄像头尤甚), 丢一小段避免拿半初始化的帧做判断。30fps 下 ≈ 1 秒。
CAMERA_AE_SETTLE_FRAMES = 30
#: **dshow 专用**的自动曝光取值: 0.25=手动, 0.75=自动。这是 dshow 的语义,
#: 对 msmf/其它后端**没有意义**, 所以只在 dshow 链路里设 (见 _configure_dshow)。
DSHOW_AUTO_EXPOSURE = 0.75


def _limit_opencv_threads(n):
    if n and int(n) > 0:
        cv2.setNumThreads(int(n))
        return int(n)
    return 0


def _grab_burst(cap, n):
    """连读 n 帧, 返回成功读到的。"""
    out = []
    for _ in range(n):
        ok, frame = cap.read()
        if ok and frame is not None:
            out.append(frame)
    return out


def _picture_is_moving(cap, warmup=5, sample=3, min_diff=0.5):
    """画面是不是真的在变 —— 虚拟摄像头会给**冻结的占位图**。

    这点必须查: 本机 index=0 配 DSHOW 能打开、也能读到 1280x720 的帧, 但相邻
    帧完全一致 (平均差 0.0000)。测角算法拿冻结帧永远返回 0, 表现是"开合上盖
    毫无反应", 只看 isOpened() 完全发现不了。
    真实传感器即使静止也有噪点 (实测 5~9), 冻结帧恒为 0, 判据很干净。

    返回 (是否在动, 最后一帧)。
    """
    frames = _grab_burst(cap, warmup + sample)
    if len(frames) < 2:
        return False, (frames[-1] if frames else None)
    tail = frames[-sample:] if len(frames) >= sample else frames
    d = [float(np.abs(tail[i].astype(np.int16)
                      - tail[i + 1].astype(np.int16)).mean())
         for i in range(len(tail) - 1)]
    return (max(d) if d else 0.0) > min_diff, frames[-1]


def _dump_dir():
    """冻结帧 dump 目录: <数据目录>/diagnostics/debug/camera_dump。

    取不到 paths 时返回 None (dump 只是诊断, 失败不影响主流程)。
    """
    if paths is None:
        return None
    try:
        d = Path(paths.debug_dir()) / "camera_dump"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001  dump 失败不该影响主流程
        return None


def _drain_frames(cap, n):
    """打开后丢 n 帧 (传感器刚上电的前几帧不稳定)。读不到就提前停。"""
    for _ in range(n):
        ok, _ = cap.read()
        if not ok:
            break


# ═══════════════════════════════════════════════════════════════════
# "上次成功的后端" 记忆 —— 根治"每次都先撞上虚拟摄像头"
# ═══════════════════════════════════════════════════════════════════
# 问题: `auto` 的后端顺序是写死的 dshow -> msmf -> any。但 index 与设备的对应
# **随后端而变**, 而且**虚拟摄像头(手机投屏/联想超级互联)常常在 dshow 下排在
# 最前面并给出冻结帧**。于是在这类机器上, 每次打开摄像头都要先"打开虚拟摄像头
# -> 丢 30 帧 -> 检测到冻结 -> dump 一张 -> release", 再轮到真摄像头。
#
# 修法: 把**每个 index 上次真正成功的后端**记到一个小状态文件, 下次 auto 时把它
# 排到最前, 直接命中真摄像头; 它若失败(拔了/换了设备)再退回完整列表。
# 状态文件跟 config 放一起 (<数据目录>/diagnostics/config/camera_backend.json)。
def _state_path():
    if paths is None:
        return None
    try:
        return paths.config_file("camera_backend.json")
    except Exception:  # noqa: BLE001
        return None


def _load_good_backends():
    p = _state_path()
    if p is None or not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001  坏了就当没有
        return {}


def _remember_backend(index, backend):
    """记住某 index 成功的后端 (供下次 auto 优先)。失败静默。"""
    p = _state_path()
    if p is None:
        return
    try:
        d = _load_good_backends()
        if d.get(str(index)) == backend:
            return
        d[str(index)] = backend
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _configure_dshow(cap):
    """dshow 链路专有的初始化: 打开自动曝光。

    `CAP_PROP_AUTO_EXPOSURE` 的 0.25/0.75 是 **dshow 专有语义** (0.25=手动,
    0.75=自动)。老代码设 0.25(手动) 却从不给 `CAP_PROP_EXPOSURE` 值, 驱动回退到
    默认 1/64s, 室内画面亮度只有 ~5, ORB 提不出特征 -> 无法标定/检测。这里改成
    自动 (0.75)。

    **只对 dshow 调用。** 对 msmf/其它后端, 这个属性没有该语义, 硬设可能破坏原本
    正常的曝光 (实测日志: 成功的是 msmf)。

    必须在 `_drain_frames` **之前**调用 —— 丢帧的目的就是等自动曝光收敛, 先丢帧
    再设曝光等于白丢。
    """
    try:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, DSHOW_AUTO_EXPOSURE)
    except Exception:  # noqa: BLE001  个别设备不支持, 忽略
        pass


def open_camera(index=0, backend="auto", warmup=5,
                threads=DEFAULT_OPENCV_THREADS):
    """打开摄像头, 返回 (VideoCapture, 后端名)。

    成功条件是**两条都要满足**:
      1) 真的读到一帧 (有的后端 isOpened() 为真但 read() 一直失败);
      2) 画面真的在变 (排除虚拟摄像头的冻结帧, 见 _picture_is_moving)。
    "auto" 依次试 dshow -> msmf -> any。
    """
    _limit_opencv_threads(threads)
    if backend == "auto":
        full = [("dshow", cv2.CAP_DSHOW), ("msmf", cv2.CAP_MSMF),
                ("any", cv2.CAP_ANY)]
        # 把"上次成功的后端"提到最前 —— 避免每次都先撞上 dshow 下的虚拟摄像头。
        # 找不到/记的后端不在列表里就保持原顺序。
        good = _load_good_backends().get(str(index))
        order = sorted(full, key=lambda kv: 0 if kv[0] == good else 1)
        if good and order[0][0] == good:
            wdlog.log.debug("index=%d 上次成功的后端是 %s, 优先试它" % (index, good), tag="camera")
    else:
        order = [(backend, _BACKEND_APIS[backend])]

    tried = []
    dump_dir = _dump_dir()
    for name, api in order:
        cap = cv2.VideoCapture(index, api)
        if not cap.isOpened():
            cap.release()
            tried.append("%s: 打不开" % name)
            continue
        # **先按后端做专有初始化 (仅 dshow 需要设自动曝光), 再丢帧** ——
        # 丢帧就是为了等自动曝光收敛, 顺序反了等于白丢。
        if name == "dshow":
            _configure_dshow(cap)
        _drain_frames(cap, CAMERA_AE_SETTLE_FRAMES)
        moving, frame = _picture_is_moving(cap, warmup=warmup)
        if moving:
            # 注意: 成功路径不 release —— 返回的就是这个 cap 给上层继续用。
            _remember_backend(index, name)     # 记住它, 下次直接用
            return cap, name
        cap.release()
        if frame is None:
            tried.append("%s: 打开了但读不到帧" % name)
        else:
            tried.append("%s: 读到的是冻结帧(虚拟摄像头)" % name)
            # 把抓到的最后一帧存下来方便查验是"哪种冻结" (纯色? 占位图?).
            if dump_dir is not None:
                ts = time.strftime("%Y%m%d_%H%M%S")
                fp = dump_dir / ("index%d_%s_%s.png" % (index, name, ts))
                try:
                    cv2.imwrite(str(fp), frame)
                    tried[-1] += " (画面已存到 %s)" % fp
                    wdlog.log.warn("index=%d 后端 %s 是冻结帧(虚拟摄像头), "
                                   "画面 dump 到 %s" % (index, name, fp), tag="camera")
                except Exception:  # noqa: BLE001
                    pass

    raise RuntimeError(
        "摄像头 index=%d 没有可用组合。逐个后端的尝试结果: %s。\n"
        "  排查建议:\n"
        "    0) 冻结帧画面已 dump 到 %s (路径也标在各条尝试结果后), 可直接看抓到的是什么;\n"
        "    1) 在设置窗口点「扫描」逐个探测 index x 后端 (会跳过冻结帧);\n"
        "    2) 注意 index 与设备的对应关系**随后端而变**, 且虚拟摄像头\n"
        "       (如手机投屏) 常常排在前面并给出冻结帧;\n"
        "    3) 设置 -> 隐私和安全性 -> 相机: 打开\"让桌面应用访问相机\";\n"
        "    4) 确认没有别的程序(微信/钉钉/相机应用)正占用摄像头。"
        % (index, "; ".join(tried) if tried else "无",
           str(dump_dir) if dump_dir else "(dump 目录创建失败, 略过)"))


# ═══════════════════════════════════════════════════════════════════
# 角度 -> level (产品参数, 沿用原项目约定)
# ═══════════════════════════════════════════════════════════════════

def angle_to_level(pitch_deg, scale, sign, deadzone):
    """俯仰角(度) -> (level, fold_angle)。纯函数, 便于单独验证。

        fold_angle = clip(180 + sign * scale * pitch_deg, 0, 180)
        level      = (180 - fold_angle) / 180

    上盖完全展开 (标定时 pitch=0) -> fold=180 -> level=0 (清晰);
    合盖使 pitch 增大 -> fold 减小 -> level 增大 (越虚)。
    """
    fold = float(np.clip(180.0 + sign * scale * pitch_deg, 0.0, 180.0))
    lvl = (180.0 - fold) / 180.0
    if lvl <= deadzone:
        lvl = 0.0
    else:
        # 死区要**减掉再拉伸**, 不能直接归零: 直接归零会在 level == deadzone
        # 处留下一个 0 -> 0.03 的硬跳变, 边缘抖动时浓度跟着跳, 玻璃层就反复
        # 显隐 (屏幕和光标看起来一闪一闪)。减掉之后边界两侧都收敛到 0。
        lvl = (lvl - deadzone) / (1.0 - deadzone)
    return lvl, fold


# ═══════════════════════════════════════════════════════════════════
# 一欧拉滤波
# ═══════════════════════════════════════════════════════════════════

class OneEuroFilter:
    """一欧拉滤波: 自适应低通, 慢速强平滑、快速跟手。

    参考 Casiez et al. 2012 "1€ Filter" 的定义:
        cutoff = min_cutoff + beta * |dx/dt|
    即速度越大, 截止频率越高 (越跟手); 静止时截止频率低 (越平滑)。
    """

    def __init__(self, min_cutoff=1.0, beta=0.05, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.reset()

    def reset(self):
        self._x = None
        self._dx = 0.0

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * np.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def filter(self, x, dt):
        dt = max(float(dt), 1e-3)
        if self._x is None:
            self._x = float(x)
            return float(x)
        dx = (x - self._x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        self._x = a * float(x) + (1.0 - a) * self._x
        self._dx = dx_hat
        return self._x


# ═══════════════════════════════════════════════════════════════════
# 核心: 从匹配点直接求"绕铰链轴的旋转角"
# ═══════════════════════════════════════════════════════════════════

def _to_bearings(pts, Kinv):
    """像素坐标 -> 归一化像平面上的**方向向量** (齐次, 模长为 1)。

    这一步把 K 从问题里消掉: 之后只剩"方向 -> 方向"的旋转关系。
    """
    n = len(pts)
    homo = np.hstack([pts.reshape(-1, 2), np.ones((n, 1))])
    v = homo @ Kinv.T
    norm = np.linalg.norm(v, axis=1, keepdims=True)
    norm[norm < 1e-12] = 1.0
    return v / norm


def solve_rotation(a, b, weights=None):
    """求最优旋转 R, 使 R·a_i ≈ b_i。返回 (R, 残差中位数); 无解返回 (None, inf)。

    这就是 **Wahba / Kabsch 问题**。解法是标准的 SVD:

        令  M = Σ w_i · b_i ⊗ a_i   (3x3)
            M = U Σ Vᵀ
            R = U · diag(1, 1, det(U Vᵀ)) · Vᵀ

    最后那个 diag(1,1,±1) 是必须的: 不加的话 SVD 可能给出一个**反射**矩阵
    (det = -1), 那在物理上不是旋转, 会让角度突然反向。

    比"求单应矩阵再分解"稳的原因: 只拟合 3 个自由度, 且结果天然正交。
    """
    if len(a) < 4:
        return None, float("inf")
    w = np.ones(len(a)) if weights is None else np.asarray(weights, dtype=np.float64)
    M = (b * w[:, None]).T @ a
    try:
        U, _, Vt = np.linalg.svd(M)
    except np.linalg.LinAlgError:
        return None, float("inf")
    d = float(np.linalg.det(U @ Vt))
    R = U @ np.diag([1.0, 1.0, 1.0 if d >= 0 else -1.0]) @ Vt
    # 残差: 直接用角度空间的中位误差, 比像素误差更能反映"角度准不准"
    pa = a @ R.T
    cos = np.clip(np.sum(pa * b, axis=1), -1.0, 1.0)
    resid = float(np.median(np.degrees(np.arccos(cos))))
    return R, resid


def angle_about_axis(R, axis):
    """把旋转矩阵 R 里"绕 axis 的那一个角"取出来 (弧度)。

    **这是本实现的关键一步。** 我们的物理运动只有 1 个自由度, 但 R 描述的是
    3 个。常见做法是把 R 转成欧拉角再取一个分量 —— 那在侧向分量不为零时
    会和主角度耦合 (侧倾 5 度能让读数偏好几度)。
    这里改成: 取 R 的旋转向量 (轴角表示), 再**投影到铰链轴方向**上。
    侧向分量被显式丢掉, 主角度不受它污染。

    用轴角而不是欧拉角还有一个好处: 小角度下没有万向锁, 也不依赖旋转顺序。
    """
    rvec, _ = cv2.Rodrigues(R)
    v = rvec.ravel()
    ax = np.asarray(axis, dtype=np.float64)
    n = np.linalg.norm(ax)
    if n < 1e-12:
        return 0.0
    ax = ax / n
    # 旋转向量在铰链轴上的**带符号分量**就是那个角
    return float(np.dot(v, ax))


# ═══════════════════════════════════════════════════════════════════
# 跟踪器
# ═══════════════════════════════════════════════════════════════════

#: 一帧里至少要有这么多对匹配才敢用它算角度。
MIN_INLIERS = 10
#: 单帧增量超过这个值 (弧度) 视为误匹配, 丢弃并重建参考帧。~17 度。
#: 真实开合每帧不会跳这么多, 跳了就是匹配错了。
MAX_STEP_RAD = 0.30
#: 参考帧重建阈值 (弧度): 相对参考帧转了这么多就把参考帧换成当前帧。~9 度。
#: 太小 -> 频繁重建, 累积误差大; 太大 -> 画面重叠不够, 匹配失败。
REBASE_RAD = 0.16
#: 角度采样点的间距 (弧度)。每转过这么多度就存一份"角度 + 描述子",
#: 用于**绝对**回访校正。~10 度。
SAMPLE_RAD = 0.175
#: 回访时, 候选采样点必须落在这个范围内才去试 (弧度)。~30 度。
VISIT_WINDOW_RAD = 0.52
#: 回访校正要求的内点数。比常规匹配高, 因为这是**绝对**校正, 给错了会跳。
VISIT_MIN_INLIERS = 14
#: 最多保留多少个采样点 (防止长时间运行后无限增长)。
MAX_SAMPLES = 72


class OrbTracker:
    """上盖俯仰角跟踪器。

    with_camera=False 时不打开摄像头, 只留算法本体 —— 这样能用合成帧
    在没有摄像头的机器上验证匹配与测角。
    """

    def __init__(self, camera_index=0, nfeatures=1200, ratio=0.75,
                 with_camera=True, min_cutoff=1.0, beta=0.05, d_cutoff=1.0,
                 backend="auto", threads=DEFAULT_OPENCV_THREADS,
                 axis=(1.0, 0.0, 0.0)):
        self.cap = None
        self.backend_name = None
        if with_camera:
            # 打开 + 曝光等后端专有初始化都在 open_camera 里做完了 (见
            # _configure_dshow) —— 这里不再重复设曝光, 免得对 msmf 也设上
            # dshow 专有的值而破坏曝光。
            self.cap, self.backend_name = open_camera(camera_index, backend,
                                                      threads=threads)

        self.orb = cv2.ORB_create(nfeatures=nfeatures)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.ratio = ratio
        # CLAHE: 局部对比度归一化, 抗自动曝光漂移 (开灯/关灯后画面亮度变了,
        # 但纹理结构没变, 归一化后特征点还能匹配上)
        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        #: 铰链轴 (相机坐标系)。笔记本上盖绕屏幕水平中线转, 默认 x 轴。
        #: 若你的摄像头是竖装的, 用 camera_axis="y" 改成 y 轴。
        self.axis = [float(v) for v in axis]

        self.K = None
        self._Kinv = None

        # 参考帧 (滑动)
        self._ref_gray = None
        self._ref_kp = None
        self._ref_des = None
        #: 参考帧建立时, 已经累积到的绝对角度
        self._ref_base = 0.0

        #: 当前绝对角度 (弧度, 未滤波)
        self.angle = 0.0

        #: 按角度均匀采样的"回访点", 用于绝对校正
        self._samples = []

        self._oneuro = OneEuroFilter(min_cutoff=min_cutoff, beta=beta,
                                     d_cutoff=d_cutoff)

        # 调试
        self.last_good = []
        self.dbg_ref_gray = None
        self.dbg_ref_kp = None
        self.dbg_cur_gray = None
        self.dbg_cur_kp = None
        self.dbg_matches = None

    # ---------------- 生命周期 ----------------
    def release(self):
        if self.cap is not None:
            self.cap.release()

    def read(self):
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        return frame if (ok and frame is not None) else None

    @property
    def has_reference(self):
        return self._ref_des is not None

    # ---------------- 标定 ----------------
    def set_reference(self, gray):
        """把当前画面设为"上盖完全展开"的基准, 角度清零。

        返回特征点是否够用 (不够的话调用方应该提示用户换个场景)。
        """
        prepared = self._prepare(gray)
        kp, des = self.orb.detectAndCompute(prepared, None)
        n = 0 if des is None else len(kp)
        wdlog.log.debug("标定: 提取到 %d 个特征点 (需要 >= 20)" % n, tag="camera.cv")
        if des is None or len(kp) < 20:
            return False
        self._install_reference(prepared, kp, des, 0.0)
        self.angle = 0.0
        self._oneuro.reset()
        self._samples = [{"angle": 0.0, "des": des, "kp": kp}]
        return True

    def _prepare(self, gray):
        if self.K is None:
            h, w = gray.shape[:2]
            # 内参不知道就按"焦距 = 长边"估。这个近似只影响归一化平面的尺度,
            # 对**角度**没有影响 (纯旋转下角度和 f 无关), 所以够用。
            f = float(max(h, w))
            self.K = np.array([[f, 0.0, w / 2.0],
                               [0.0, f, h / 2.0],
                               [0.0, 0.0, 1.0]], dtype=np.float64)
            self._Kinv = np.linalg.inv(self.K)
        return self.clahe.apply(gray)

    def _install_reference(self, prepared, kp, des, base_angle):
        self._ref_gray = prepared.copy()
        self._ref_kp = kp
        self._ref_des = des
        self._ref_base = float(base_angle)

    # ---------------- 匹配 ----------------
    def _good_matches(self, des1, des2):
        """Lowe's ratio test 筛选匹配对。返回 (索引对, 匹配对象列表)。"""
        if des1 is None or des2 is None:
            return [], []
        pairs = self.matcher.knnMatch(des1, des2, k=2)
        good = []
        for p in pairs:
            # k=2 时偶尔只返回 1 个邻居, 那种没有"次优"可比, 直接丢
            if len(p) == 2 and p[0].distance < self.ratio * p[1].distance:
                good.append(p[0])
        return good, good

    def _measure(self, ref_des, ref_kp, des, kp, min_inliers):
        """量出"当前帧相对参考帧"的旋转角。

        返回 (角度 或 None, 内点数, 匹配列表, 当前帧用到的点, 参考帧用到的点)。
        """
        good, _ = self._good_matches(ref_des, des)
        if len(good) < MIN_INLIERS:
            return None, len(good), good, None, None

        a_px = np.float32([ref_kp[m.queryIdx].pt for m in good])
        b_px = np.float32([kp[m.trainIdx].pt for m in good])

        a = _to_bearings(a_px, self._Kinv)
        b = _to_bearings(b_px, self._Kinv)

        # RANSAC: 随机取 4 对求 R, 数内点。纯旋转模型下内点判据是
        # "把这个方向转过去, 和目标的夹角够小"。
        R, inl = self._ransac_rotation(a, b)
        if R is None or inl.sum() < min_inliers:
            n = int(inl.sum()) if inl is not None else 0
            return None, n, good, a_px, b_px

        # 用内点重解一次 (最小二乘意义上的最优)
        R2, _ = solve_rotation(a[inl], b[inl])
        if R2 is None:
            return None, int(inl.sum()), good, a_px, b_px
        return (angle_about_axis(R2, self.axis), int(inl.sum()),
                good, a_px, b_px)

    def _ransac_rotation(self, a, b, thresh_deg=2.5, iters=64, seed=None):
        """纯旋转的 RANSAC。返回 (最优 R, 内点布尔数组)。

        内点判据用**角度**而不是像素距离: 把 a_i 用候选 R 转过去, 和 b_i 的
        夹角小于阈值就算内点。角度判据对画面位置不敏感, 比像素判据稳。

        **性能**: 每次迭代都要对全部 N 个匹配做 `a @ R.T` + arccos, 而这是
        摄像头模式每帧的主导 CPU 开销 (nfeatures=1200 时 N 可到几百)。两条
        不改变结果的省法:
          1. **提前退出**: 一旦某个候选的共识已经覆盖了几乎所有匹配, 再抽新的
             4 点集不可能明显更好 —— 直接停。真实场景里前几次迭代通常就命中。
          2. **只对"有机会赢"的候选做全量打分**: 先算内点数的上界没意义 (仍需
             全量), 所以这里靠 (1) 就够了; 不再额外降 iters (那会改变结果)。
        确定性不变: 同一输入仍然给出同一个 R (rng 种子固定、退出条件只依赖
        已算出的共识数)。
        """
        n = len(a)
        if n < 4:
            return None, np.zeros(n, dtype=bool)
        rng = np.random.default_rng(seed if seed is not None else 12345)
        cos_thresh = np.cos(np.radians(thresh_deg))

        #: 共识覆盖到这么高比例就没必要再抽了 —— 剩下的差异只在内点边缘,
        #: 而后面 `_measure` 还会用内点做一次最小二乘精解, 不受这点影响。
        good_enough = max(4, int(n * 0.9))

        best_R, best_mask, best_n = None, None, 0
        for _ in range(iters):
            idx = rng.choice(n, size=4, replace=False)
            R, _ = solve_rotation(a[idx], b[idx])
            if R is None:
                continue
            pa = a @ R.T
            cos = np.clip(np.sum(pa * b, axis=1), -1.0, 1.0)
            mask = cos >= cos_thresh
            cnt = int(mask.sum())
            if cnt > best_n:
                best_R, best_mask, best_n = R, mask, cnt
                if best_n >= good_enough:
                    break               # 已足够好, 省下剩余迭代
        if best_R is None:
            return None, np.zeros(n, dtype=bool)
        return best_R, best_mask

    # ---------------- 绝对回访校正 ----------------
    def _visit_correction(self, kp, des, guess):
        """如果当前画面和某个已记录的采样点匹配得好, 直接采用它的绝对角度。

        这是**绝对**校正: 不是靠积分, 而是"这个角度我见过, 画面一模一样"。
        增量累积的漂移到这里会被一次性拉回。
        """
        if not self._samples or des is None:
            return None
        near = [s for s in self._samples
                if abs(s["angle"] - guess) < VISIT_WINDOW_RAD]
        if not near:
            return None
        near.sort(key=lambda s: abs(s["angle"] - guess))

        best_angle, best_inl = None, -1
        for s in near[:2]:
            d, inl, _, _, _ = self._measure(s["des"], s["kp"], des, kp,
                                            VISIT_MIN_INLIERS)
            if d is not None and inl > best_inl:
                best_inl, best_angle = inl, s["angle"] + d
        if best_angle is None or best_inl < VISIT_MIN_INLIERS:
            return None
        return best_angle

    def _maybe_sample(self, kp, des, angle):
        """按角度均匀铺采样点。

        **不存 gray 图像。** 采样点只需要"角度 + 描述子 + 关键点"就能做回访
        校正 (`_visit_correction` 只读这三个), 原来那份 `prepared.copy()` 从头
        到尾没人读过 —— 而 MAX_SAMPLES=72 个 1280x720 的灰度帧 = **约 66MB
        常驻内存**白白占着。删掉。
        """
        for s in self._samples:
            if abs(s["angle"] - angle) < SAMPLE_RAD:
                return
        self._samples.append({"angle": float(angle), "des": des, "kp": kp})
        self._samples.sort(key=lambda s: s["angle"])
        if len(self._samples) > MAX_SAMPLES:
            # 丢最远的, 保持靠近当前角度的那一段
            self._samples.sort(key=lambda s: abs(s["angle"] - angle))
            self._samples = self._samples[:MAX_SAMPLES]
            self._samples.sort(key=lambda s: s["angle"])

    # ---------------- 主估计 ----------------
    def estimate(self, gray, dt=None):
        """返回滤波后的绝对俯仰角 (弧度); 这帧无法估计时返回 None。"""
        self.last_good = []
        if not self.has_reference:
            return None

        prepared = self._prepare(gray)
        kp, des = self.orb.detectAndCompute(prepared, None)
        if des is None or len(kp) < 10:
            return None

        step, inliers, good, a_px, b_px = self._measure(
            self._ref_des, self._ref_kp, des, kp, MIN_INLIERS)

        self.last_good = good
        self.dbg_ref_gray = self._ref_gray
        self.dbg_ref_kp = self._ref_kp
        self.dbg_cur_gray = prepared
        self.dbg_cur_kp = kp
        self.dbg_matches = good

        if step is None:
            # 匹配失败: 画面可能已经转太远, 把参考帧换到当前帧让下一帧能跟上。
            # **不结算角度** —— 没有可信增量, 硬加会引入漂移。
            wdlog.log.debug("匹配不足 (%d good), 重建参考帧 @ %.1f°"
                            % (inliers, np.degrees(self.angle)), tag="camera.cv")
            self._install_reference(prepared, kp, des, self.angle)
            return None
        if abs(step) > MAX_STEP_RAD:
            # 单帧跳太多 = 匹配错了。同样重建参考帧, 不采信这个增量。
            wdlog.log.debug("单帧跳变 %.1f° (> %.0f°), 丢弃增量并重建参考帧"
                            % (np.degrees(abs(step)), np.degrees(MAX_STEP_RAD)),
                            tag="camera.cv")
            self._install_reference(prepared, kp, des, self.angle)
            return None

        guess = self._ref_base + step

        # 绝对校正 (每 4 帧试一次, 省 CPU; 它本来就是个"定期校准"的角色)
        self._frame_count = getattr(self, "_frame_count", 0) + 1
        corrected = None
        if self._frame_count % 4 == 0:
            corrected = self._visit_correction(kp, des, guess)

        if corrected is not None:
            if abs(corrected - guess) > 0.02:
                wdlog.log.debug("回访校正: %.2f° -> %.2f° (拉回 %.2f°)"
                                % (np.degrees(guess), np.degrees(corrected),
                                   np.degrees(abs(corrected - guess))), tag="camera.cv")
            self.angle = float(corrected)
            self._install_reference(prepared, kp, des, self.angle)
        else:
            self.angle = float(guess)
            if abs(step) > REBASE_RAD:
                # 转得够远了, 把参考帧推进一格 (角度已经结算进 _ref_base)
                wdlog.log.debug("推进参考帧 @ %.1f°" % np.degrees(self.angle),
                                tag="camera.cv")
                self._install_reference(prepared, kp, des, self.angle)

        self._maybe_sample(kp, des, self.angle)

        if dt is None:
            dt = 1.0 / 30.0
        dt = float(np.clip(dt, 1e-3, 0.2))
        return self._oneuro.filter(self.angle, dt)

    # ---------------- 调试 ----------------
    def draw_matches(self, cur_gray=None):
        if (self.dbg_ref_gray is None or self.dbg_ref_kp is None
                or self.dbg_cur_gray is None or self.dbg_cur_kp is None
                or not self.dbg_matches):
            fallback = cur_gray if cur_gray is not None else self.dbg_cur_gray
            if fallback is None:
                return np.zeros((240, 320, 3), dtype=np.uint8)
            return cv2.cvtColor(fallback, cv2.COLOR_GRAY2BGR)
        return cv2.drawMatches(
            self.dbg_ref_gray, self.dbg_ref_kp, self.dbg_cur_gray, self.dbg_cur_kp,
            self.dbg_matches, None,
            flags=cv2.DrawMatchesFlags_NOT_DRAW_SINGLE_POINTS,
        )


# ═══════════════════════════════════════════════════════════════════
# 角度源包装 (后台线程)
# ═══════════════════════════════════════════════════════════════════

class CameraAngleSource(AngleSource):
    """把 OrbTracker 跑在后台线程里, 对外只吐 level。"""

    name = "camera"

    def __init__(self, cfg):
        # 用户会调的留在 config.json: index / scale / sign / backend。
        self.index = int(cfg.get("camera_index", 0))
        self.scale = float(cfg.get("camera_scale", 1.1))
        self.sign = int(cfg.get("camera_sign", -1))
        self.backend = str(cfg.get("camera_backend", "auto"))
        if self.backend not in BACKEND_CHOICES:
            wdlog.log.warn("未知 camera_backend=%r, 回退到 auto" % (self.backend,), tag="camera")
            self.backend = "auto"
        # 下面这些是算法/滤波微调, 普通用户几乎不动 —— 写死为源码常量,
        # 不再进 config.json (见本文件顶部的 CAMERA_* 常量)。
        self.nfeatures = CAMERA_NFEATURES
        self.deadzone = CAMERA_DEADZONE
        # 自动标定由用户在设置里控制 (autocal_on_glass_open); 源码常量只是默认。
        self.autocal = bool(cfg.get("autocal_on_glass_open", CAMERA_AUTOCAL))
        self.target_fps = CAMERA_FPS
        self.min_cutoff = CAMERA_MIN_CUTOFF
        self.beta = CAMERA_BETA
        self.d_cutoff = CAMERA_D_CUTOFF
        self.threads = CAMERA_OPENCV_THREADS
        # 铰链轴: 上盖绕屏幕水平中线转 -> 图像 x 轴。竖装摄像头用 "y"。
        ax = str(cfg.get("camera_axis", "x")).lower()
        self.axis = (0.0, 1.0, 0.0) if ax == "y" else (1.0, 0.0, 0.0)

        self.detail = {}
        self._thread = None
        #: stop() 超时后仍活着的旧采集线程 (卡在打开设备里), 防止 start() 重复起
        self._orphan = None
        self._stop = False
        self._lock = threading.Lock()

        self._tracker = None
        self._frames = 0

        self._level = None
        self._pitch_deg = 0.0
        self._fold_angle = 180.0
        self._matches = 0
        self._fps = 0.0
        self._status = "未启动"

        self._cal_request = False
        self._want_debug = False
        self._dbg_img = None
        self._dbg_seq = 0
        self._open_error = None

    # ---------- 生命周期 ----------
    def start(self):
        """**不阻塞**地开始采集。

        打开摄像头要 ~1 秒 (逐个后端试 + 抓几帧做冻结帧检测), 有的机器上甚至
        要 20 秒。同步做的话调用方 (Qt 主线程) 就会僵住 —— 用户看到的就是
        "点开启玻璃层卡一下"。所以设备在采集线程里打开。
        """
        if self._thread is not None:
            return
        # 上一轮 stop() 超时后可能仍有 daemon 线程卡在打开设备里 —— 那种情况
        # 绝不新建第二个线程 (两个线程会抢同一个摄像头)。等它自己结束。
        if self._orphan is not None:
            if self._orphan.is_alive():
                wdlog.log.warn("上一个采集线程仍在打开设备, 暂不重启", tag="camera")
                return
            self._orphan = None
        self._stop = False
        self._open_error = None
        with self._lock:
            self._status = "正在打开…"
        self._thread = threading.Thread(target=self._run, name="camera-angle",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        th, self._thread = self._thread, None
        if th is not None:
            # 别久等 —— 这同样是在主线程上调的。正常 ~33ms 就退出; 只有在
            # "正在打开设备"阶段可能久一点 (open_camera 最坏 ~20s), 那种情况
            # join 会超时, 旧线程仍在跑。
            th.join(timeout=0.6)
        if th is not None and th.is_alive():
            # join 超时: 旧线程还活着 (卡在 open_camera 里)。**寄存它**,
            # 供 start() 检查 —— 否则紧接着 start() 会再起一个线程、两个线程
            # 抢同一个摄像头。
            self._orphan = th
        else:
            self._orphan = None
            tracker, self._tracker = self._tracker, None
            if tracker is not None:
                tracker.release()

    def available(self):
        return self._open_error is None

    def opening(self):
        """还在打开设备中 (此时 available() 仍为 True, 但还不能下结论)。"""
        return self._thread is not None and self._tracker is None

    def ready(self):
        """设备**真的就绪**了吗 —— 打开成功且已经产出若干帧。

        判据不能只看 `opening()` (那只表示"打开动作还没结束"): 打开成功但还在
        抓头几帧时, 画面可能还没稳定(曝光/AE 未收敛)。自动标定必须等到这时机
        之后, 否则会拿一张未就绪的帧当基准。`_frames > target_fps` 约等于"出图
        满 1 秒", 与摄像头源自身的按帧自动标定判据一致。
        """
        if self._tracker is None or self._open_error is not None:
            return False
        return self._frames > int(self.target_fps)

    def calibrated(self):
        """是否已经标定过基准帧 (tracker 有参考帧)。"""
        return bool(self._tracker is not None and self._tracker.has_reference)

    # ---------- 外部命令 ----------
    def request_calibration(self):
        self._cal_request = True

    def set_debug(self, on):
        self._want_debug = bool(on)

    def adjust_scale(self, delta):
        self.scale = round(max(0.1, self.scale + delta), 2)
        return self.scale

    def flip_sign(self):
        self.sign = -self.sign
        return self.sign

    def pop_debug(self):
        """返回 (图像, 序号); 序号未变则返回 (None, seq)。"""
        with self._lock:
            return self._dbg_img, self._dbg_seq

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

    # ---------- 主循环 ----------
    def _run(self):
        try:
            self._tracker = OrbTracker(
                self.index, self.nfeatures,
                min_cutoff=self.min_cutoff, beta=self.beta,
                d_cutoff=self.d_cutoff, backend=self.backend,
                threads=self.threads, axis=self.axis)
        except Exception as exc:  # noqa: BLE001
            self._open_error = str(exc)
            with self._lock:
                self._status = "摄像头不可用"
            wdlog.log.error("摄像头打开失败: %s" % exc, tag="camera")
            return
        if self._stop:
            self._tracker.release()
            self._tracker = None
            return
        wdlog.log.info("index=%d 用后端 %s 打开成功 (OpenCV 线程数限为 %d, 铰链轴=%s)"
                       % (self.index, self._tracker.backend_name,
                          _limit_opencv_threads(self.threads),
                          "x" if self.axis[0] else "y"), tag="camera")
        with self._lock:
            self._status = "已打开, 等待标定"

        tracker = self._tracker
        t_prev = None
        t_fps = time.time()
        n_fps = 0

        # 包 try/finally: 无论正常退出还是抛异常, 摄像头**一定**会被释放。
        # 泄漏设备会导致下次打开失败 (表现为"开一下玻璃层就再也打不开摄像头")。
        try:
            while not self._stop:
                loop_start = time.time()
                frame = tracker.read()
                if frame is None:
                    time.sleep(0.01)
                    continue

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                self._frames += 1

                if self._cal_request:
                    self._cal_request = False
                    ok = tracker.set_reference(gray)
                    with self._lock:
                        self._status = "已标定" if ok else "标定失败(特征点不足)"
                    wdlog.log.info("标定" + ("成功" if ok
                                            else "失败: 特征点不足, 请对着有纹理的场景"), tag="camera")

                # 启动后自动标定一次, 让程序开箱可用
                if (self.autocal and not tracker.has_reference
                        and self._frames > int(self.target_fps * 1.5)):
                    ok = tracker.set_reference(gray)
                    with self._lock:
                        self._status = ("自动标定成功" if ok
                                        else "自动标定失败(请手动标定)")
                    wdlog.log.info("自动标定" + ("成功" if ok
                                                else "失败, 请手动标定"), tag="camera")

                dt = (loop_start - t_prev) if t_prev is not None else None
                t_prev = loop_start
                pitch_rad = tracker.estimate(gray, dt)

                with self._lock:
                    if pitch_rad is not None:
                        self._pitch_deg = float(np.degrees(pitch_rad))
                        lvl, fold = angle_to_level(
                            self._pitch_deg, self.scale, self.sign, self.deadzone)
                        self._fold_angle = fold
                        self._level = lvl
                        self._matches = len(tracker.last_good)
                        self._status = "追踪中"
                    else:
                        self._matches = len(tracker.last_good)
                        self._status = ("未标定(请标定)" if not tracker.has_reference
                                        else "匹配不足")
                    self.detail = {
                        "pitch": self._pitch_deg,
                        "fold": self._fold_angle,
                        "matches": self._matches,
                        "scale": self.scale,
                        "sign": self.sign,
                        "backend": tracker.backend_name,
                    }

                # 调试画面在采集线程里生成, 显示交给主线程 (避免跨线程调 cv2)
                if self._want_debug:
                    img = tracker.draw_matches(gray)
                    with self._lock:
                        self._dbg_img = img
                        self._dbg_seq += 1

                n_fps += 1
                now = time.time()
                if now - t_fps >= 1.0:
                    with self._lock:
                        self._fps = n_fps / (now - t_fps)
                    n_fps = 0
                    t_fps = now

                if self.target_fps > 0:
                    spare = (1.0 / self.target_fps) - (time.time() - loop_start)
                    if spare > 0:
                        time.sleep(spare)
        finally:
            if self._tracker is tracker:
                self._tracker = None
            try:
                tracker.release()
            except Exception:  # noqa: BLE001
                pass
