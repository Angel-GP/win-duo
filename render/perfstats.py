"""玻璃层显示周期的重绘率统计 —— 隐藏时输出一条 INFO 摘要, 退出时汇总。

═════════════════════════════════════════════════════════════════════════
为什么用分桶直方图而不是存原始样本
═════════════════════════════════════════════════════════════════════════
样本来自 _print_status 0.1s 节流下的一次 _paint_rate() 快照。长期运行
(挂机一天 = 单个周期最多几万样本) 不能把原始样本攒在内存里 —— 那是给
"内存随运行时间线性增长"这个新 bug 开门。所以: 均值/方差/最值用运行
统计量 (n/Σ/Σ²/min/max, O(1) 内存), 1% 分位数用**固定分桶直方图**估算
—— 桶边界按对数刻度铺在 ~1..130/s 区间 (重绘率恒为非负, 上限就是显示
器刷新率量级; 对数刻度让低速率区间分得更细, 正是"掉帧"最该看清的区域)。

精度: 对数桶在 15/s 附近约 0.6/s 一桶, 对"p1 是否跌破 15"这类判断足够。
"""
import math
import time

#: 直方图覆盖的速率上界 (显示器刷新率量级, 超出归最后一桶)
_HI = 130.0
#: 桶数。ln(130)/96 ≈ 0.0486/桶, 即每桶约 +5% 速率 -> 15/s 附近约 0.6/s 一桶
_N_BUCKETS = 96
_STEP = math.log(_HI) / _N_BUCKETS


def _bucket_of(rate):
    """非负速率 -> 桶下标。0 和负值 (未初始化) 都进 0 号桶。"""
    if rate <= 0.0:
        return 0
    i = int(math.log(rate) / _STEP)
    return i if i < _N_BUCKETS else _N_BUCKETS - 1


def _bucket_upper(i):
    """桶 i 的上边界 (速率值)。"""
    return math.exp((i + 1) * _STEP)


class ShowPeriodStats:
    """一次玻璃层显示周期 (show -> hide) 的重绘率采样。O(1) 内存。"""

    def __init__(self):
        self.t_start = time.time()
        self.n = 0
        self.sum = 0.0
        self.sum2 = 0.0
        self.lo = float("inf")
        self.hi = 0.0
        self.buckets = [0] * _N_BUCKETS

    def add(self, rate):
        if rate < 0.0:
            return
        self.n += 1
        self.sum += rate
        self.sum2 += rate * rate
        if rate < self.lo:
            self.lo = rate
        if rate > self.hi:
            self.hi = rate
        self.buckets[_bucket_of(rate)] += 1

    # ------------------------------------------------------------ 结果
    @property
    def duration(self):
        return max(0.0, time.time() - self.t_start)

    def p1(self):
        """1% 分位数: 从低桶往高累加, 找到累计数 >= ceil(0.01*n) 的第一个
        桶, 取其上边界作近似值 (分桶估算, 恒为该桶内的保守偏大值)。"""
        if self.n == 0:
            return float("nan")
        need = max(1, math.ceil(0.01 * self.n))
        acc = 0
        for i, c in enumerate(self.buckets):
            acc += c
            if acc >= need:
                return _bucket_upper(i)
        return self.hi

    def summary_line(self):
        """一行摘要文本; 没采到样本 (显示时长 < 0.1s) 时返回 None。
        样本 <2 时方差无统计意义, 标 N/A。"""
        if self.n == 0:
            return None
        mean = self.sum / self.n
        if self.n >= 2:
            var = max(0.0, (self.sum2 - self.sum * self.sum / self.n)
                      / (self.n - 1))
            var_s = "%.2f" % var
        else:
            var_s = "N/A"
        return ("重绘率统计: n=%d 最好=%.1f/s 最差=%.1f/s 平均=%.1f/s "
                "方差=%s p1=%.1f/s 时长=%.0fs"
                % (self.n, self.hi, self.lo, mean, var_s,
                   self.p1(), self.duration))


class SessionStats:
    """整个日志会话 (进程生命周期) 的周期摘要集合 —— 供 atexit 汇总。

    每个元素只存一条已格式化的短摘要 (周期数 = 显示/隐藏切换次数, 一天
    也就几百条, 无内存压力); 原始 ShowPeriodStats 用完即弃。
    """

    def __init__(self):
        self.lines = []

    def add_period(self, stats):
        line = stats.summary_line()
        if line is not None:
            self.lines.append(line)

    def report(self):
        """退出摘要的多行文本; 一次显示周期都没有 (玻璃层从没开过) 则 None。"""
        if not self.lines:
            return None
        return "玻璃层共显示 %d 个周期:\n  " % len(self.lines) + "\n  ".join(self.lines)
