# -*- coding: utf-8 -*-
"""PDF 转 CBZ 漫画打包工具
功能：选择 PDF/文件夹 -> 无损提取图片（原格式、xref+MD5 去重）-> 多进程并行打包为 CBZ
     附带：批量重命名 CBZ（统一为“名称 v卷号”格式）
说明：PyMuPDF 官方不支持多线程，并行采用多进程（每个进程独立打开 PDF）。
"""
import os
import re
import glob
import hashlib
import zipfile
import shutil
import tempfile
import threading
import multiprocessing
import traceback
from collections import Counter, namedtuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pymupdf


# 转换任务参数：用具名元组替代裸元组，避免位置传参错位，且可被 pickle。
ConvertJob = namedtuple("ConvertJob", "key pdf_path out_dir keep_images overwrite")


def _safe_stem(name):
    """清洗卷名，使其可安全用作目录名（去 Windows 非法字符与首尾空格/点）。"""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return stem or "untitled"


def _cleanup_stale_tmp(out_dir):
    """清理输出目录里上次崩溃残留的 .tmp_* 目录（在开始转换前调用）。"""
    try:
        for d in glob.glob(os.path.join(out_dir, ".tmp_*")):
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def scan_pdfs(folder, recursive=False):
    """扫描 PDF。递归与非递归行为一致：大小写不敏感（.pdf / .PDF 都收）。"""
    result = []
    if recursive:
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".pdf"):
                    result.append(os.path.join(root, f))
    else:
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.lower().endswith(".pdf"):
                result.append(entry.path)
    return sorted(result)


def convert_pdf_to_cbz(job):
    """转换单个 PDF（多进程 worker）。入参为 ConvertJob（可 pickle）。
    返回 (key, 是否成功, 消息)。key 用于回写状态，避免顺序错位。"""
    key = job.key
    pdf_path = job.pdf_path
    out_dir = job.out_dir
    keep_images = job.keep_images
    overwrite = job.overwrite
    base = os.path.basename(pdf_path)
    vol = os.path.splitext(base)[0]
    cbz = os.path.join(out_dir, vol + ".cbz")

    if os.path.exists(cbz) and not overwrite:
        return key, False, f"跳过：{vol}.cbz 已存在（勾选“覆盖”可重做）"

    # 中间文件默认放系统临时目录（用完即删，不污染输出目录）。
    # 但勾选「保留中间图片」时，必须放到输出目录下用户能看到的位置，
    # 否则「保留」等于没保留（图藏在 %TEMP% 里没人找得到）。
    if keep_images:
        work = os.path.join(out_dir, _safe_stem(vol) + "_images")
        os.makedirs(work, exist_ok=True)
        work_is_temp = False
    else:
        work = tempfile.mkdtemp(prefix=f"pdf2cbz_{os.getpid()}_")
        work_is_temp = True
    try:
        saved = []
        with pymupdf.open(pdf_path) as doc:
            seen_xref, seen_hash = set(), set()
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    if xref in seen_xref:
                        continue
                    seen_xref.add(xref)
                    info = doc.extract_image(xref)          # 原始字节，无损、保原格式
                    d = hashlib.md5(info["image"]).hexdigest()
                    if d in seen_hash:
                        continue
                    seen_hash.add(d)
                    n = len(saved) + 1
                    fname = f"img_{n:03d}.{info['ext']}"
                    with open(os.path.join(work, fname), "wb") as f:
                        f.write(info["image"])
                    saved.append(fname)

        if not saved:
            return key, False, f"失败：{base} 未提取到任何图片"

        # 先写临时文件再原子替换，避免中途失败留下半截 CBZ。
        tmp_cbz = cbz + ".part"
        try:
            with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as z:
                for fn in saved:
                    z.write(os.path.join(work, fn), arcname=fn)
            os.replace(tmp_cbz, cbz)          # 原子；Windows/POSIX 均可覆盖
        finally:
            if os.path.exists(tmp_cbz):
                try:
                    os.remove(tmp_cbz)
                except OSError:
                    pass

        size_mb = os.path.getsize(cbz) / 1024 / 1024
        note = f"{len(saved)} 张唯一图片，{size_mb:.1f} MB"
        if keep_images:
            note += f"；图片已保留到 {os.path.basename(work)}\\"
        elif work_is_temp:
            shutil.rmtree(work, ignore_errors=True)
        return key, True, f"完成：{vol}.cbz（{note}）"
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return key, False, f"失败：{base}：{e}\n{traceback.format_exc()}"


# ---------- 批量重命名 ----------
# 卷号识别：只认「明确的卷标记」，不再瞎抓任意数字（避免把年份/页数/章节号当卷号）。
# 支持：Vol.12 / Vol 12 / v12 / V12 / 第12卷 / 第12巻 / 12巻 / #12
_RE_VOL_PATTERNS = [
    re.compile(r"[Vv]ol\.?\s*(\d{1,4})"),
    re.compile(r"[Vv](\d{1,4})(?!\d)"),
    re.compile(r"第\s*(\d{1,4})\s*[卷巻册]"),
    re.compile(r"(\d{1,4})\s*[巻册]"),
    re.compile(r"#\s*(\d{1,4})"),
]


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


def _nat_key(name):
    """自然排序：1.cbz, 2.cbz, ..., 10.cbz（数字按数值，而非字典序）"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def plan_renames(folder, prefix, mode="auto"):
    """计算重命名计划。返回 [(原名, 新名)]，不执行。"""
    cbzs = sorted(glob.glob(os.path.join(folder, "*.cbz")), key=_nat_key)
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


class RenameDialog(tk.Toplevel):
    def __init__(self, master, default_dir):
        super().__init__(master)
        self.title("批量重命名 CBZ")
        self.geometry("620x460")
        self.minsize(540, 360)

        self.dir_var = tk.StringVar(value=default_dir)
        self.prefix_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="自动提取")
        self.skipped = set()  # 被取消（跳过）重命名的原文件名集合

        row1 = ttk.Frame(self)
        row1.pack(fill="x", padx=8, pady=4)
        ttk.Label(row1, text="目录：").pack(side="left")
        ttk.Entry(row1, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row1, text="浏览…", command=self._browse).pack(side="left")

        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="名称前缀：").pack(side="left")
        ttk.Entry(row2, textvariable=self.prefix_var, width=18).pack(side="left", padx=6)
        ttk.Label(row2, text="例：I's → I's v01.cbz").pack(side="left", padx=6)
        ttk.Label(row2, text="卷号：").pack(side="left", padx=(16, 0))
        cb = ttk.Combobox(row2, textvariable=self.mode_var, state="readonly", width=16,
                          values=["自动提取", "按顺序编号"])
        cb.pack(side="left", padx=6)

        cols = ("status", "old", "new")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=10)
        self.tree.heading("status", text="状态")
        self.tree.heading("old", text="原文件名")
        self.tree.heading("new", text="新文件名")
        self.tree.column("status", width=60, anchor="center")
        self.tree.column("old", width=245)
        self.tree.column("new", width=245)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.tag_configure("skip", foreground="gray")
        self.tree.bind("<Double-Button-1>", self._toggle_skip)

        self.lbl = ttk.Label(self, text="")
        self.lbl.pack(fill="x", padx=8)

        row3 = ttk.Frame(self)
        row3.pack(fill="x", padx=8, pady=6)
        ttk.Button(row3, text="执行重命名", command=self._do_rename).pack(side="right")
        ttk.Button(row3, text="刷新", command=self._refresh).pack(side="right", padx=6)
        ttk.Button(row3, text="恢复选中", command=self._restore_selected).pack(side="right", padx=6)
        ttk.Button(row3, text="跳过选中", command=self._skip_selected).pack(side="right", padx=6)

        self.prefix_var.trace_add("write", lambda *a: self._refresh())
        self.mode_var.trace_add("write", lambda *a: self._refresh())
        self.dir_var.trace_add("write", lambda *a: self._refresh())
        self._refresh()

    def _browse(self):
        d = filedialog.askdirectory(title="选择包含 CBZ 的目录")
        if d:
            self.dir_var.set(d)

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        folder = self.dir_var.get().strip()
        prefix = self.prefix_var.get().strip()
        mode = "auto" if self.mode_var.get() == "自动提取" else "seq"
        if not folder or not os.path.isdir(folder):
            self.lbl.config(text="目录不存在或无文件")
            return
        try:
            plan = plan_renames(folder, prefix, mode)
        except Exception as e:
            self.lbl.config(text=f"错误：{e}")
            return
        if not plan:
            self.lbl.config(text="该目录下没有 .cbz 文件")
            return
        existing = {old for old, _ in plan}
        self.skipped &= existing  # 清理已不在列表中的跳过项
        for old, new in plan:
            if old in self.skipped:
                self.tree.insert("", "end", values=("跳过", old, new), tags=("skip",))
            else:
                self.tree.insert("", "end", values=("执行", old, new))
        if self.skipped:
            self.lbl.config(text=f"共 {len(plan)} 个文件，其中 {len(self.skipped)} 个已取消（跳过），执行时不会被改名")
        else:
            self.lbl.config(text=f"共 {len(plan)} 个文件；双击行或选中后点「跳过选中」可取消单个文件")

    # ---- 取消 / 恢复单个文件 ----
    def _skip_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        for item in sel:
            vals = self.tree.item(item, "values")
            self.skipped.add(vals[1])
            self.tree.item(item, values=("跳过", vals[1], vals[2]), tags=("skip",))
        self._update_lbl()

    def _restore_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        for item in sel:
            vals = self.tree.item(item, "values")
            self.skipped.discard(vals[1])
            self.tree.item(item, values=("执行", vals[1], vals[2]), tags=())
        self._update_lbl()

    def _toggle_skip(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        for item in sel:
            vals = self.tree.item(item, "values")
            if vals[1] in self.skipped:
                self.skipped.discard(vals[1])
                self.tree.item(item, values=("执行", vals[1], vals[2]), tags=())
            else:
                self.skipped.add(vals[1])
                self.tree.item(item, values=("跳过", vals[1], vals[2]), tags=("skip",))
        self._update_lbl()

    def _update_lbl(self):
        total = len(self.tree.get_children())
        if self.skipped:
            self.lbl.config(text=f"共 {total} 个文件，其中 {len(self.skipped)} 个已取消（跳过），执行时不会被改名")
        else:
            self.lbl.config(text=f"共 {total} 个文件；双击行或选中后点「跳过选中」可取消单个文件")

    def _do_rename(self):
        folder = self.dir_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("错误", "目录不存在")
            return
        plan = plan_renames(folder, self.prefix_var.get().strip(),
                            "auto" if self.mode_var.get() == "自动提取" else "seq")
        plan = [(old, new) for old, new in plan if old not in self.skipped]  # 过滤已取消的文件
        if not plan:
            if self.skipped:
                messagebox.showinfo("提示", "所有文件都已取消（跳过），没有可重命名的文件")
            else:
                messagebox.showinfo("提示", "没有可重命名的文件")
            return
        conflicts = find_conflicts(plan, folder)
        if conflicts:
            messagebox.showerror("冲突", "以下目标文件名存在冲突，未执行任何重命名：\n"
                                 + "\n".join(conflicts))
            return
        no_change = all(old == new for old, new in plan)
        if no_change:
            messagebox.showinfo("提示", "文件名已符合目标格式，无需修改")
            return
        if not messagebox.askyesno("确认", f"将重命名 {len(plan)} 个文件，继续？"):
            return
        # 两步走：先全部改成临时唯一名，再改成目标名。避免 A->B、B->C 的链式互相覆盖。
        staged = []
        ok = 0
        errs = []
        for k, (old, new) in enumerate(plan):
            src = os.path.join(folder, old)
            tmp = os.path.join(folder, f".__rn_{k}_{os.getpid()}.tmp")
            try:
                os.rename(src, tmp)
                staged.append((tmp, new, old))
            except OSError as e:
                errs.append(f"{old}：{e}")
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
        msg = f"已重命名 {ok}/{len(plan)} 个文件"
        if self.skipped:
            msg += f"（取消 {len(self.skipped)} 个）"
        if errs:
            msg += "\n\n失败项：\n" + "\n".join(errs[:10])
        messagebox.showinfo("完成", msg)
        self._refresh()


# ---------- 主窗口 ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF 转 CBZ 漫画打包工具")
        self.geometry("780x620")
        self.minsize(680, 520)

        self.files = []          # [(path, status)]
        self.out_dir = tk.StringVar(value="")
        self.keep_images = tk.BooleanVar(value=False)
        self.overwrite = tk.BooleanVar(value=False)
        self.recursive = tk.BooleanVar(value=False)
        self.workers = tk.StringVar(value="6")
        self.running = False
        self._done_count = 0
        self._pool = None
        self._cancelled = False
        # 保护跨线程共享字段（_pool / _cancelled），主线程与 worker 线程都会读写
        self._state_lock = threading.Lock()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        frm_top = ttk.LabelFrame(self, text="1. 选择 PDF 文件（可多选 / 添加整个文件夹）")
        frm_top.pack(fill="both", expand=True, **pad)

        btns = ttk.Frame(frm_top)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="添加 PDF…", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="添加文件夹…", command=self.add_folder).pack(side="left", padx=6)
        ttk.Checkbutton(btns, text="含子文件夹", variable=self.recursive).pack(side="left", padx=4)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(btns, text="清空列表", command=self.clear_files).pack(side="left")

        cols = ("name", "path", "status")
        self.tree = ttk.Treeview(frm_top, columns=cols, show="headings", height=8)
        self.tree.heading("name", text="文件名")
        self.tree.heading("path", text="路径")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=220)
        self.tree.column("path", width=340)
        self.tree.column("status", width=120)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)

        frm_opt = ttk.LabelFrame(self, text="2. 选项")
        frm_opt.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_opt)
        row1.pack(fill="x", padx=8, pady=2)
        ttk.Label(row1, text="输出目录：").pack(side="left")
        ttk.Entry(row1, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row1, text="浏览…", command=self.browse_out).pack(side="left")

        row2 = ttk.Frame(frm_opt)
        row2.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(row2, text="保留提取的中间图片（默认不保留，只留 CBZ）",
                        variable=self.keep_images).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row2, text="覆盖已存在的 CBZ（默认跳过）",
                        variable=self.overwrite).pack(side="left")
        ttk.Label(row2, text="并行任务数：").pack(side="left", padx=(16, 4))
        ttk.Combobox(row2, textvariable=self.workers, state="readonly", width=4,
                     values=["1", "2", "4", "6", "8"]).pack(side="left")

        frm_run = ttk.LabelFrame(self, text="3. 开始")
        frm_run.pack(fill="x", **pad)

        row = ttk.Frame(frm_run)
        row.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(row, text="开始转换", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="停止", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(row, text="批量重命名 CBZ…", command=self.open_rename).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.log = tk.Text(frm_run, height=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """关窗时若仍在转换，提示并清理子进程，避免孤儿进程。"""
        if self.running:
            if not messagebox.askyesno("确认退出", "转换仍在进行中，确定退出吗？\n未完成的任务会被中止。"):
                return
            with self._state_lock:
                self._cancelled = True
                pool = self._pool
            if pool is not None:
                try:
                    pool.terminate()
                    pool.join()
                except Exception:
                    pass
        self.destroy()

    # ---------- 文件列表 ----------
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
        self._add_paths(list(paths))

    def add_folder(self):
        d = filedialog.askdirectory(title="选择文件夹")
        if not d:
            return
        found = scan_pdfs(d, self.recursive.get())
        if not found:
            messagebox.showinfo("提示", "该文件夹下没有找到 PDF 文件")
            return
        self._add_paths(found)

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(p)
            if not any(f[0] == p for f in self.files):
                self.files.append([p, ""])
                self.tree.insert("", "end", values=(os.path.basename(p), p, ""))
                added += 1
        if added and not self.out_dir.get() and paths:
            self.out_dir.set(os.path.dirname(paths[0]))
        self.log_line(f"已添加 {added} 个 PDF（累计 {len(self.files)} 个）")

    def remove_selected(self):
        """按路径精确移除选中项。不用 tree.index 反查（多选删除时索引会前移导致删错）。"""
        sel_items = list(self.tree.selection())
        if not sel_items:
            return
        # 先取出要删的路径集合，再统一过滤，避免边删边算索引
        paths_to_remove = set()
        for item in sel_items:
            vals = self.tree.item(item, "values")
            if vals and len(vals) > 1:
                paths_to_remove.add(os.path.normpath(vals[1]))
            self.tree.delete(item)
        self.files = [f for f in self.files
                      if os.path.normpath(f[0]) not in paths_to_remove]
        self.log_line(f"已移除 {len(paths_to_remove)} 个（剩余 {len(self.files)} 个）")

    def clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.files = []

    def browse_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_dir.set(d)

    def open_rename(self):
        RenameDialog(self, self.out_dir.get().strip() or os.getcwd())

    # ---------- 日志 ----------
    def log_line(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 转换（多进程并行） ----------
    def _set_status_by_path(self, path, status):
        """按文件路径回写状态，避免「过滤掉已完成项后 index 错位」。"""
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            if os.path.normpath(vals[1]) == os.path.normpath(path):
                vals[2] = status
                self.tree.item(item, values=vals)
                return
        # 回退：找不到就按文件名匹配
        name = os.path.basename(path)
        for item in self.tree.get_children():
            vals = list(self.tree.item(item, "values"))
            if vals[0] == name:
                vals[2] = status
                self.tree.item(item, values=vals)
                return

    def _on_one_done(self, path, ok, msg):
        self.log_line(msg)
        self._set_status_by_path(path, "完成" if ok else "失败/跳过")
        self._done_count += 1
        self.progress.configure(value=self._done_count)
        # 同步内存状态：仅「完成」被排除出后续运行，失败项仍可重试
        for f in self.files:
            if os.path.normpath(f[0]) == os.path.normpath(path):
                f[1] = "完成" if ok else ""
                break

    def start(self):
        if self.running:
            return
        todo = [f[0] for f in self.files if f[1] != "完成"]
        if not todo:
            messagebox.showinfo("提示", "没有待转换的 PDF（已完成的不会重复转换）")
            return
        out_dir = self.out_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请选择输出目录")
            return
        if not os.path.isdir(out_dir):
            messagebox.showwarning("提示", "输出目录不存在")
            return

        _cleanup_stale_tmp(out_dir)

        keep = self.keep_images.get()
        ove = self.overwrite.get()
        try:
            n_workers = max(1, min(16, int(self.workers.get())))
        except ValueError:
            n_workers = 4

        # 任务键用文件路径（唯一），而非序号 —— 序号在过滤后会错位。
        params = [ConvertJob(key=p, pdf_path=p, out_dir=out_dir,
                             keep_images=keep, overwrite=ove) for p in todo]

        self.running = True
        self._done_count = 0
        with self._state_lock:
            self._pool = None
            self._cancelled = False
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.progress.configure(maximum=len(todo), value=0)
        self.log_line(f"== 开始转换 {len(todo)} 个文件（并行 {n_workers}），输出到：{out_dir} ==")

        def worker():
            pool = None
            try:
                # maxtasksperchild 限制单进程内存累积，避免大 PDF 长跑内存泄漏
                pool = multiprocessing.Pool(n_workers, maxtasksperchild=8)
                with self._state_lock:
                    if self._cancelled:
                        # 构建期间用户已请求停止：立刻收尾，不再提交任务
                        pool.terminate()
                        pool.join()
                        return
                    self._pool = pool
                sent = 0
                for key, ok, msg in pool.imap_unordered(convert_pdf_to_cbz, params):
                    sent += 1
                    with self._state_lock:
                        cancelled = self._cancelled
                    if cancelled:
                        break
                    self.after(0, self._on_one_done, key, ok, msg)
            except Exception:
                self.after(0, self.log_line, "异常：\n" + traceback.format_exc())
            finally:
                if pool is not None:
                    try:
                        pool.terminate()
                        pool.join()
                    except Exception:
                        pass
                with self._state_lock:
                    self._pool = None
                    cancelled = self._cancelled
                self.after(0, self.log_line,
                           "== 已停止（未完成的任务被中止）==" if cancelled
                           else "== 全部处理结束 ==")
                self.after(0, self._finish)

        threading.Thread(target=worker, daemon=True).start()

    def cancel(self):
        with self._state_lock:
            if not self.running or self._cancelled:
                return
        if not messagebox.askyesno("确认停止", "确定要停止转换吗？\n正在处理的文件会被中止，已完成的不受影响。"):
            return
        with self._state_lock:
            self._cancelled = True
            pool = self._pool
        self.btn_cancel.config(state="disabled")
        self.log_line("== 收到停止请求，正在中止… ==")
        if pool is not None:
            try:
                pool.terminate()          # 已加锁取引用；terminate 本身幂等
            except Exception:
                pass

    def _finish(self):
        self.running = False
        with self._state_lock:
            self._cancelled = False
            self._pool = None
        self.btn_start.config(state="normal")
        self.btn_cancel.config(state="disabled")


if __name__ == "__main__":
    multiprocessing.freeze_support()   # PyInstaller --windowed 下多进程必需
    App().mainloop()
