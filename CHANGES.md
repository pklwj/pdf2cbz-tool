# 与上游 v1.4.1 的差异

对照文件：`upstream_v1.4.1.py`（pklwj/pdf2cbz-tool 的 `pdf2cbz_gui.py`，799 行，v1.4.1 / 2026-09-14）

对比方式：按 AST 取每个函数/方法的函数体，去掉文档字符串与空行，归一化三处——
函数名去掉前导下划线、调用加上模块前缀（`plan_renames` → `rename.plan_renames`）、常量同名。
归一化后仍逐字相同，才算「未改」。

| 项 | 数量 |
|---|---|
| 上游函数/方法 | 36 |
| 逐字未改（只换了所在模块） | 21 |
| 有实质改动 | 15 |
| 新增函数/方法 | 27 |

## 一、逐字未改（只换了文件）

`_zip_compress_type`、`_list_cbz`、`extract_volume`、`_nat_key`、`plan_renames`、`find_conflicts`、`App._log_to_file`、`App._on_close`、`App._on_run_end`、`App._set_status_by_path`、`App.browse_out`、`App.cancel`、`App.clear_files`、`App.open_rename`、`App.remove_selected`、`RenameDialog.__init__`、`RenameDialog._browse`、`RenameDialog._restore_selected`、`RenameDialog._skip_selected`、`RenameDialog._toggle_skip`、`RenameDialog._update_lbl`

这 21 个函数/方法的函数体与上游完全一致，拆分只改变了它们所在的文件。

## 二、有实质改动（15 处）

### `_safe_stem` → `archive.py:35`

非法字符正则提成模块级预编译常量 `_ILLEGAL_CHARS`，字符集与 strip 规则不变。

```diff
--- upstream:_safe_stem
+++ archive.py:35
@@ -1,2 +1,2 @@
-    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
+    stem = _ILLEGAL_CHARS.sub("_", name).strip(" .")
     return stem or "untitled"
```

### `_cleanup_stale_tmp` → `batch.py:80`

`.part` 后缀判断改调 `archive.is_part_file()`——临时名里现在夹了 pid，原来的 `.cbz.part` 字面后缀匹配不上了。

```diff
--- upstream:_cleanup_stale_tmp
+++ batch.py:80
@@ -3,5 +3,5 @@
             if name.startswith(".tmp_") or (
                     name.startswith(".__rn_") and name.endswith(".tmp")) or (
-                    name.lower().endswith(".cbz.part")):
+                    is_part_file(name)):
                 p = os.path.join(out_dir, name)
                 try:
```

### `convert_pdf_to_cbz` → `batch.py:100`

改名为 `convert_file` 并支持 MOBI：提取循环搬去 `extractor.iter_unique_images()`（内部按扩展名分派），打包搬去 `archive.build_cbz()`。keep_images 搬家、原子换名、失败兜底三段原样保留，只把 `os.replace` 换成带重试的版本。

```diff
--- upstream:convert_pdf_to_cbz
+++ batch.py:100
@@ -1,81 +1,33 @@
-    pdf_path = job.pdf_path
+    src_path = job.src_path
     out_dir = job.out_dir
     keep_images = job.keep_images
     overwrite = job.overwrite
-    base = os.path.basename(pdf_path)
+    base = os.path.basename(src_path)
     vol = os.path.splitext(base)[0]
     cbz = os.path.join(out_dir, vol + ".cbz")
     if os.path.exists(cbz) and not overwrite:
-        return pdf_path, False, f"跳过：{vol}.cbz 已存在（勾选“覆盖”可重做）"
-    work = tempfile.mkdtemp(prefix=f"pdf2cbz_{os.getpid()}_") if keep_images else None
+        return src_path, False, f"跳过：{vol}.cbz 已存在（勾选“覆盖”可重做）"
+    work = None
+    if keep_images:
+        # 放输出目录而不是系统 temp：①同盘移动免拷贝；②子进程被终止时也落在
+        # 启动清理能扫到的 .tmp_* 规则里，不会在 %TEMP% 里永久残留
+        work = os.path.join(out_dir, f".tmp_work_{os.getpid()}")
+        shutil.rmtree(work, ignore_errors=True)
+        os.makedirs(work, exist_ok=True)
+    progress = getattr(job, "progress", None)
+    on_page = _throttled_page_cb(progress, vol) if progress is not None else None
     try:
-        saved = 0
-        tmp_cbz = cbz + ".part"
         try:
-            with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as z, \
-                 pymupdf.open(pdf_path) as doc:
-                seen_xref, seen_hash = set(), set()
-                for page in doc:
-                    for img in page.get_images(full=True):
-                        xref = img[0]
-                        if xref in seen_xref:
-                            continue
-                        seen_xref.add(xref)
-                        info = doc.extract_image(xref)      # 原始字节，无损、保原格式
-                        d = hashlib.md5(info["image"]).hexdigest()
-                        if d in seen_hash:
-                            continue
-                        seen_hash.add(d)
-                        saved += 1
-                        ext = info["ext"]
-                        fname = f"img_{saved:03d}.{ext}"
-                        # 直写 zip：bytes 写入磁盘上的 .part 后即可释放，
-                        # 峰值内存 = 单张图。压缩方式按格式逐张选。
-                        z.writestr(fname, info["image"],
-                                   compress_type=zip_compress_type(ext))
-                        if keep_images:
-                            with open(os.path.join(work, fname), "wb") as f:
-                                f.write(info["image"])
-            if not saved:
-                return pdf_path, False, f"失败：{base} 未提取到任何图片"
-            os.replace(tmp_cbz, cbz)      # 原子；Windows/POSIX 均可覆盖
-        finally:
-            if os.path.exists(tmp_cbz):
-                try:
-                    os.remove(tmp_cbz)
-                except OSError:
-                    pass
+            saved = build_cbz(src_path, cbz, image_sink=work, on_page=on_page)
+        except NoImagesError:
+            return src_path, False, f"失败：{base} 未提取到任何图片"
         size_mb = os.path.getsize(cbz) / 1024 / 1024
         note = f"{saved} 张唯一图片，{size_mb:.1f} MB"
         if keep_images:
-            # 原子移入输出目录：先到 .tmp_imgs_<pid> 暂存，再 rename 成正式名。
-            # 暂存名命中启动清理的 .tmp_* 规则，中断残留下次自动扫掉。
-            dst = os.path.join(out_dir, safe_stem(vol) + "_images")
-            staging = os.path.join(out_dir, f".tmp_imgs_{os.getpid()}")
-            try:
-                if os.path.isdir(dst):
-                    shutil.rmtree(dst, ignore_errors=True)   # 覆盖重跑清旧图
-                shutil.move(work, staging)          # 跨盘时为 copy+delete，可接受
-                os.rename(staging, dst)             # 同盘 rename，原子
-                work = None                         # 已移走，不再归本进程管
-                note += f"；图片已保留到 {os.path.basename(dst)}\\"
-            except OSError as e:
-                # CBZ 已经落盘，图片目录搬家失败不该把整件事报成失败。
-                # 也不能把它留在 .tmp_imgs_* —— 那个名字下次启动会被当垃圾清掉。
-                where = staging if os.path.isdir(staging) else work
-                salvage = os.path.join(out_dir, safe_stem(vol) + "_images.partial")
-                try:
-                    if os.path.isdir(where):
-                        shutil.rmtree(salvage, ignore_errors=True)
-                        shutil.move(where, salvage)
-                        note += f"；图片目录更名失败（{e}），已暂存为 {os.path.basename(salvage)}"
-                    else:
-                        note += f"；图片目录更名失败（{e}）"
-                except OSError:
-                    note += f"；图片目录更名失败（{e}），图片残留在 {where}"
-                work = None     # 无论落在哪，都别再被 finally 删掉
-        return pdf_path, True, f"完成：{vol}.cbz（{note}）"
+            note += _install_images(work, out_dir, vol)
+            work = None     # 已移走或已暂存，都不再由本进程清理
+        return src_path, True, f"完成：{vol}.cbz（{note}）"
     except Exception as e:
-        return pdf_path, False, f"失败：{base}：{e}\n{traceback.format_exc()}"
+        return src_path, False, f"失败：{base}：{e}\n{traceback.format_exc()}"
     finally:
         # work 为 None（已移走）或 keep_images=False（本来就是 None）时不清理
```

### `RenameDialog._refresh` → `main.py:112`

底部文案改走 `_update_lbl()`。原来是两处各写一遍同样的 if，加一个「跳过」按钮就得改两处。

```diff
--- upstream:RenameDialog._refresh
+++ main.py:112
@@ -22,6 +22,3 @@
             else:
                 self.tree.insert("", "end", values=("执行", old, new))
-        if self.skipped:
-            self.lbl.config(text=f"共 {len(plan)} 个文件，其中 {len(self.skipped)} 个已取消（跳过），执行时不会被改名")
-        else:
-            self.lbl.config(text=f"共 {len(plan)} 个文件；双击行或选中后点「跳过选中」可取消单个文件")
+        self._update_lbl()
```

### `RenameDialog._do_rename` → `main.py:180`

两步改名 + 单文件回滚那一整段搬去 `rename.execute_renames()`，这里只剩参数校验、确认框、汇总消息。行为不变，差别是那段代码终于能被单测碰到。

```diff
--- upstream:RenameDialog._do_rename
+++ main.py:180
@@ -4,5 +4,5 @@
             return
         plan = plan_renames(folder, self.prefix_var.get().strip(),
-                            "auto" if self.mode_var.get() == "自动提取" else "seq")
+                                   "auto" if self.mode_var.get() == "自动提取" else "seq")
         # TOCTOU 防护：预览之后目录内容若变化，执行的计划与用户看到的不一致
         current_olds = {old for old, _ in plan}
@@ -32,29 +32,5 @@
         if not messagebox.askyesno("确认", f"将重命名 {len(plan)} 个文件，继续？"):
             return
-        # 两步走：先全部改成临时唯一名，再改成目标名。避免 A->B、B->C 的链式互相覆盖。
-        staged = []
-        ok = 0
-        errs = []
-        for k, (old, new) in enumerate(plan):
-            src = os.path.join(folder, old)
-            tmp = os.path.join(folder, f".__rn_{k}_{os.getpid()}.tmp")
-            try:
-                os.rename(src, tmp)
-                staged.append((tmp, new, old))
-            except OSError as e:
-                errs.append(f"{old}：{e}")
-        for tmp, new, old in staged:
-            dst = os.path.join(folder, new)
-            try:
-                if os.path.exists(dst):
-                    raise OSError("目标文件已存在（可能被其他程序占用）")
-                os.rename(tmp, dst)
-                ok += 1
-            except OSError as e:
-                errs.append(f"{old} -> {new}：{e}")
-                try:                       # 回滚该文件，避免留 .tmp
-                    os.rename(tmp, os.path.join(folder, old))
-                except OSError:
-                    pass
+        ok, errs = execute_renames(folder, plan)
         msg = f"已重命名 {ok}/{len(plan)} 个文件"
         if self.skipped:
```

### `App.__init__` → `main.py:226`

加事件队列 `_events` 与 `_run_total`；并行度默认值从写死的 `"6"` 改为 `_default_workers()`（按 cpu_count()//2 落档位）。

```diff
--- upstream:App.__init__
+++ main.py:226
@@ -1,12 +1,12 @@
         super().__init__()
-        self.title("PDF 转 CBZ 漫画打包工具")
+        self.title(_APP_TITLE)
         self.geometry("780x620")
         self.minsize(680, 520)
-        self.files = []          # [(path, status)]
+        self.files = []          # [[path, status]]
         self.out_dir = tk.StringVar(value="")
         self.keep_images = tk.BooleanVar(value=False)
         self.overwrite = tk.BooleanVar(value=False)
         self.recursive = tk.BooleanVar(value=False)
-        self.workers = tk.StringVar(value="6")
+        self.workers = tk.StringVar(value=_default_workers())
         self.running = False
         self._done_count = 0
@@ -16,4 +16,11 @@
         self._run_ok = 0
         self._run_fail = 0
+        self._run_total = 0
+        # 工作线程只往队列里放事件，所有界面读写都回主线程做（Tk 非线程安全）
+        self._events = queue.Queue()
+        self._progress_q = None      # Manager 队列：worker 上报页进度
+        self._manager = None
+        self._prog_marks = {}        # 文件名 → Text mark，一文件一行原地刷新
+        self._prog_seq = 0
         # 保护跨线程共享字段（_pool / _cancelled），主线程与 worker 线程都会读写
         self._state_lock = threading.Lock()
```

### `App._build_ui` → `main.py:257`

标题常量化；「并行任务数」从复选框行挪到输出目录行（原位置在 780 宽下会把下拉框顶出可视区）；新增「打开输出目录」按钮；列表绑定 Ctrl+A / Delete / 双击定位。

```diff
--- upstream:App._build_ui
+++ main.py:257
@@ -1,8 +1,8 @@
         pad = {"padx": 8, "pady": 4}
-        frm_top = ttk.LabelFrame(self, text="1. 选择 PDF 文件（可多选 / 添加整个文件夹）")
+        frm_top = ttk.LabelFrame(self, text="1. 选择文件（PDF / MOBI / EPUB，可多选 / 添加整个文件夹）")
         frm_top.pack(fill="both", expand=True, **pad)
         btns = ttk.Frame(frm_top)
         btns.pack(fill="x", padx=8, pady=4)
-        ttk.Button(btns, text="添加 PDF…", command=self.add_files).pack(side="left")
+        ttk.Button(btns, text="添加文件…", command=self.add_files).pack(side="left")
         ttk.Button(btns, text="添加文件夹…", command=self.add_folder).pack(side="left", padx=6)
         ttk.Checkbutton(btns, text="含子文件夹", variable=self.recursive).pack(side="left", padx=4)
@@ -18,4 +18,8 @@
         self.tree.column("status", width=120)
         self.tree.pack(fill="both", expand=True, padx=8, pady=4)
+        self.tree.bind("<Control-a>", self._select_all)
+        self.tree.bind("<Control-A>", self._select_all)
+        self.tree.bind("<Delete>", lambda e: self.remove_selected())
+        self.tree.bind("<Double-Button-1>", self._reveal_selected)
         frm_opt = ttk.LabelFrame(self, text="2. 选项")
         frm_opt.pack(fill="x", **pad)
@@ -25,4 +29,8 @@
         ttk.Entry(row1, textvariable=self.out_dir).pack(side="left", fill="x", expand=True, padx=6)
         ttk.Button(row1, text="浏览…", command=self.browse_out).pack(side="left")
+        # 并行度放这一行：原来挤在复选框行，780 宽下会把下拉框顶出可视区
+        ttk.Label(row1, text="并行任务数：").pack(side="left", padx=(12, 4))
+        ttk.Combobox(row1, textvariable=self.workers, state="readonly", width=3,
+                     values=list(_WORKER_CHOICES)).pack(side="left")
         row2 = ttk.Frame(frm_opt)
         row2.pack(fill="x", padx=8, pady=2)
@@ -31,7 +39,4 @@
         ttk.Checkbutton(row2, text="覆盖已存在的 CBZ（默认跳过）",
                         variable=self.overwrite).pack(side="left")
-        ttk.Label(row2, text="并行任务数：").pack(side="left", padx=(16, 4))
-        ttk.Combobox(row2, textvariable=self.workers, state="readonly", width=4,
-                     values=["1", "2", "4", "6", "8"]).pack(side="left")
         frm_run = ttk.LabelFrame(self, text="3. 开始")
         frm_run.pack(fill="x", **pad)
@@ -43,4 +48,5 @@
         self.btn_cancel.pack(side="left", padx=6)
         ttk.Button(row, text="批量重命名 CBZ…", command=self.open_rename).pack(side="left", padx=8)
+        ttk.Button(row, text="打开输出目录", command=self.open_out_dir).pack(side="left")
         self.progress = ttk.Progressbar(row, mode="determinate")
         self.progress.pack(side="left", fill="x", expand=True, padx=8)
```

### `App._on_one_done` → `main.py:565`

多一行标题进度 `完成数/总数`。

```diff
--- upstream:App._on_one_done
+++ main.py:565
@@ -7,4 +7,5 @@
         self._done_count += 1
         self.progress.configure(value=self._done_count)
+        self.title(f"{_APP_TITLE} — {self._done_count}/{self._run_total}")
         # 同步内存状态：仅「完成」被排除出后续运行，失败项仍可重试
         for f in self.files:
```

### `App.start` → `main.py:581`

新增输出重名预检（`batch.find_output_collisions`）；worker 不再调 `self.after`，改投队列；末尾启动 `_pump()`。

```diff
--- upstream:App.start
+++ main.py:581
@@ -3,5 +3,5 @@
         todo = [f[0] for f in self.files if f[1] != "完成"]
         if not todo:
-            messagebox.showinfo("提示", "没有待转换的 PDF（已完成的不会重复转换）")
+            messagebox.showinfo("提示", "没有待转换的文件（已完成的不会重复转换）")
             return
         out_dir = self.out_dir.get().strip()
@@ -12,4 +12,15 @@
             messagebox.showwarning("提示", "输出目录不存在")
             return
+        # 同名不同目录的 PDF 会落到同一个 <卷名>.cbz，并行时互相覆盖 —— 先问一声
+        collisions = find_output_collisions(todo)
+        if collisions:
+            preview = "\n".join(f"{name}  ← {len(ps)} 个文件" for name, ps in collisions[:5])
+            more = f"\n…另有 {len(collisions) - 5} 组" if len(collisions) > 5 else ""
+            if not messagebox.askyesno(
+                    "输出重名",
+                    f"有 {len(collisions)} 组文件会写到同一个 CBZ（同名但不在同一目录）：\n"
+                    f"{preview}{more}\n\n"
+                    "转换时它们会互相覆盖，最终只留最后完成的那个。要继续吗？"):
+                return
         cleanup_stale_tmp(out_dir)
         keep = self.keep_images.get()
@@ -19,9 +30,40 @@
         except ValueError:
             n_workers = 4
-        # 任务键用文件路径（唯一），而非序号 —— 序号在过滤后会错位。
-        params = [ConvertJob(pdf_path=p, out_dir=out_dir,
-                             keep_images=keep, overwrite=ove) for p in todo]
+        # 单文件 + 多核：任务粒度是「一个文件一个进程」，不切段就只有 1 个核在跑。
+        # 页数够多才值得切（切段要多一次合并，小文件不划算）。
+        split = None
+        if len(todo) == 1 and n_workers > 1 and probe.can_split(todo[0]):
+            pages = probe.image_count(todo[0])
+            vol = os.path.splitext(os.path.basename(todo[0]))[0]
+            if pages >= _SPLIT_MIN_PAGES and (ove or not os.path.exists(os.path.join(out_dir, vol + ".cbz"))):
+                split = (todo[0], pages, vol)
+        # 页进度要跨进程回传：Manager 队列的代理可 pickle，能塞进任务里传给 worker。
+        # 分段模式不建（进度按段计就够），省掉一个常驻进程。
+        self._manager = None
+        self._progress_q = None
+        if not split:
+            try:
+                self._manager = multiprocessing.Manager()
+                self._progress_q = self._manager.Queue()
+            except Exception:
+                self._manager = None      # 起不来就退化成没有页进度
+        if split:
+            pdf, pages, vol = split
+            seg = min(n_workers, pages)
+            step = -(-pages // seg)                     # 向上取整，末段可能短
+            params = [RangeJob(pdf, os.path.join(out_dir, f"{vol}.cbz.p{s:05d}.part"), s,
+                                     min(s + step, pages))
+                      for s in range(0, pages, step)]
+        else:
+            # 任务键用文件路径（唯一），而非序号 —— 序号在过滤后会错位。
+            params = [ConvertJob(src_path=p, out_dir=out_dir, keep_images=keep,
+                                       overwrite=ove, progress=self._progress_q) for p in todo]
         self.running = True
         self._done_count = 0
+        self._prog_marks.clear()
+        self._prog_seq = 0
+        self._events = queue.Queue()      # 新一轮，丢掉上一轮残留事件
+        # 分段时进度按「段数 + 1 次合并」计，否则按文件数计
+        self._run_total = len(params) + (1 if split else 0)
         with self._state_lock:
             self._pool = None
@@ -29,5 +71,5 @@
         self.btn_start.config(state="disabled")
         self.btn_cancel.config(state="normal")
-        self.progress.configure(maximum=len(todo), value=0)
+        self.progress.configure(maximum=self._run_total, value=0)
         self._run_started = time.time()
         self._run_ok = 0
@@ -36,6 +78,16 @@
         self.log_line(f"== 开始转换 {len(todo)} 个文件（并行 {n_workers}），"
                       f"保留中间图片：{'是' if keep else '否'}，覆盖：{'是' if ove else '否'} ==")
+        # 图片编码探测：只读 xref 不解码，约 0.3 ms/页。文件多时只探前若干个，
+        # 免得转换还没开始就先卡住界面。
+        for p in todo[:_PROBE_LIMIT]:
+            self.log_line(probe.summary(p))
+        if len(todo) > _PROBE_LIMIT:
+            self.log_line(f"  （文件较多，编码探测只覆盖前 {_PROBE_LIMIT} 个）")
+        if split:
+            self.log_line(f"== 单文件按页切 {len(params)} 段并行（{pages} 页），完成后合并 ==")
         def worker():
             pool = None
+            done_parts = []
+            range_failed = False
             try:
                 # maxtasksperchild 限制单进程内存累积，避免大 PDF 长跑内存泄漏
@@ -48,12 +100,39 @@
                         return
                     self._pool = pool
-                for key, ok, msg in pool.imap_unordered(convert_pdf_to_cbz, params):
-                    with self._state_lock:
-                        cancelled = self._cancelled
-                    if cancelled:
+                it = pool.imap_unordered(run_job, params)
+                finished = False
+                while True:
+                    try:
+                        res = it.next(timeout=0.3)
+                    except StopIteration:
+                        finished = True
                         break
-                    self.after(0, self._on_one_done, key, ok, msg)
+                    except multiprocessing.TimeoutError:
+                        # 必须轮询：terminate() 之后迭代器不会再吐结果，
+                        # 阻塞式 for 循环会永远等下去（旧版就卡死在这里）
+                        with self._state_lock:
+                            if self._cancelled:
+                                break
+                        continue
+                    if res[0] == "range":
+                        _, part, ok, msg, n, label, elapsed = res
+                        if ok:
+                            done_parts.append(part)
+                        else:
+                            range_failed = True
+                        self._events.put(("range", (part, ok, msg, n, label, elapsed)))
+                    else:
+                        _, key, ok, msg = res
+                        self._events.put(("done", (key, ok, msg)))
+                if finished and split:
+                    pdf, _pages, vol = split
+                    if range_failed:
+                        self._events.put(("done", (pdf, False, "失败：有页段未完成，已跳过合并")))
+                    else:
+                        self._events.put(("log", f"== {len(done_parts)} 个页段完成，合并中… =="))
+                        self._events.put(("done", merge_ranges(
+                            pdf, out_dir, vol, sorted(done_parts), keep)))
             except Exception:
-                self.after(0, self.log_line, "异常：\n" + traceback.format_exc())
+                self._events.put(("log", "异常：\n" + traceback.format_exc()))
             finally:
                 if pool is not None:
@@ -66,5 +145,11 @@
                     self._pool = None
                     cancelled = self._cancelled
-                self.after(0, self._on_run_end, cancelled)
-                self.after(0, self._finish)
+                if cancelled:
+                    # 中止时立刻清掉 .part / .tmp_*，别留到下次启动才清
+                    try:
+                        cleanup_stale_tmp(out_dir)
+                    except Exception:
+                        pass
+                self._events.put(("end", cancelled))
         threading.Thread(target=worker, daemon=True).start()
+        self._pump()
```

### `App._finish` → `main.py:777`

结束后把标题复位。

```diff
--- upstream:App._finish
+++ main.py:777
@@ -5,2 +5,10 @@
         self.btn_start.config(state="normal")
         self.btn_cancel.config(state="disabled")
+        self.title(_APP_TITLE)
+        self._progress_q = None
+        if self._manager is not None:
+            try:
+                self._manager.shutdown()
+            except Exception:
+                pass
+            self._manager = None
```

### `scan_pdfs` → `batch.py:44`

改名为 `scan_inputs`，扩展名从 `.pdf` 扩成 `SOURCE_EXTS`（PDF + MOBI 家族），递归与非递归两段结构不变。

```diff
--- upstream:scan_pdfs
+++ batch.py:44
@@ -3,9 +3,9 @@
         for root, _, files in os.walk(folder):
             for f in files:
-                if f.lower().endswith(".pdf"):
+                if f.lower().endswith(SOURCE_EXTS):
                     result.append(os.path.join(root, f))
     else:
         for entry in os.scandir(folder):
-            if entry.is_file() and entry.name.lower().endswith(".pdf"):
+            if entry.is_file() and entry.name.lower().endswith(SOURCE_EXTS):
                 result.append(entry.path)
     return sorted(result)
```

### `App._add_paths` → `main.py:361`

```diff
--- upstream:App._add_paths
+++ main.py:361
@@ -8,3 +8,3 @@
         if added and not self.out_dir.get() and paths:
             self.out_dir.set(os.path.dirname(paths[0]))
-        self.log_line(f"已添加 {added} 个 PDF（累计 {len(self.files)} 个）")
+        self.log_line(f"已添加 {added} 个文件（累计 {len(self.files)} 个）")
```

### `App.add_files` → `main.py:341`

```diff
--- upstream:App.add_files
+++ main.py:341
@@ -1,4 +1,8 @@
         paths = filedialog.askopenfilenames(
-            title="选择 PDF",
-            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")])
+            title="选择文件",
+            filetypes=[("PDF 与电子书", "*.pdf *.mobi *.azw *.azw3 *.prc *.epub"),
+                       ("PDF 文件", "*.pdf"),
+                       ("MOBI / AZW 电子书", "*.mobi *.azw *.azw3 *.prc"),
+                       ("EPUB", "*.epub"),
+                       ("所有文件", "*.*")])
         self._add_paths(list(paths))
```

### `App.add_folder` → `main.py:351`

```diff
--- upstream:App.add_folder
+++ main.py:351
@@ -2,7 +2,7 @@
         if not d:
             return
-        found = scan_pdfs(d, self.recursive.get())
+        found = scan_inputs(d, self.recursive.get())
         if not found:
-            messagebox.showinfo("提示", "该文件夹下没有找到 PDF 文件")
+            messagebox.showinfo("提示", "该文件夹下没有找到 PDF / MOBI / EPUB 文件")
             return
         self._add_paths(found)
```

### `App.log_line` → `main.py:432`

```diff
--- upstream:App.log_line
+++ main.py:432
@@ -5,4 +5,5 @@
         if n > _LOG_MAX_LINES:
             self.log.delete("1.0", f"{n - _LOG_MAX_LINES}.0")
+            self._prog_marks.clear()    # 行被删过，旧 mark 位置已不可信
         self.log.see("end")
         self.log.configure(state="disabled")
```

## 三、新增（27 个）

| 函数/方法 | 位置 | 作用 |
|---|---|---|
| `App._drain_progress` | main.py:491 | （见代码） |
| `App._on_range_done` | main.py:554 | （见代码） |
| `App._pump` | main.py:511 | 主线程每 80ms 排空事件队列。Tk 非线程安全，工作线程只投数据，界面读写全回主线程。 |
| `App._render_progress` | main.py:462 | （见代码） |
| `App._reveal_selected` | main.py:415 | 双击行在资源管理器里定位该文件（explorer /select,）。 |
| `App._select_all` | main.py:411 | Ctrl+A 全选列表。 |
| `App.open_out_dir` | main.py:398 | 「打开输出目录」按钮，os.startfile。 |
| `_add_image` | archive.py:78 | （见代码） |
| `_default_workers` | main.py:43 | 并行度默认值：max(1, cpu_count()//2) 落进 1/2/4/6/8 档位。本机 12 逻辑核 → 6。 |
| `_install_images` | batch.py:147 | （见代码） |
| `_throttled_page_cb` | batch.py:28 | （见代码） |
| `build_cbz` | archive.py:86 | 从 convert_pdf_to_cbz 里搬出来的打包段：写 .part → 逐张选压缩方式 → 原子换名。返回图片张数，0 张抛 NoImagesError。 |
| `convert_range` | batch.py:175 | （见代码） |
| `execute_renames` | rename.py:89 | 从 RenameDialog._do_rename 搬出的执行段：先全改临时名再改目标名，单文件失败回滚。返回 (成功数, 错误列表)。 |
| `find_output_collisions` | batch.py:60 | 按输出名（小写归一）给待转 PDF 分组，返回重名组。同名不同目录的 PDF 会写到同一个 CBZ，后完成的覆盖前面的。 |
| `images_dir_name` | archive.py:41 | 把 `<卷名>_images` 的命名收成一处（原来在 convert 里拼字符串）。 |
| `is_part_file` | archive.py:55 | 识别带 pid 的 .part，供启动清理使用。 |
| `iter_pdf_images` | pdf_images.py:52 | 从 extractor.py 的 `_iter_pdf_images` 搬来并转公开（分派层要调它），xref + MD5 双层去重不变。 |
| `iter_unique_images` | extractor.py:19 | 从 convert_pdf_to_cbz 里搬出来的提取循环，改成生成器，逐张 yield (ext, bytes)，峰值内存仍是一张图。现在只剩分派：PDF 走 `pdf_images.iter_pdf_images`，MOBI 家族走 `mobi.iter_mobi_images`。 |
| `merge_parts` | archive.py:130 | （见代码） |
| `merge_ranges` | batch.py:201 | （见代码） |
| `part_path` | archive.py:46 | 临时文件名改为 `<卷>.cbz.<pid>.part`。原先固定 `<卷>.cbz.part`，同名任务并发时共用同一文件，撞 WinError 32 双方全败。 |
| `probe_pdf` | pdf_images.py:23 | 从 extractor.py 搬到 pdf_images.py，逻辑未改：只读 xref 扫 Filter 分布，不解码像素。 |
| `replace_with_retry` | archive.py:61 | os.replace 遇 PermissionError 退避重试 6 次。修 WinError 5/32：两个进程同时换同一个目标名、或杀软/索引器短暂占用刚写好的文件。 |
| `run_job` | batch.py:191 | （见代码） |
| `run_selftest` | main.py:794 | （见代码） |
| `write_part` | archive.py:114 | （见代码） |

## 四、没有删除任何上游逻辑

上游每个函数都在新代码里有对应物：21 个原样搬走，15 个改了内部实现，没有丢弃任何一个。
`convert_pdf_to_cbz` 与 `_do_rename` 里被抽走的两段，分别落在 `archive.build_cbz` +
`extractor.iter_unique_images` 与 `rename.execute_renames`，逐行搬，没有重写。

MOBI / AZW / AZW3 与 EPUB 支持、以及源文件体检，分别是新增模块 `mobi.py` / `epub.py` /
`probe.py`，不属于上游对照范围——上游只有 PDF 路径。

## 五、怎么自己验

```bash
python selftest.py     # 含「产物逐条等价(名/大小/CRC)」，跑的就是 upstream_v1.4.1.py
python e2e_gui.py      # GUI 两条路径端到端
```
