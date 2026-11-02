# -*- coding: utf-8 -*-
"""图片提取总入口：只做分派，不含任何提取实现。

三个实现平级，各自可独立测试：
    pdf_images.py   PDF —— PyMuPDF 按 xref 直取原始字节
    mobi.py         MOBI / AZW / AZW3 —— pymobi 解容器 + 逐记录嗅探
    epub.py         EPUB —— 标准库解 ZIP + 按 spine 阅读顺序取图

两者契约一致：流式产出 (扩展名, 原始字节)，同一张图只产出一次，峰值内存约一张图。
调用方向保持单向：archive → extractor → {pdf_images, mobi, epub}，没有反向依赖。
"""
from typing import Iterator, Tuple

import epub
import mobi
import pdf_images


def iter_unique_images(path: str, start: int = 0, end: int = None,
                       on_page=None) -> Iterator[Tuple[str, bytes]]:
    """按扩展名分派到具体实现。

    start/end 对 PDF 是页号区间，对 MOBI 是图片序号区间；end 省略即到末尾。
    上层（archive / 分段 worker）只认这个函数，不必关心源文件是哪种格式。
    """
    if mobi.is_mobi(path):
        return mobi.iter_mobi_images(path, start, end, on_page)
    if epub.is_epub(path):
        return epub.iter_epub_images(path, start, end, on_page)
    return pdf_images.iter_pdf_images(path, start, end, on_page)
