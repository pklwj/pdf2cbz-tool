# -*- coding: utf-8 -*-
"""PDF 转 CBZ 漫画打包工具 —— 图形界面与唯一启动入口。

用法：
    python main.py            # 直接跑
    PDF2CBZ.exe               # 或用打包好的单文件 exe

其余模块职责：
    extractor.py  PDF 内图片无损提取（xref + MD5 去重）
    archive.py    CBZ 打包（按格式选压缩方式、原子写入）
    batch.py      批量转换（多进程 worker、目录扫描、残留清理）
    rename.py     批量重命名逻辑（卷号识别、计划、冲突检测）
"""
import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import batch
import rename

_LOG_MAX_LINES = 3000
_APP_TITLE = "PDF 转 CBZ 漫画打包工具"
_WORKER_CHOICES = ("1", "2", "4", "6", "8")
# 单文件低于这个页数就不切段：分段要付出一次合并开销（读+写一份完整副本）
_SPLIT_MIN_PAGES = 40


def _default_workers():
    """默认并行度：按物理核数（逻辑核 / 2 的近似）挑，落在候选档位里。"""
    half = max(1, (os.cpu_count() or 4) // 2)
    picks = [int(x) for x in _WORKER_CHOICES if int(x) <= half]
    return str(max(picks) if picks else int(_WORKER_CHOICES[0]))


# ---------- 批量重命名对话框 ----------
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
        self._snapshot_olds = None   # 最近一次预览时的文件名集合（防 TOCTOU）

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
            plan = rename.plan_renames(folder, prefix, mode)
        except Exception as e:
            self.lbl.config(text=f"错误：{e}")
            return
        if not plan:
            self.lbl.config(text="该目录下没有 .cbz 文件")
            return
        existing = {old for old, _ in plan}
        self._snapshot_olds = existing   # 记录预览时刻的目录内容
        self.skipped &= existing  # 清理已不在列表中的跳过项
        for old, new in plan:
            if old in self.skipped:
                self.tree.insert("", "end", values=("跳过", old, new), tags=("skip",))
            else:
                self.tree.insert("", "end", values=("执行", old, new))
        self._update_lbl()

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
        plan = rename.plan_renames(folder, self.prefix_var.get().strip(),
                                   "auto" if self.mode_var.get() == "自动提取" else "seq")
        # TOCTOU 防护：预览之后目录内容若变化，执行的计划与用户看到的不一致
        current_olds = {old for old, _ in plan}
        if self._snapshot_olds is not None and current_olds != self._snapshot_olds:
            messagebox.showwarning(
                "目录已变化",
                "预览之后目录内容发生了变化（文件增删或重命名）。\n"
                "列表已自动刷新为最新计划，请确认无误后再点「执行重命名」。")
            self._refresh()
            return
        plan = [(old, new) for old, new in plan if old not in self.skipped]  # 过滤已取消的文件
        if not plan:
            if self.skipped:
                messagebox.showinfo("提示", "所有文件都已取消（跳过），没有可重命名的文件")
            else:
                messagebox.showinfo("提示", "没有可重命名的文件")
            return
        conflicts = rename.find_conflicts(plan, folder)
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
        ok, errs = rename.execute_renames(folder, plan)
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
        self.title(_APP_TITLE)
        self.geometry("780x620")
        self.minsize(680, 520)

        self.files = []          # [[path, status]]
        self.out_dir = tk.StringVar(value="")
        self.keep_images = tk.BooleanVar(value=False)
        self.overwrite = tk.BooleanVar(value=False)
        self.recursive = tk.BooleanVar(value=False)
        self.workers = tk.StringVar(value=_default_workers())
        self.running = False
        self._done_count = 0
        self._pool = None
        self._cancelled = False
        self._run_started = 0.0
        self._run_ok = 0
        self._run_fail = 0
        self._run_total = 0
        # 工作线程只往队列里放事件，所有界面读写都回主线程做（Tk 非线程安全）
        self._events = queue.Queue()
        self._progress_q = None      # Manager 队列：worker 上报页进度
        self._manager = None
        self._prog_marks = {}        # 文件名 → Text mark，一文件一行原地刷新
        self._prog_seq = 0
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
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Control-A>", self._select_all)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Double-Button-1>", self._reveal_selected)

        frm_opt = ttk.LabelFrame(self, text="2. 选项")
        frm_opt.pack(fill="x", **pad)

        row1 = ttk.Frame(frm_opt)
        row1.pack(fill="x", padx=8, pady=2)
        ttk.Label(row1, text="输出目录：").pack(side="left")
        ttk.Entry(row1, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row1, text="浏览…", command=self.browse_out).pack(side="left")
        # 并行度放这一行：原来挤在复选框行，780 宽下会把下拉框顶出可视区
        ttk.Label(row1, text="并行任务数：").pack(side="left", padx=(12, 4))
        ttk.Combobox(row1, textvariable=self.workers, state="readonly", width=3,
                     values=list(_WORKER_CHOICES)).pack(side="left")

        row2 = ttk.Frame(frm_opt)
        row2.pack(fill="x", padx=8, pady=2)
        ttk.Checkbutton(row2, text="保留提取的中间图片（默认不保留，只留 CBZ）",
                        variable=self.keep_images).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row2, text="覆盖已存在的 CBZ（默认跳过）",
                        variable=self.overwrite).pack(side="left")

        frm_run = ttk.LabelFrame(self, text="3. 开始")
        frm_run.pack(fill="x", **pad)

        row = ttk.Frame(frm_run)
        row.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(row, text="开始转换", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="停止", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(row, text="批量重命名 CBZ…", command=self.open_rename).pack(side="left", padx=8)
        ttk.Button(row, text="打开输出目录", command=self.open_out_dir).pack(side="left")
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
        found = batch.scan_pdfs(d, self.recursive.get())
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

    def open_out_dir(self):
        """在文件管理器里打开输出目录。"""
        d = self.out_dir.get().strip()
        if not d or not os.path.isdir(d):
            messagebox.showwarning("提示", "输出目录不存在")
            return
        try:
            os.startfile(d)                       # Windows
        except AttributeError:
            subprocess.Popen(["xdg-open", d])     # 其他平台兜底
        except OSError as e:
            messagebox.showerror("打开失败", str(e))

    def _select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _reveal_selected(self, _event=None):
        """双击文件行：在资源管理器中定位它。"""
        sel = self.tree.selection()
        if not sel:
            return
        p = self.tree.item(sel[0], "values")[1]
        if not os.path.exists(p):
            return
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(p)}"])
        except OSError:
            pass

    def open_rename(self):
        RenameDialog(self, self.out_dir.get().strip() or os.getcwd())

    # ---------- 日志 ----------
    def log_line(self, s, to_file=True):
        """写入 GUI 日志框；同时按需追加到输出目录的 pdf2cbz.log。
        写文件失败绝不影响主流程（例如目录只读、被占用）。"""
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        # 限制行数，避免长会话把日志堆到吃掉内存（最旧的先丢）
        n = int(self.log.index("end-1c").split(".")[0])
        if n > _LOG_MAX_LINES:
            self.log.delete("1.0", f"{n - _LOG_MAX_LINES}.0")
            self._prog_marks.clear()    # 行被删过，旧 mark 位置已不可信
        self.log.see("end")
        self.log.configure(state="disabled")
        if to_file:
            self._log_to_file(s)

    def _log_to_file(self, s):
        """追加到 <输出目录>/pdf2cbz.log。多行内容按行加时间戳；失败静默。"""
        out_dir = self.out_dir.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            return
        try:
            path = os.path.join(out_dir, "pdf2cbz.log")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                for i, line in enumerate(str(s).splitlines() or [""]):
                    # 首行带时间戳，后续行缩进对齐（traceback 等）
                    f.write(f"[{ts}] {line}\n" if i == 0 else f"           {line}\n")
        except OSError:
            pass          # 日志写不出不是致命错误，忽略即可

    def _render_progress(self, label, done, total):
        """每个文件在日志区占一行，原位刷新「页进度」。

        用 Text mark 锚定行首：多文件并发时各改各的行，不会互相覆盖或错位。
        只进 GUI，不写 pdf2cbz.log —— 进度每秒好几条，落盘会把日志冲成流水账。
        """
        if total <= 0:
            return
        pct = done * 100 // total
        txt = f"  {label}：{done}/{total} 页（{pct}%）"
        self.log.configure(state="normal")
        mark = self._prog_marks.get(label)
        try:
            if mark is None:
                self.log.insert("end", txt + "\n")
                mark = f"pg{self._prog_seq}"
                self._prog_seq += 1
                line = int(self.log.index("end-1c").split(".")[0]) - 1
                self.log.mark_set(mark, f"{line}.0")
                self.log.mark_gravity(mark, "left")
                self._prog_marks[label] = mark
            else:
                self.log.delete(mark, f"{mark} lineend")
                self.log.insert(mark, txt)
        except tk.TclError:
            self._prog_marks.pop(label, None)      # mark 失效就下次重建
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_progress(self):
        """取走队列里的页进度，按文件各留最后一条再渲染。

        多文件并行时不能只取全局最后一条 —— 那样只有一个文件能显示进度。
        """
        if self._progress_q is None:
            return
        latest = {}
        while True:
            try:
                label, done, total = self._progress_q.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError):
                return          # Manager 已关闭
            latest[label] = (done, total)     # dict 保留插入顺序
        for label, (done, total) in latest.items():
            self._render_progress(label, done, total)

    # ---------- 转换（多进程并行） ----------
    def _pump(self):
        """在主线程消费 worker 投递的事件。

        Tk 不是线程安全的：工作线程只往队列里放数据，界面读写全部回到这里做。
        （原先工作线程直接调 self.after()，靠 mainloop 恰好能跑，属未定义行为。）
        """
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._drain_progress()      # 先把该文件最后的页进度画完
                self._on_one_done(*payload)
            elif kind == "range":
                self._on_range_done(*payload)
            elif kind == "log":
                self.log_line(payload)
            elif kind == "end":
                self._drain_progress()      # 先把残留进度画完，_finish 会关掉队列
                self._on_run_end(payload)
                self._finish()
        self._drain_progress()
        if self.running:
            self.after(80, self._pump)

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

    def _on_range_done(self, part, ok, msg, n=0, label="", elapsed=0.0):
        """一个页段完成：推进进度、改标题，并在日志里留一行明细。"""
        self._done_count += 1
        self.progress.configure(value=self._done_count)
        self.title(f"{_APP_TITLE} — 分段 {self._done_count}/{self._run_total - 1}")
        if ok:
            self.log_line(f"  页段 {self._done_count}/{self._run_total - 1}："
                          f"{label} → {n} 张，{elapsed:.1f}s")
        else:
            self.log_line(f"  页段失败：{msg}")

    def _on_one_done(self, path, ok, msg):
        self.log_line(msg)
        if ok:
            self._run_ok += 1
        else:
            self._run_fail += 1
        self._set_status_by_path(path, "完成" if ok else "失败/跳过")
        self._done_count += 1
        self.progress.configure(value=self._done_count)
        self.title(f"{_APP_TITLE} — {self._done_count}/{self._run_total}")
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

        # 同名不同目录的 PDF 会落到同一个 <卷名>.cbz，并行时互相覆盖 —— 先问一声
        collisions = batch.find_output_collisions(todo)
        if collisions:
            preview = "\n".join(f"{name}  ← {len(ps)} 个 PDF" for name, ps in collisions[:5])
            more = f"\n…另有 {len(collisions) - 5} 组" if len(collisions) > 5 else ""
            if not messagebox.askyesno(
                    "输出重名",
                    f"有 {len(collisions)} 组 PDF 会写到同一个 CBZ（同名但不在同一目录）：\n"
                    f"{preview}{more}\n\n"
                    "转换时它们会互相覆盖，最终只留最后完成的那个。要继续吗？"):
                return

        batch.cleanup_stale_tmp(out_dir)

        keep = self.keep_images.get()
        ove = self.overwrite.get()
        try:
            n_workers = max(1, min(16, int(self.workers.get())))
        except ValueError:
            n_workers = 4

        # 单文件 + 多核：任务粒度是「一个文件一个进程」，不切段就只有 1 个核在跑。
        # 页数够多才值得切（切段要多一次合并，小文件不划算）。
        split = None
        if len(todo) == 1 and n_workers > 1:
            pages = batch.count_pages(todo[0])
            vol = os.path.splitext(os.path.basename(todo[0]))[0]
            if pages >= _SPLIT_MIN_PAGES and (ove or not os.path.exists(os.path.join(out_dir, vol + ".cbz"))):
                split = (todo[0], pages, vol)

        # 页进度要跨进程回传：Manager 队列的代理可 pickle，能塞进任务里传给 worker。
        # 分段模式不建（进度按段计就够），省掉一个常驻进程。
        self._manager = None
        self._progress_q = None
        if not split:
            try:
                self._manager = multiprocessing.Manager()
                self._progress_q = self._manager.Queue()
            except Exception:
                self._manager = None      # 起不来就退化成没有页进度

        if split:
            pdf, pages, vol = split
            seg = min(n_workers, pages)
            step = -(-pages // seg)                     # 向上取整，末段可能短
            params = [batch.RangeJob(pdf, os.path.join(out_dir, f"{vol}.cbz.p{s:05d}.part"), s,
                                     min(s + step, pages))
                      for s in range(0, pages, step)]
        else:
            # 任务键用文件路径（唯一），而非序号 —— 序号在过滤后会错位。
            params = [batch.ConvertJob(pdf_path=p, out_dir=out_dir, keep_images=keep,
                                       overwrite=ove, progress=self._progress_q) for p in todo]

        self.running = True
        self._done_count = 0
        self._prog_marks.clear()
        self._prog_seq = 0
        self._events = queue.Queue()      # 新一轮，丢掉上一轮残留事件
        # 分段时进度按「段数 + 1 次合并」计，否则按文件数计
        self._run_total = len(params) + (1 if split else 0)
        with self._state_lock:
            self._pool = None
            self._cancelled = False
        self.btn_start.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.progress.configure(maximum=self._run_total, value=0)
        self._run_started = time.time()
        self._run_ok = 0
        self._run_fail = 0
        self.log_line(f"===== 运行开始 | 输出目录：{out_dir} =====")
        self.log_line(f"== 开始转换 {len(todo)} 个文件（并行 {n_workers}），"
                      f"保留中间图片：{'是' if keep else '否'}，覆盖：{'是' if ove else '否'} ==")
        if split:
            self.log_line(f"== 单文件按页切 {len(params)} 段并行（{pages} 页），完成后合并 ==")

        def worker():
            pool = None
            done_parts = []
            range_failed = False
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
                for res in pool.imap_unordered(batch.run_job, params):
                    with self._state_lock:
                        cancelled = self._cancelled
                    if cancelled:
                        break
                    if res[0] == "range":
                        _, part, ok, msg, n, label, elapsed = res
                        if ok:
                            done_parts.append(part)
                        else:
                            range_failed = True
                        self._events.put(("range", (part, ok, msg, n, label, elapsed)))
                    else:
                        _, key, ok, msg = res
                        self._events.put(("done", (key, ok, msg)))
                else:
                    # 循环正常跑完（未被 break）才合并；被中止则交给下次启动清理 .part
                    if split:
                        pdf, _pages, vol = split
                        if range_failed:
                            self._events.put(("done", (pdf, False, "失败：有页段未完成，已跳过合并")))
                        else:
                            self._events.put(("log", f"== {len(done_parts)} 个页段完成，合并中… =="))
                            self._events.put(("done", batch.merge_ranges(
                                pdf, out_dir, vol, sorted(done_parts), keep)))
            except Exception:
                self._events.put(("log", "异常：\n" + traceback.format_exc()))
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
                self._events.put(("end", cancelled))

        threading.Thread(target=worker, daemon=True).start()
        self._pump()

    def _on_run_end(self, cancelled):
        """统一输出运行结束摘要（GUI + 日志文件）。"""
        elapsed = time.time() - self._run_started if self._run_started else 0
        total = self._run_ok + self._run_fail
        head = "已停止" if cancelled else "运行结束"
        self.log_line(f"===== {head} | 共 {total} 个：成功 {self._run_ok}，"
                      f"失败/跳过 {self._run_fail}，用时 {elapsed:.1f}s =====")
        if cancelled:
            self.log_line("（未完成的任务被中止；失败项可在下次运行时重试）")
        if self._run_fail:
            # 只提示到 GUI，不写进日志文件（否则日志里自己指自己，多余）
            self.log_line("（失败原因见本目录下的 pdf2cbz.log）", to_file=False)

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
        self.title(_APP_TITLE)
        self._progress_q = None
        if self._manager is not None:
            try:
                self._manager.shutdown()
            except Exception:
                pass
            self._manager = None


def run_selftest():
    """无界面自检：验证打包成 exe 后 PyMuPDF 与多进程 Pool 仍可用。

    窗口模式（--windowed）没有 stdout，结果写 %TEMP%\\pdf2cbz_selftest.txt，
    退出码 0 = 通过。用法：PDF2CBZ.exe --selftest
    """
    import zipfile
    import tempfile
    import pymupdf

    lines = []
    ok = False
    try:
        tmp = tempfile.mkdtemp(prefix="pdf2cbz_selftest_")
        src = os.path.join(tmp, "Vol 01.pdf")
        doc = pymupdf.open()
        for i in range(3):                       # 3 页 3 张不同图，去重后应为 3
            px = pymupdf.Pixmap(pymupdf.csRGB, 64, 64,
                                bytes((40 + i * 80, 90, 160)) * (64 * 64), 0)
            page = doc.new_page(width=200, height=200)
            page.insert_image(pymupdf.Rect(10, 10, 150, 150), stream=px.tobytes("png"))
        doc.save(src)
        doc.close()

        out = os.path.join(tmp, "out")
        os.makedirs(out)
        with multiprocessing.Pool(2) as pool:
            res = list(pool.imap_unordered(
                batch.convert_pdf_to_cbz, [batch.ConvertJob(src, out, False, False)]))
        path, success, msg = res[0]
        lines.append(f"转换: ok={success} msg={msg}")
        cbz = os.path.join(out, "Vol 01.cbz")
        n = len(zipfile.ZipFile(cbz).infolist()) if os.path.exists(cbz) else -1
        lines.append(f"CBZ 条目数: {n}")

        # 分段并行路径也要在冻结环境里验证：分两段写盘再合并
        import archive
        pa = os.path.join(out, "Vol 01.cbz.p00000.part")
        pb = os.path.join(out, "Vol 01.cbz.p00001.part")
        archive.write_part(src, pa, 0, 2)
        archive.write_part(src, pb, 2, 3)
        n_split = archive.merge_parts([pa, pb], os.path.join(out, "split.cbz"))
        lines.append(f"分段合并条目数: {n_split}")
        lines.append(f"PyMuPDF: {pymupdf.__version__ if hasattr(pymupdf, '__version__') else pymupdf.version}")
        lines.append(f"frozen: {getattr(sys, 'frozen', False)}")
        ok = bool(success) and n == 3 and n_split == 3
    except Exception:
        lines.append(traceback.format_exc())
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    report = os.path.join(tempfile.gettempdir(), "pdf2cbz_selftest.txt")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()   # PyInstaller --windowed 下多进程必需
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    App().mainloop()
