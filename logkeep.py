"""日志文件的保留策略 —— 按"时间/数量"清理旧日志。

═══════════════════════════════════════════════════════════════════════
语义: 两个维度**各自独立**, 同时生效 (取交集)
═══════════════════════════════════════════════════════════════════════

    天数(days)   数量(count)   结果
    ─────────────────────────────────────────────────────────
    -1          -1            全留, 什么都不删
    -1          30            只留最近 30 个文件          <- 默认
    7           -1            只留最近 7 天内的
    7           30            两个条件**都要满足**
    0           任意          不保存日志
    任意        0             不保存日志

**为什么"任一为 0 就不保存"而不是只认 0/0**: 保留 = 同时落在天数内**且**排在
前 count 个之内的文件。任一上限为 0, 这个交集就是空的。反过来讲, 如果
days=0 / count=30 时还留 30 个, 那"保留 0 天"就成了一句空话。

**`-1` 表示该维度不设限**, 不是"保留 -1 天"。

═══════════════════════════════════════════════════════════════════════
为什么单独一个模块
═══════════════════════════════════════════════════════════════════════

删除文件是有风险的动作 (删错就是用户的历史日志没了), 而 `import main` 会顺带
装日志 tee、建日志文件、开摄像头 —— 为测一个"该不该删"付这个代价太大。
独立出来, 边界情况 (0 / -1 / 恰好到点 / 删不掉 / 混合) 才测得动。
"""
import time
from pathlib import Path

#: 保留时间 (天): -1 = 不限, 0 = 不保存日志, N>0 = 只留最近 N 天
DEFAULT_DAYS = -1
#: 保留数量 (个): -1 = 不限, 0 = 不保存日志, N>0 = 最多留 N 个文件
DEFAULT_COUNT = 30

#: `last.log` —— 始终指向最新一次运行的硬链接。**永远不删**: 删掉目录项会让
#: "看最新日志"这个入口失效 (见 main._TeeLogger._link_last)。
LAST_LOG_NAME = "last.log"


def as_int(value, default):
    """把配置里的值转成 int; 转不了就用 default。

    **不能写成 `int(v or 0)`** —— 那样配置里一个笔误 ("abc") 或缺失会变成 0,
    而 0 在这里的含义是"不保存日志", 等于用户什么都没干日志就没了。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def enabled(days, count):
    """按这套策略, **要不要写日志文件**。

    任一维度为 0 -> 一个都不留 -> 不保存 (见文件头的语义说明)。
    """
    return as_int(days, DEFAULT_DAYS) != 0 and as_int(count, DEFAULT_COUNT) != 0


def describe(days, count):
    """给设置界面用的一句话说明。"""
    days = as_int(days, DEFAULT_DAYS)
    count = as_int(count, DEFAULT_COUNT)
    if not enabled(days, count):
        which = "保留时间" if days == 0 else "保留数量"
        return "不保存日志（%s为 0）" % which
    parts = ["不限时间" if days < 0 else "保留最近 %d 天" % days,
             "不限数量" if count < 0 else "最多保留 %d 个文件" % count]
    return "，".join(parts)


def prune(log_dir, keep_path=None, days=DEFAULT_DAYS, count=DEFAULT_COUNT,
          now=None):
    """按策略删除旧日志, 返回 `(删除数, 保留数, 错误列表)`。

    - **绝不删** `keep_path` (本次运行正在写的那个) 和 `last.log`。
    - `count` 指**目录里最终保留的日志文件总数** (含本次运行这个)。本次的文件
      不在候选里, 所以历史最多留 `count - 1` 个。
    - 只删 `*.log`。抓图 (camera_dump) 等在别的目录, 不受影响。
    - **失败只记不抛**: 它在启动路径上, 清理失败不该挡住程序启动; 单个文件删
      不掉 (被杀软/别的程序占着) 就跳过, 不影响其余。
    """
    log_dir = Path(log_dir)
    days = as_int(days, DEFAULT_DAYS)
    count = as_int(count, DEFAULT_COUNT)
    if not enabled(days, count):
        return 0, 0, []

    keep = None
    if keep_path is not None:
        try:
            keep = Path(keep_path).resolve()
        except OSError:
            keep = None

    # 收集候选: 目录里的 *.log, 去掉 last.log 和本次运行的文件
    entries = []
    try:
        for p in log_dir.glob("*.log"):
            if p.name == LAST_LOG_NAME:
                continue
            try:
                if keep is not None and p.resolve() == keep:
                    continue
                entries.append((p, p.stat().st_mtime))
            except OSError:
                continue        # 单个文件读不到状态就跳过, 不影响其余
    except OSError as exc:
        return 0, 0, [str(exc)]

    entries.sort(key=lambda e: e[1], reverse=True)      # 新的在前

    now = time.time() if now is None else now
    cutoff = (now - days * 86400.0) if days > 0 else None
    # 本次运行的文件占掉一个名额 (它一定被保留, 但算在总数里)
    room = count - 1 if (count > 0 and keep is not None) else count

    deleted, kept, errors = 0, 0, []
    for i, (p, mtime) in enumerate(entries):
        too_old = cutoff is not None and mtime < cutoff
        too_many = count > 0 and i >= max(0, room)
        if not (too_old or too_many):
            kept += 1
            continue
        try:
            p.unlink()
            deleted += 1
        except OSError as exc:
            errors.append("%s: %s" % (p.name, exc))
            kept += 1           # 删不掉就当作还留着, 别谎报
    return deleted, kept, errors
