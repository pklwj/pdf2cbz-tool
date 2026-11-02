# -*- coding: utf-8 -*-
"""PDF 内嵌图片提取：xref 直取原始字节，不重编码、不重采样、不改色彩空间。

`doc.extract_image()` 返回的就是 PDF 里那张图的原始编码字节 —— 与源图完全一致，
所以产物不取决于本工具的编码策略，只取决于作者当初塞进去的是什么。

去重做两层：
  1) xref 级：同一图片对象被多页复用时 xref 相同，只取一次；
  2) 内容级：不同 xref 但字节完全相同的图（有些 PDF 会重复嵌入），MD5 兜住。

本模块是纯模块：不读 argv、不打印、导入无副作用。
"""
import hashlib
from collections import Counter
from typing import Iterator, Tuple

import pymupdf

# Filter 名里没有 `/Filter` 键的裸位图，统一记成这个名字
UNCOMPRESSED = "未压缩"


def probe_pdf(pdf_path: str):
    """只读 xref 扫图片编码，不解码任何一个像素。

    返回 (页数, 唯一图片数, Counter{编码名: 张数})。
    用来在转换前判断这次是「字节直拷，快」还是「解压重编码，慢」：
    DCTDecode（JPEG）直接拷贝字节约 8 ms/张，FlateDecode 要解码再编 PNG，约 560 ms/张。

    Filter 可能是数组（如 `/Filter [/FlateDecode]`），逐个 token 计入。
    实测 200 页 / 883 MB 的 PDF 只要 53 ms，可以放在转换前同步跑。
    """
    dist = Counter()
    seen = set()
    with pymupdf.open(pdf_path) as doc:
        pages = doc.page_count
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:           # 同一对象被多页引用，只统计一次
                    continue
                seen.add(xref)
                kind, val = doc.xref_get_key(xref, "Filter")
                if kind == "null":         # 裸位图没有 Filter 键，PyMuPDF 返回 ('null','null')
                    dist[UNCOMPRESSED] += 1
                else:
                    for tok in val.strip("[]").split():
                        dist[tok.lstrip("/")] += 1
    return pages, len(seen), dist


def iter_pdf_images(pdf_path: str, start: int = 0, end: int = None,
                    on_page=None) -> Iterator[Tuple[str, bytes]]:
    """按页面顺序流式产出 (扩展名, 原始字节)，同一张图只产出一次。

    只处理 [start, end) 页（end 省略即到末尾）。分段并行时每个进程给一段，
    各自 open 一次 PDF —— MuPDF 不支持跨线程共享文档。
    产出一张、调用方消费一张，因此峰值内存约等于单张图，与页数无关。
    on_page(pno, total) 在每页的图片消费完时回调，用于上报进度。
    """
    seen_xref = set()
    seen_hash = set()
    with pymupdf.open(pdf_path) as doc:
        stop = doc.page_count if end is None else min(end, doc.page_count)
        for pno in range(max(0, start), stop):
            page = doc[pno]
            # full=True 才带 xref；get_images() 只给序号，无法做对象级去重。
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen_xref:
                    continue
                seen_xref.add(xref)
                info = doc.extract_image(xref)
                data = info["image"]
                digest = hashlib.md5(data).hexdigest()
                if digest in seen_hash:    # 不同 xref 但字节相同，第二层去重
                    continue
                seen_hash.add(digest)
                yield info["ext"], data
            if on_page is not None:
                on_page(pno, stop)
