# -*- coding: utf-8 -*-
"""EPUB 内嵌图片提取：只用标准库（zipfile + ElementTree），不引入 EPUB 解析库。

EPUB 就是个 ZIP，结构三层：
    1. `META-INF/container.xml` 指明 OPF 在哪；
    2. OPF 的 `<manifest>` 列出全部资源（含媒体类型），`<spine>` 给出阅读顺序；
    3. 每页 XHTML 里的 `<img src>` / `<image xlink:href>` 指向真正的图片。

取图顺序 = **先按 spine 的页序取每页引用的图，再补上 manifest 里没被任何页引用的图**。
漫画书这样拿到的就是翻页顺序；封面、版权图这类「只在 manifest 里挂着」的也不会丢。

去重按**资源路径**，不按字节：同一文件被多页引用时 href 相同，天然只取一次；
不同路径但字节相同的情况极少，那时保两页更安全（按字节去重会少一页）。
这条与 MOBI 不同 —— MOBI 没有路径，只能按内容去重。

本模块是纯模块：不读 argv、不打印、导入无副作用。
"""
import os
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, namedtuple
from typing import Iterator, Tuple
from urllib.parse import unquote

EPUB_EXTS = (".epub",)

# 探测结果：图片数、格式分布、字节数、EPUB 版本
EpubInfo = namedtuple("EpubInfo", "images kinds size version")

# 按文件名后缀认图（probe 阶段只看名字，不用把图读出来）
_IMG_SUFFIX = re.compile(r"\.(jpe?g|png|gif|webp|bmp|svg)$", re.I)

# XHTML 不是严格 XML 时的兜底：直接抓 img/image 的 src 或 xlink:href
_SRC_FALLBACK = re.compile(
    r"""<(?:img|image)\b[^>]*?\b(?:src|xlink:href)\s*=\s*["']([^"']+)["']""", re.I)

# 只由这几个媒体类型认定「这是一页正文」；spine 里其它类型（图片、样式）跳过
_PAGE_TYPES = ("application/xhtml+xml", "text/html")


class EpubError(Exception):
    """不是可解析的 EPUB。"""


class EpubDrmError(EpubError):
    """resource 被加密。本工具只搬字节，不解密。"""


def is_epub(path: str) -> bool:
    """按扩展名判断（大小写不敏感）。"""
    return path.lower().endswith(EPUB_EXTS)


def _local(tag):
    """取标签/属性名的本地名：`{ns}img` → `img`。

    EPUB 里命名空间五花八门（OPF / XHTML / xlink），只认尾巴最省心。
    """
    return tag.rsplit("}", 1)[-1].lower()


def _open(path):
    """打开容器。EPUB 本质是 ZIP，打不开就不是 EPUB。"""
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise EpubError(f"不是有效的 EPUB（ZIP 打不开：{e}）") from e


def _find_opf(z):
    """定位 OPF。正常走 container.xml；缺失或损坏时退化为「找第一个 .opf」。"""
    try:
        data = z.read("META-INF/container.xml")
    except KeyError:
        data = b""
    names = z.namelist()
    lower = {n.lower(): n for n in names}
    if data:
        try:
            root = ET.fromstring(data)
            for el in root.iter():
                if _local(el.tag) != "rootfile":
                    continue
                full = el.get("full-path")
                if not full:
                    continue
                # full-path 写成反斜杠或大小写不符是真实存在的（不同打包工具），
                # 精确匹配失败时要兜底，否则会掉进下面的「猜一个 .opf」
                cand = full.replace("\\", "/").lstrip("/")
                if cand in names:
                    return cand
                if cand.lower() in lower:
                    return lower[cand.lower()]
        except ET.ParseError:
            pass                              # 坏 XML 不致命，走下面的兜底
    # 兜底：挑最浅的 .opf。同一本书里可能夹着模板/示例 OPF（实测 OEBPS/samples/template.opf），
    # 按 namelist 顺序取第一个会挑错，深度优先更靠谱。
    cands = [n for n in names if n.lower().endswith(".opf")]
    if cands:
        return min(cands, key=lambda n: (n.count("/"), len(n)))
    raise EpubError("找不到 OPF（不是标准 EPUB）")


def _resolve(base, href):
    """把 href 解析成 ZIP 内路径：去锚点与查询串、反转义、相对 base 所在目录归一化。

    **查询串必须剥掉**：`x.jpg?v=1` 是合法 URI，但 `?v=1` 不是文件名的一部分。
    只剥锚点（`#`）而漏掉查询串时，这一页的图会解析失败、落到「补漏」阶段的末尾，
    结果就是页序错乱（实测把 [1,2] 变成 [2,1]）。
    分隔符按 URI 语法在反转义**之前**切，否则文件名里编码过的 `%3F` 会被误切。
    """
    href = href.split("#", 1)[0].split("?", 1)[0].strip()
    href = unquote(href).replace("\\", "/")   # 少数书商用反斜杠
    if not href:
        return ""
    if href.startswith("/"):                  # 少数书用绝对路径
        return posixpath.normpath(href.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), href))


def _parse_opf(z, opf_path):
    """读 OPF，返回 (manifest, spine, 版本号)。

    manifest 是 id → {path, type, props}；spine 是 idref 的顺序列表（阅读顺序）。
    """
    try:
        root = ET.fromstring(z.read(opf_path))
    except KeyError as e:
        raise EpubError(f"OPF 不存在：{opf_path}") from e
    except ET.ParseError as e:
        raise EpubError(f"OPF 解析失败：{e}") from e

    manifest, spine = {}, []
    version = root.get("version", "2.0")
    for el in root.iter():
        name = _local(el.tag)
        if name == "item":
            item_id, href = el.get("id"), el.get("href")
            if item_id and href:
                manifest[item_id] = {
                    "path": _resolve(opf_path, href),
                    "type": (el.get("media-type") or "").lower(),
                    "props": (el.get("properties") or "").lower(),
                }
        elif name == "itemref":
            idref = el.get("idref")
            if idref and idref not in spine:   # 同一 idref 重复出现只算一次
                spine.append(idref)
    return manifest, spine, version


def _hrefs_from_doc(data):
    """从一页 XHTML 里按文档顺序取出图片引用（相对本页的 href）。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:                     # 不是严格 XML，退回正则
        return _SRC_FALLBACK.findall(data.decode("utf-8", "ignore"))
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("img", "image"):
            continue
        for key, val in el.attrib.items():
            if _local(key) in ("src", "href") and val:
                out.append(val)
                break
    return out


def _image_paths(z, opf_path, manifest, spine):
    """列出图片路径：spine 页序优先，之后补 manifest 里没被引用的图。顺序即输出顺序。"""
    names = set(z.namelist())
    lower = {n.lower(): n for n in names}      # ZIP 名大小写敏感，但 href 常写错大小写

    def real(path):
        if not path:
            return None
        if path in names:
            return path
        return lower.get(path.lower())

    ordered, seen = [], set()
    for idref in spine:
        item = manifest.get(idref)
        if not item or item["type"] not in _PAGE_TYPES:
            continue
        try:
            data = z.read(item["path"])
        except KeyError:                       # manifest 指向不存在的文件，跳过这页
            continue
        for href in _hrefs_from_doc(data):
            path = real(_resolve(item["path"], href))
            if path and path not in seen and _IMG_SUFFIX.search(path):
                seen.add(path)
                ordered.append(path)

    # 补漏：封面、版权页图片常常只在 manifest 里挂着，正文从不引用
    for item in manifest.values():
        path = real(item["path"])
        if not path or path in seen:
            continue
        if item["type"].startswith("image/") or _IMG_SUFFIX.search(path):
            seen.add(path)
            ordered.append(path)
    return ordered


def _check_drm(z, image_paths):
    """encryption.xml 点名加密了某张图 → 拒。

    字体混淆（Adobe / IDPF 两种）也会写进 encryption.xml，那是常态，不算 DRM，
    所以只对「被加密的路径里有图片」这种情况报错。
    """
    names = z.namelist()
    if "META-INF/encryption.xml" not in names:
        return
    try:
        root = ET.fromstring(z.read("META-INF/encryption.xml"))
    except ET.ParseError:
        return                                 # 解析不了就当没有，别误伤正常书
    def norm(path):
        """归一化：反转义、反斜杠转正、去前导斜杠、小写。

        ZIP 名大小写敏感，但 URI 与条目名大小写不一致的书真实存在；
        同一本书既然能容忍 href 大小写（见 _image_paths），DRM 判定也必须一视同仁，
        否则就会出现「图取得到、加密查不出」，把密文写进 CBZ。
        """
        return posixpath.normpath(unquote(path).replace("\\", "/").lstrip("/")).lower()

    targets = set()
    for el in root.iter():
        if _local(el.tag) != "cipherreference":
            continue
        for key, val in el.attrib.items():     # 属性名可能是 URI、uri、Uri
            if _local(key) == "uri" and val:
                targets.add(norm(val))
                break
    hit = [p for p in image_paths if norm(p) in targets]
    if hit:
        raise EpubDrmError(f"{len(hit)} 张图被加密（如 {hit[0]}），"
                           "本工具只搬原始字节，不解密")


def _ext_of(path: str) -> str:
    """图片扩展名，统一成 jpeg（与 PDF / MOBI 两条路径保持一致）。"""
    suffix = os.path.splitext(path)[1].lstrip(".").lower()
    return "jpeg" if suffix in ("jpg", "jpe") else (suffix or "bin")


def _collect(path: str):
    """打开一本书并算出图片路径表，probe 与提取共用，保证两者结论一致。"""
    z = _open(path)
    try:
        opf = _find_opf(z)
        manifest, spine, version = _parse_opf(z, opf)
        paths = _image_paths(z, opf, manifest, spine)
        _check_drm(z, paths)
        return z, paths, version
    except Exception:
        z.close()
        raise


def probe_epub(path: str) -> EpubInfo:
    """只读 OPF 与 XHTML，返回 EpubInfo —— 不读任何一张图。

    与提取共用 `_image_paths`，所以「探测报几张」和「实际出几张」不会分叉。
    """
    z, paths, version = _collect(path)
    try:
        kinds = Counter(_ext_of(p) for p in paths)
        return EpubInfo(len(paths), kinds, os.path.getsize(path), version)
    finally:
        z.close()


def iter_epub_images(path: str, start: int = 0, end: int = None,
                     on_page=None) -> Iterator[Tuple[str, bytes]]:
    """按阅读顺序流式产出 (扩展名, 原始字节)。

    只处理 [start, end) 张（end 省略即到末尾），供分段并行使用。
    每次只读一个 ZIP 条目，峰值内存 ≈ 单张图。
    on_page(序号, 总数) 每张消费完回调一次，用于上报进度。
    """
    z, paths, _version = _collect(path)
    try:
        sel = paths[start:end]
        total = len(sel)
        for n, p in enumerate(sel):
            yield _ext_of(p), z.read(p)         # 字节原样，不解码不重编码
            if on_page is not None:
                on_page(n, total)
    finally:
        z.close()
