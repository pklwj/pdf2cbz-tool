# -*- coding: utf-8 -*-
"""PDF 图片提取。

doc.extract_image() 直接返回 PDF 内嵌图片的原始编码字节 —— 不重编码、不重采样、
不改色彩空间，所以产物与源 PDF 里的图完全一致。

去重做两层：
  1) xref 级：同一图片对象被多页复用时 xref 相同，只取一次；
  2) 内容级：不同 xref 但字节完全相同的图（有些 PDF 会重复嵌入），MD5 兜住。
"""
import hashlib
from typing import Iterator, Tuple

import pymupdf


def iter_unique_images(pdf_path: str, start: int = 0, end: int = None) -> Iterator[Tuple[str, bytes]]:
    """按页面顺序流式产出 (扩展名, 原始字节)，同一张图只产出一次。

    只处理 [start, end) 页（end 省略即到末尾）。分段并行时每个进程给一段，
    各自 open 一次 PDF —— MuPDF 不支持跨线程共享文档。
    产出一张、调用方消费一张，因此峰值内存约等于单张图，与页数无关。
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
                if digest in seen_hash:
                    continue
                seen_hash.add(digest)
                yield info["ext"], data
