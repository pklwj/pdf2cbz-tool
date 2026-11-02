# -*- coding: utf-8 -*-
"""CBZ 打包：按图片格式逐张选压缩方式，原子写入，绝不留半截文件。

为什么要逐张选：
  - 已压缩格式（jpg/png/webp/gif/jpx/jbig2）再 DEFLATE 一遍几乎不省体积，纯烧 CPU → ZIP_STORED；
  - 未压缩位图（bmp/tiff/ppm）与未知格式不压就等着 CBZ 比原 PDF 还大 → ZIP_DEFLATED。
"""
import os
import re
import time
import zipfile
import hashlib

import extractor

# 本身就是压缩格式的图片：再压一遍没有收益。
PRE_COMPRESSED_EXTS = frozenset({
    "jpg", "jpeg", "png", "webp", "gif", "jpx", "jp2", "jbig2", "jb2",
})

# Windows 文件名非法字符 + 控制字符。
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class NoImagesError(Exception):
    """源文件里一张图都没提取到（纯文字 PDF，或 MOBI / EPUB 里没有图片资源）。"""


def zip_compress_type(ext):
    """按图片扩展名选 zip 压缩方式。未知格式一律 DEFLATE（宁慢勿胖）。"""
    return (zipfile.ZIP_STORED if (ext or "").lower() in PRE_COMPRESSED_EXTS
            else zipfile.ZIP_DEFLATED)


def safe_stem(name):
    """清洗卷名，使其可安全用作目录名（去 Windows 非法字符与首尾空格/点）。"""
    stem = _ILLEGAL_CHARS.sub("_", name).strip(" .")
    return stem or "untitled"


def images_dir_name(volume):
    """勾选「保留中间图片」时，输出目录下的子目录名：<卷名>_images。"""
    return safe_stem(volume) + "_images"


def part_path(cbz_path):
    """打包临时文件名：带 pid，避免同名任务并发时共用同一个 .part。

    共用会撞 Windows 文件锁（WinError 32 / 5），两个任务一起失败。
    命名仍以 .part 结尾且含 `.cbz.`，启动清理能认出来。
    """
    return f"{cbz_path}.{os.getpid()}.part"


def is_part_file(name):
    """是否是本工具留下的 CBZ 打包临时文件（<卷名>.cbz.<pid>.part）。"""
    low = name.lower()
    return low.endswith(".part") and ".cbz." in low


def replace_with_retry(src, dst, attempts=6, base_delay=0.08):
    """os.replace，遇 PermissionError 退避重试。

    Windows 上换名会被短暂占用打断：杀软扫描、搜索索引器打开刚写好的文件，
    或两个任务同时换同一个目标名（WinError 5 / 32）。这类占用都是毫秒级，
    退避几轮即可，不该让整次转换失败。
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(base_delay * (i + 1))


def _add_image(z, fname, data, ext, sink=None):
    """写一条图到 zip：已压缩格式原样存入，未压缩位图走 DEFLATE。"""
    z.writestr(fname, data, compress_type=zip_compress_type(ext))
    if sink is not None:
        with open(os.path.join(sink, fname), "wb") as f:
            f.write(data)


def build_cbz(src_path, cbz_path, image_sink=None, on_page=None):
    """把源文件（PDF / MOBI 家族）内所有唯一图片打包成 cbz_path，返回写入的张数。

    image_sink 不为 None 时，每张图同时落盘到该目录（调用方负责清理）。
    on_page 每页处理完回调一次，供上层上报进度。
    先写临时文件，全部成功后再 os.replace 原子换名：
    中途崩溃或中断只留 .part，不会产出半截还能打开的 CBZ。
    """
    tmp_cbz = part_path(cbz_path)
    saved = 0
    try:
        with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as z:
            for ext, data in extractor.iter_unique_images(src_path, on_page=on_page):
                saved += 1
                # 直写磁盘上的 .part：字节落盘后即可释放，峰值内存 = 单张图。
                _add_image(z, f"img_{saved:03d}.{ext}", data, ext, image_sink)
        if not saved:
            raise NoImagesError(os.path.basename(src_path))
        replace_with_retry(tmp_cbz, cbz_path)   # 原子换名；Windows 短暂占用会重试
        return saved
    finally:
        if os.path.exists(tmp_cbz):
            try:
                os.remove(tmp_cbz)
            except OSError:
                pass


def write_part(src_path, part_file, start, end, image_sink=None):
    """写一个页段 / 图片段的临时 zip（分段并行用），返回 (图片数, 字节数)。

    段内自己做 xref + 内容去重；跨段的重复由 merge_parts 再兜一次。
    """
    saved = 0
    total = 0
    with zipfile.ZipFile(part_file, "w", zipfile.ZIP_STORED) as z:
        for ext, data in extractor.iter_unique_images(src_path, start, end):
            saved += 1
            total += len(data)
            # 段内文件名无所谓，合并时统一重排成 img_%03d.ext
            _add_image(z, f"p{saved:05d}.{ext}", data, ext, image_sink)
    return saved, total


def merge_parts(parts, cbz_path, image_sink=None):
    """按给定顺序合并页段 zip，返回唯一图片数。

    跨段按内容 MD5 去重，输出命名统一成 img_001.ext…（与单进程路径一致）。
    同样走「临时文件 + 原子换名 + 占用重试」，中途失败不留半截 CBZ。
    """
    tmp_cbz = part_path(cbz_path)
    saved = 0
    seen = set()
    try:
        with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as out:
            for p in parts:
                with zipfile.ZipFile(p) as z:
                    for info in z.infolist():
                        data = z.read(info.filename)
                        digest = hashlib.md5(data).hexdigest()
                        if digest in seen:
                            continue
                        seen.add(digest)
                        saved += 1
                        ext = os.path.splitext(info.filename)[1].lstrip(".") or "png"
                        _add_image(out, f"img_{saved:03d}.{ext}", data, ext, image_sink)
        if not saved:
            raise NoImagesError(os.path.basename(cbz_path))
        replace_with_retry(tmp_cbz, cbz_path)
        return saved
    finally:
        if os.path.exists(tmp_cbz):
            try:
                os.remove(tmp_cbz)
            except OSError:
                pass
