# -*- coding: utf-8 -*-
"""源文件体检：回答「能不能转、有多少页/图、快还是慢、能不能切段」。

不产出图片，也不碰转换流程。任务层（batch）与界面层（main）只问它，
不必认识 PDF / MOBI / EPUB 各自的细节 —— 这是接到第三种格式之后才值得的一层：
格式分支从两处收敛到一处，以后加格式只改这里和 extractor。

PDF 的快慢由内嵌图片的**编码方式**决定，与体积无关：
DCTDecode（JPEG）字节直拷约 8 ms/张，FlateDecode 要解压再编 PNG 约 560 ms/张，差 71 倍。
"""
import os

import epub
import mobi
import pdf_images

# 可转换的源文件。新增格式 = 这里加后缀 + 写一个实现模块 + extractor 加一条分派。
SOURCE_EXTS = (".pdf",) + mobi.MOBI_EXTS + epub.EPUB_EXTS

# 编码 → (处理方式, 快慢)。判断依据是 PyMuPDF 能不能直接给出原始字节：
# DCT/JPX/JBIG2/CCITT 都是成品压缩流，取出来就是合法图片；Flate 与裸位图要重新编码。
_FILTER_HINT = {
    "DCTDecode": ("JPEG 字节直拷", "快"),
    "JPXDecode": ("JPEG2000 字节直拷", "快"),
    "JBIG2Decode": ("JBIG2 字节直拷", "快"),
    "CCITTFaxDecode": ("CCITT 字节直拷", "快"),
    "FlateDecode": ("解压后重编码 PNG", "慢"),
    "未压缩": ("裸位图，需编码 PNG", "慢"),
}


def summary(src_path: str) -> str:
    """一行摘要，供转换前打印。只读索引与记录头，不解码任何一张图。"""
    name = os.path.basename(src_path)
    if src_path.lower().endswith(".pdf"):
        return _pdf_summary(src_path, name)
    if mobi.is_mobi(src_path):
        return _mobi_summary(src_path, name)
    if epub.is_epub(src_path):
        return _epub_summary(src_path, name)
    return f"  {name}：格式不支持"


def _pdf_summary(src_path, name):
    try:
        pages, total, dist = pdf_images.probe_pdf(src_path)
    except Exception as e:
        return f"  {name}：读取失败（{e}），转换大概率也会失败"
    head = f"  {name}：{pages} 页，{total} 张图"
    if not total:
        return head + " → 未检测到图片，转换会失败（只支持图片型 PDF）"
    top = dist.most_common(3)
    shown = " / ".join(f"{k} {v}" for k, v in top)
    if len(dist) > 3:
        shown += f" / 另 {len(dist) - 3} 种"
    action, speed = _FILTER_HINT.get(top[0][0], ("未知编码", "未知"))
    return f"{head} · {shown} → 多数 {top[0][0]}，{action}，{speed}"


def _mobi_summary(src_path, name):
    try:
        info = mobi.probe_mobi(src_path)
    except Exception as e:                     # 缺 pymobi / 文件损坏 / 记录表异常
        return f"  {name}：读取失败（{e}），转换大概率也会失败"
    size_mb = info.size / 1024 / 1024
    head = f"  {name}：{mobi.describe(info)}，{info.images} 张图，{size_mb:.1f} MB"
    if info.encrypted:
        return head + " → 带 DRM 加密，只能跳过"
    if not info.images:
        return head + " → 未检测到图片"
    shown = " / ".join(f"{k} {v}" for k, v in info.kinds.most_common(3))
    if len(info.kinds) > 3:
        shown += f" / 另 {len(info.kinds) - 3} 种"
    return f"{head} · {shown} → 字节直拷，快"


def _epub_summary(src_path, name):
    try:
        info = epub.probe_epub(src_path)
    except epub.EpubDrmError as e:             # 加密书单独说清楚，别混进「读取失败」
        return f"  {name}：{e} → 只能跳过"
    except Exception as e:
        return f"  {name}：读取失败（{e}），转换大概率也会失败"
    size_mb = info.size / 1024 / 1024
    head = f"  {name}：EPUB {info.version}，{info.images} 张图，{size_mb:.1f} MB"
    if not info.images:
        return head + " → 未检测到图片（多半是纯文字书）"
    shown = " / ".join(f"{k} {v}" for k, v in info.kinds.most_common(3))
    if len(info.kinds) > 3:
        shown += f" / 另 {len(info.kinds) - 3} 种"
    return f"{head} · {shown} → 字节直拷，快"


def image_count(src_path: str) -> int:
    """页数 / 图片数。读不出来返回 0，交给调用方按「不分段」处理。

    MOBI / EPUB 的「页」就是一张图，所以直接数资源，不必把图读出来。
    """
    try:
        if src_path.lower().endswith(".pdf"):
            return pdf_images.probe_pdf(src_path)[0]
        if mobi.is_mobi(src_path):
            return mobi.probe_mobi(src_path).images
        if epub.is_epub(src_path):
            return epub.probe_epub(src_path).images
    except Exception:
        return 0
    return 0


def can_split(src_path: str) -> bool:
    """只有 PDF 支持按页切段并行。

    MOBI / EPUB 的图片是成品字节直拷，切成多进程只会多付一次合并开销。
    """
    return src_path.lower().endswith(".pdf")
