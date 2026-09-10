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
import threading
import multiprocessing
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pymupdf


def scan_pdfs(folder, recursive=False):
    if recursive:
        result = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".pdf"):
                    result.append(os.path.join(root, f))
        return sorted(result)
    return sorted(glob.glob(os.path.join(folder, "*.pdf")))


def convert_pdf_to_cbz(args):
    """转换单个 PDF（多进程 worker）。参数打包为元组以便 Pool 分发。
    返回 (任务序号, 是否成功, 消息)。不依赖 GUI，可被 pickle。"""
    idx, pdf_path, out_dir, keep_images, overwrite = args
    base = os.path.basename(pdf_path)
    vol = os.path.splitext(base)[0]
    cbz = os.path.join(out_dir, vol + ".cbz")

    if os.path.exists(cbz) and not overwrite:
        return idx, False, f"跳过：{vol}.cbz 已存在（勾选“覆盖”可重做）"

    work = os.path.join(out_dir, f".tmp_{vol}_{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    try:
        doc = pymupdf.open(pdf_path)
        seen_xref, seen_hash, saved = set(), set(), []
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
        doc.close()

        if not saved:
            return idx, False, f"失败：{base} 未提取到任何图片"

        with zipfile.ZipFile(cbz, "w", zipfile.ZIP_STORED) as z:
            for fn in saved:
                z.write(os.path.join(work, fn), arcname=fn)

        size_mb = os.path.getsize(cbz) / 1024 / 1024
        if not keep_images:
            shutil.rmtree(work, ignore_errors=True)
        return idx, True, f"完成：{vol}.cbz（{len(saved)} 张唯一图片，{size_mb:.1f} MB）"
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return idx, False, f"失败：{base}：{e}"


# ---------- 批量重命名 ----------
_RE_VOL = re.compile(r"(?:[Vv]ol\.?|第)\s*(\d{1,4})")
_RE_NUM = re.compile(r"(\d{1,4})")


def extract_volume(filename):
    """从文件名提取卷号（int 或 None）。优先 Vol.xx / 第xx卷，再取第一个数字。"""
    m = _RE_VOL.search(filename)
    if m:
        return int(m.group(1))
    m = _RE_NUM.search(filename)
    if m:
        return int(m.group(1))
    return None


def plan_renames(folder, prefix, mode="auto"):
    """计算重命名计划。返回 [(原名, 新名)]，不执行。"""
    cbzs = sorted(glob.glob(os.path.join(folder, "*.cbz")))
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


class RenameDialog(tk.Toplevel):
    def __init__(self, master, default_dir):
        super().__init__(master)
        self.title("批量重命名 CBZ")
        self.geometry("620x460")
        self.minsize(540, 360)

        self.dir_var = tk.StringVar(value=default_dir)
        self.prefix_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="auto")

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

        cols = ("old", "new")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=10)
        self.tree.heading("old", text="原文件名")
        self.tree.heading("new", text="新文件名")
        self.tree.column("old", width=280)
        self.tree.column("new", width=280)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)

        self.lbl = ttk.Label(self, text="")
        self.lbl.pack(fill="x", padx=8)

        row3 = ttk.Frame(self)
        row3.pack(fill="x", padx=8, pady=6)
        ttk.Button(row3, text="执行重命名", command=self._do_rename).pack(side="right")
        ttk.Button(row3, text="刷新", command=self._refresh).pack(side="right", padx=6)

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
        for old, new in plan:
            self.tree.insert("", "end", values=(old, new))
        self.lbl.config(text=f"共 {len(plan)} 个文件，预览如上；请确认无误后执行")

    def _do_rename(self):
        folder = self.dir_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("错误", "目录不存在")
            return
        plan = plan_renames(folder, self.prefix_var.get().strip(),
                            "auto" if self.mode_var.get() == "自动提取" else "seq")
        targets = [os.path.join(folder, new) for _, new in plan]
        conflicts = [new for (old, new), t in zip(plan, targets)
                     if os.path.exists(t) and os.path.basename(t) != old]
        if conflicts:
            messagebox.showerror("冲突", "以下新文件名已存在，未执行：\n" + "\n".join(conflicts))
            return
        if not plan:
            messagebox.showinfo("提示", "没有可重命名的文件")
            return
        if not messagebox.askyesno("确认", f"将重命名 {len(plan)} 个文件，继续？"):
            return
        ok = 0
        for old, new in plan:
            src = os.path.join(folder, old)
            dst = os.path.join(folder, new)
            try:
                os.rename(src, dst)
                ok += 1
            except OSError as e:
                messagebox.showerror("失败", f"{old}：{e}")
        messagebox.showinfo("完成", f"已重命名 {ok}/{len(plan)} 个文件")
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
        self.workers = tk.StringVar(value="4")
        self.running = False
        self._done_count = 0

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
                     values=["1", "2", "4", "8"]).pack(side="left")

        frm_run = ttk.LabelFrame(self, text="3. 开始")
        frm_run.pack(fill="x", **pad)

        row = ttk.Frame(frm_run)
        row.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(row, text="开始转换", command=self.start)
        self.btn_start.pack(side="left")
        ttk.Button(row, text="批量重命名 CBZ…", command=self.open_rename).pack(side="left", padx=8)
        self.progress = ttk.Progressbar(row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.log = tk.Text(frm_run, height=8, state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=4)

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
        for item in self.tree.selection():
            i = self.tree.index(item)
            self.tree.delete(item)
            self.files.pop(i)

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
    def _set_status(self, i, status):
        children = self.tree.get_children()
        if 0 <= i < len(children):
            item = children[i]
            vals = list(self.tree.item(item, "values"))
            vals[2] = status
            self.tree.item(item, values=vals)

    def _on_one_done(self, idx, ok, msg):
        self.log_line(msg)
        self._set_status(idx, "完成" if ok else "失败/跳过")
        self._done_count += 1
        self.progress.configure(value=self._done_count)

    def start(self):
        if self.running:
            return
        todo = [f[0] for f in self.files if f[1] != "完成"]
        if not todo:
            messagebox.showinfo("提示", "请先添加 PDF 文件")
            return
        out_dir = self.out_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("提示", "请选择输出目录")
            return
        if not os.path.isdir(out_dir):
            messagebox.showwarning("提示", "输出目录不存在")
            return

        keep = self.keep_images.get()
        ove = self.overwrite.get()
        n_workers = int(self.workers.get())
        params = [(i, path, out_dir, keep, ove) for i, path in enumerate(todo)]

        self.running = True
        self._done_count = 0
        self.btn_start.config(state="disabled")
        self.progress.configure(maximum=len(todo), value=0)
        self.log_line(f"== 开始转换 {len(todo)} 个文件（并行 {n_workers}），输出到：{out_dir} ==")

        def worker():
            try:
                with multiprocessing.Pool(n_workers) as pool:
                    for idx, ok, msg in pool.imap_unordered(convert_pdf_to_cbz, params):
                        self.after(0, self._on_one_done, idx, ok, msg)
                self.after(0, self.log_line, "== 全部处理结束 ==")
            except Exception as e:
                self.after(0, self.log_line, f"异常：{e}")
            finally:
                self.after(0, self._finish)

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self):
        self.running = False
        self.btn_start.config(state="normal")


if __name__ == "__main__":
    multiprocessing.freeze_support()   # PyInstaller --windowed 下多进程必需
    App().mainloop()
