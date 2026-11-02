# -*- coding: utf-8 -*-
"""批量转换：任务参数、单文件 worker、目录扫描、崩溃残留清理。

worker 必须是模块级函数、入参必须可 pickle —— multiprocessing 靠 pickle 传参。
PyMuPDF 底层 MuPDF 不保证线程安全，所以并行只能走多进程（每进程独立开 PDF）。
"""
import os
import shutil
import tempfile
import time
import traceback
from collections import namedtuple

import pymupdf

import archive

# 转换任务参数：用具名元组替代裸元组，避免位置传参错位，且可被 pickle。
# progress 是 Manager().Queue() 的代理，worker 往里丢 (标签, 已处理页, 总页数)；
# 不需要进度时留 None（旧的四参调用保持可用）。
ConvertJob = namedtuple("ConvertJob", "pdf_path out_dir keep_images overwrite progress",
                        defaults=(None,))
# 页段任务：单文件吃多核用。一整册按页切段，各段写自己的临时 zip，最后合并。
RangeJob = namedtuple("RangeJob", "pdf_path part start end")


def _throttled_page_cb(q, label, interval=0.4):
    """页进度回调：限流后再塞进队列，避免每页都跨进程通信把日志冲爆。"""
    last = [0.0]

    def cb(pno, total):
        now = time.time()
        if pno + 1 >= total or now - last[0] >= interval:
            last[0] = now
            try:
                q.put((label, pno + 1, total))
            except Exception:
                pass          # 队列已关闭（用户点了停止）就算了

    return cb


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


def find_output_collisions(pdf_paths):
    """找出会写到同一个 <卷名>.cbz 的 PDF 组。

    不同目录下的同名 PDF（如两套书都叫 Vol 01.pdf）落到同一输出目录时，
    后完成的会覆盖先完成的，且预览里看不出来。返回 [(输出文件名, [pdf...])]，
    只含真正冲突的组；组内保持输入顺序。
    """
    groups = {}
    order = []
    for p in pdf_paths:
        vol = os.path.splitext(os.path.basename(p))[0]
        name = vol + ".cbz"
        key = name.lower()          # Windows 文件系统大小写不敏感
        if key not in groups:
            groups[key] = (name, [])
            order.append(key)
        groups[key][1].append(p)
    return [groups[k] for k in order if len(groups[k][1]) > 1]


def cleanup_stale_tmp(out_dir):
    """清理上次崩溃/中断遗留的中间产物。覆盖三类命名：
    早期版本的 `.tmp_*`、重命名回滚的 `.__rn_*.tmp`、打包中断的 `*.cbz.part`。"""
    try:
        for name in os.listdir(out_dir):
            if name.startswith(".tmp_") or (
                    name.startswith(".__rn_") and name.endswith(".tmp")) or (
                    archive.is_part_file(name)):
                p = os.path.join(out_dir, name)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except OSError:
                    pass
    except OSError:
        pass


def convert_pdf_to_cbz(job):
    """转换单个 PDF（多进程 worker）。入参为 ConvertJob（可 pickle）。
    返回 (pdf_path, 是否成功, 消息)。用路径而非序号回写状态，避免顺序错位。

    keep_images=False：流式直写 zip（单张图片进内存，写完即释放，零临时文件）。
    keep_images=True：提取到系统 temp 的唯一目录（并行 worker 互不干扰），
    打包完成后原子移入输出目录的 <卷名>_images\\。"""
    pdf_path = job.pdf_path
    out_dir = job.out_dir
    keep_images = job.keep_images
    overwrite = job.overwrite
    base = os.path.basename(pdf_path)
    vol = os.path.splitext(base)[0]
    cbz = os.path.join(out_dir, vol + ".cbz")

    if os.path.exists(cbz) and not overwrite:
        return pdf_path, False, f"跳过：{vol}.cbz 已存在（勾选“覆盖”可重做）"

    work = tempfile.mkdtemp(prefix=f"pdf2cbz_{os.getpid()}_") if keep_images else None
    progress = getattr(job, "progress", None)
    on_page = _throttled_page_cb(progress, vol) if progress is not None else None
    try:
        try:
            saved = archive.build_cbz(pdf_path, cbz, image_sink=work, on_page=on_page)
        except archive.NoImagesError:
            return pdf_path, False, f"失败：{base} 未提取到任何图片"

        size_mb = os.path.getsize(cbz) / 1024 / 1024
        note = f"{saved} 张唯一图片，{size_mb:.1f} MB"
        if keep_images:
            note += _install_images(work, out_dir, vol)
            work = None     # 已移走或已暂存，都不再由本进程清理
        return pdf_path, True, f"完成：{vol}.cbz（{note}）"
    except Exception as e:
        return pdf_path, False, f"失败：{base}：{e}\n{traceback.format_exc()}"
    finally:
        # work 为 None（已移走）或 keep_images=False（本来就是 None）时不清理
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def _install_images(work, out_dir, vol):
    """把临时图片目录原子移入输出目录，返回衔接用的提示片段（以「；」开头）。

    CBZ 已经落盘，图片目录搬家失败不该把整件事报成失败；也不能把它留在
    .tmp_imgs_*（那个名字下次启动会被当垃圾清掉），所以失败时改名为
    <卷名>_images.partial 保住数据。
    """
    dst = os.path.join(out_dir, archive.images_dir_name(vol))
    staging = os.path.join(out_dir, f".tmp_imgs_{os.getpid()}")
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)   # 覆盖重跑清旧图
        shutil.move(work, staging)          # 跨盘时为 copy+delete，可接受
        os.rename(staging, dst)             # 同盘 rename，原子
        return f"；图片已保留到 {os.path.basename(dst)}\\"
    except OSError as e:
        where = staging if os.path.isdir(staging) else work
        salvage = os.path.join(out_dir, archive.images_dir_name(vol) + ".partial")
        try:
            if os.path.isdir(where):
                shutil.rmtree(salvage, ignore_errors=True)
                shutil.move(where, salvage)
                return f"；图片目录更名失败（{e}），已暂存为 {os.path.basename(salvage)}"
            return f"；图片目录更名失败（{e}）"
        except OSError:
            return f"；图片目录更名失败（{e}），图片残留在 {where}"


def count_pages(pdf_path):
    """页数。读不出来返回 0，交给调用方按「不分段」处理。"""
    try:
        with pymupdf.open(pdf_path) as doc:
            return doc.page_count
    except Exception:
        return 0


def convert_range(job):
    """页段 worker：把 [start, end) 页写成临时 zip。

    返回 (part, 是否成功, 消息, 图片数, 页区间文案, 耗时秒)，消息只在失败时有内容。
    """
    t0 = time.time()
    label = f"第 {job.start + 1}–{job.end} 页"
    try:
        n, _ = archive.write_part(job.pdf_path, job.part, job.start, job.end)
    except Exception as e:
        return job.part, False, f"{e}\n{traceback.format_exc()}", 0, label, 0.0
    if not n:
        return job.part, False, f"{label}没有图片", 0, label, 0.0
    return job.part, True, "", n, label, round(time.time() - t0, 1)


def run_job(job):
    """Pool 统一入口：文件任务与页段任务混在同一个池里跑。

    返回值带类型标记，主线程据此决定是回写文件状态还是推进分段进度。
    """
    if isinstance(job, RangeJob):
        return ("range",) + convert_range(job)
    return ("file",) + convert_pdf_to_cbz(job)


def merge_ranges(pdf_path, out_dir, vol, parts, keep_images):
    """合并页段并收尾，返回与 convert_pdf_to_cbz 同形的 (路径, 是否成功, 消息)。"""
    base = os.path.basename(pdf_path)
    cbz = os.path.join(out_dir, vol + ".cbz")
    work = tempfile.mkdtemp(prefix=f"pdf2cbz_{os.getpid()}_") if keep_images else None
    try:
        saved = archive.merge_parts(parts, cbz, image_sink=work)
        size_mb = os.path.getsize(cbz) / 1024 / 1024
        note = f"{saved} 张唯一图片，{size_mb:.1f} MB"
        if keep_images:
            note += _install_images(work, out_dir, vol)
            work = None
        return pdf_path, True, f"完成：{vol}.cbz（{note}）"
    except archive.NoImagesError:
        return pdf_path, False, f"失败：{base} 未提取到任何图片"
    except Exception as e:
        return pdf_path, False, f"失败：{base}：{e}\n{traceback.format_exc()}"
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        for p in parts:                     # 段文件用完即删，别在输出目录留垃圾
            try:
                os.remove(p)
            except OSError:
                pass
