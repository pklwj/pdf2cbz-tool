# -*- coding: utf-8 -*-
"""CBZ 批量重命名：卷号识别、计划生成、冲突检测。

纯逻辑，不含任何 UI —— 便于单独测试。UI 在 main.py 的 RenameDialog。
"""
import os
import re
from collections import Counter

# 卷号识别：只认「明确的卷标记」，不瞎抓任意数字（避免把年份/页数/章节号当卷号）。
# 支持：Vol.12 / Vol 12 / v12 / V12 / 第12卷 / 第12巻 / 12巻 / #12
_RE_VOL_PATTERNS = (
    re.compile(r"[Vv]ol\.?\s*(\d{1,4})"),
    re.compile(r"[Vv](\d{1,4})(?!\d)"),
    re.compile(r"第\s*(\d{1,4})\s*[卷巻册]"),
    re.compile(r"(\d{1,4})\s*[巻册]"),
    re.compile(r"#\s*(\d{1,4})"),
)


def extract_volume(filename):
    """从文件名提取卷号。只认明确卷标记；无标记返回 None（由调用方退化为顺序编号）。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    for pat in _RE_VOL_PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                v = int(m.group(1))
                if 0 < v <= 9999:
                    return v
            except (ValueError, IndexError):
                continue
    return None


def nat_key(name):
    """自然排序键：1.cbz, 2.cbz, ..., 10.cbz（数字按数值，而非字典序）。

    re.split 带捕获组时产出严格交替的 [非数字, 数字, 非数字, ...]，
    所以同一位置上不会是「一会儿 int 一会儿 str」，比较不会炸 TypeError。
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def list_cbz(folder):
    """列出目录下的 CBZ，大小写不敏感（.cbz / .CBZ 都收）。"""
    return sorted(
        (entry.path for entry in os.scandir(folder)
         if entry.is_file() and entry.name.lower().endswith(".cbz")),
        key=lambda p: nat_key(os.path.basename(p)),
    )


def plan_renames(folder, prefix, mode="auto"):
    """计算重命名计划。返回 [(原名, 新名)]，不执行任何磁盘操作。"""
    cbzs = list_cbz(folder)
    plan = []
    for i, p in enumerate(cbzs, start=1):
        old = os.path.basename(p)
        if mode == "seq":
            num = i
        else:
            num = extract_volume(old)
            if num is None:
                num = i
        new = f"{prefix} v{num:02d}.cbz" if prefix else f"v{num:02d}.cbz"
        plan.append((old, new))
    return plan


def find_conflicts(plan, folder):
    """返回冲突列表：目标名在两个源之间重复，或目标名已被非自身文件占用。"""
    target_counts = Counter(new for _, new in plan)
    conflicts = []
    for old, new in plan:
        if target_counts[new] > 1:
            conflicts.append(f"{new}（多个文件重名）")
        elif old != new and os.path.exists(os.path.join(folder, new)):
            conflicts.append(f"{new}（已存在同名文件）")
    # 去重保序
    seen, out = set(), []
    for c in conflicts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def execute_renames(folder, plan):
    """执行重命名计划，返回 (成功数, 错误列表)。不碰 UI，便于单测。

    两步走：先把所有源改成临时唯一名，再逐个改成目标名。
    这样 A->B、B->C 这类链式改名不会互相覆盖（第二步开始时源名已全部腾空）。
    单文件失败只回滚它自己，其余照常完成。
    """
    staged = []
    errs = []
    for k, (old, new) in enumerate(plan):
        src = os.path.join(folder, old)
        tmp = os.path.join(folder, f".__rn_{k}_{os.getpid()}.tmp")
        try:
            os.rename(src, tmp)
            staged.append((tmp, new, old))
        except OSError as e:
            errs.append(f"{old}：{e}")
    ok = 0
    for tmp, new, old in staged:
        dst = os.path.join(folder, new)
        try:
            if os.path.exists(dst):
                raise OSError("目标文件已存在（可能被其他程序占用）")
            os.rename(tmp, dst)
            ok += 1
        except OSError as e:
            errs.append(f"{old} -> {new}：{e}")
            try:                       # 回滚该文件，避免留 .tmp
                os.rename(tmp, os.path.join(folder, old))
            except OSError:
                pass
    return ok, errs
