# -*- coding: utf-8 -*-
"""MOBI / AZW / AZW3 内嵌图片提取：只搬字节，不解码、不重编码。

本模块是纯模块：不读 argv、不打印、导入无副作用。它只做三件 pymobi 没做好的事：

  1) **找出所有图片记录。** pymobi 只在 `unpackMobi()` 里导出「正文引用到的图 + 封面」，
     漫画书里没被 HTML 引用的页会整批漏掉；它的 `imageExt()` 还会在 3 字节的短记录上
     抛 struct.error。这里改成逐条记录嗅探图片魔数，顺序天然等于记录顺序＝翻页顺序。
  2) **裁掉尾部 NUL 填充。** 实测 200 张 JPEG 里 148 张的 EOI 之后还挂着 0~2 个 0x00
     （kindlegen 补齐用），不裁的话部分阅读器会报错，CBZ 也白白变大。
  3) **丢掉缩略图。** EXTH 202 指向的是封面缩略图（实测 153×240），不是正文页。

字段偏移的来源：MOBI 头从未官方公开，pymobi 的字段表与 MobileRead Wiki
<https://wiki.mobileread.com/wiki/MOBI> 一致，并用三卷真实漫画 MOBI 逐项核对过
（268 / 259 / 260 条记录，各 195~201 张图，探测结果与逐字节扫描完全一致）。
"""
import hashlib
import os
import struct
from collections import Counter, namedtuple

# 可识别的图片魔数 → 扩展名。按记录内容判类型，不信扩展名、不信头部声明的类型。
# BMP 只有 2 字节魔数，误判概率高，另用头部声明长度复核（见 _kind）。
_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

# 支持的扩展名。.prc 只认带 MOBI 头的，纯 PalmDOC 会明确报错。
MOBI_EXTS = (".mobi", ".azw", ".azw3", ".prc")

# EXTH 记录类型（EXTH 是 MOBI 头后面的键值表，键号由社区逆向得出）
_EXTH_KF8_BOUNDARY = 121    # 合体书里 KF8 头所在记录号；有它就说明是 MOBI6+KF8 双头
_EXTH_THUMB = 202           # 缩略图序号（相对首条图片记录）

# 探测结果。size 用字节数，展示时的单位换算交给调用方。
MobiInfo = namedtuple(
    "MobiInfo", "images kinds version joint encrypted size")


class MobiError(Exception):
    """不是可解析的 MOBI/AZW 文件，或缺少 pymobi。"""


class DrmError(MobiError):
    """带 DRM 的书。本工具只搬字节，不解密。"""


def is_mobi(path):
    """按扩展名判断是不是 MOBI 家族（大小写不敏感）。"""
    return path.lower().endswith(MOBI_EXTS)


def _patch_pymobi_author_bug():
    """修补 pymobi 的 py3 移植缺陷：书里没有作者字段（EXTH 100）时，它把字符串
    `'unknown'` 交给 `toStr()` 去 `.decode()`，直接 AttributeError。

    真实书一般都有作者，但用脚本/工具生成的 MOBI 常常没有。补丁只在真的触发时打，
    而且只放宽「已经是 str 就原样返回」这一条，不改变其它行为。
    """
    import pymobi.mobi as pm

    original = pm.toStr
    if getattr(original, "_tolerant", False):
        return

    def toStr(src, coding="utf-8"):
        return src if isinstance(src, str) else original(src, coding)

    toStr._tolerant = True
    pm.toStr = toStr


def _open(path):
    """打开容器，返回 pymobi 的 BookMobi。

    pymobi 是 GPLv3 第三方库，放在函数内按需导入：没装时只有 MOBI 路径报错，
    本工具的 PDF 功能不受影响（extractor 只在走 MOBI 分支时才碰这里）。
    """
    try:
        from pymobi.mobi import BookMobi
    except ImportError as e:                       # 没装就给出可执行的补救
        raise MobiError("缺少 pymobi（MOBI 容器解析）——pip install pymobi") from e
    try:
        book = BookMobi(path)
    except AttributeError as e:
        if "decode" not in str(e):                 # 不是那个已知缺陷，照常报错
            raise MobiError(f"pymobi 打不开：{e}") from e
        _patch_pymobi_author_bug()                 # 缺作者字段：补完重试一次
        try:
            book = BookMobi(path)
        except Exception as e2:
            raise MobiError(f"pymobi 打不开：{e2}") from e2
    except Exception as e:                         # pymobi 内部异常五花八门，统一翻译
        raise MobiError(f"pymobi 打不开：{e}") from e
    if not book.mobi:
        book.f.close()
        raise MobiError("记录 0 无 MOBI 头（纯 PalmDOC / 其它格式，不支持）")
    return book


def _exth_int(book, kind):
    """取 EXTH 里的 4 字节整数；没有该键就返回 None。"""
    raw = book.mobi_exth.get(kind)
    if not raw or len(raw) < 4:
        return None
    return struct.unpack(">L", raw[:4])[0]


def _kind(head, length):
    """按魔数判图，返回扩展名或 None。

    **探测与提取必须共用这一份判定**：曾经 probe 只看 BM 两字节、extract 还校验声明长度，
    结果同一本书记录被算成 3 张图、实际只产出 1 张，日志骗人。

    BMP 的魔数只有 2 字节，随便一条文本记录都可能以 BM 开头，所以还要核对头部声明的
    文件长度（偏移 2~6 的小端 4 字节）是否落在本记录范围内。
    """
    for magic, ext in _MAGIC:
        if not head.startswith(magic):
            continue
        if ext != "bmp":
            return ext
        if len(head) < 6:
            return None
        declared = struct.unpack("<I", head[2:6])[0]
        return ext if 26 <= declared <= length else None
    return None


def _sniff(seg):
    """整条记录 → (扩展名, 干净字节)；不是图片返回 None。

    只裁记录尾部的 NUL 填充（BMP 按声明长度截断），图像字节一个不动 ——
    所以产物与书里的图逐字节相同。
    """
    ext = _kind(seg[:16], len(seg))
    if ext is None:
        return None
    if ext == "bmp":
        return ext, seg[:struct.unpack("<I", seg[2:6])[0]]
    return ext, seg.rstrip(b"\x00")


def _peek(book, rec, n=16):
    """只读某条记录的头 n 字节 —— 探测阶段不把整张图读进内存。

    16 字节够覆盖全部魔数（最长是 PNG 的 8 字节）加 BMP 的声明长度（第 6 字节止）。
    """
    book.f.seek(book.records[rec][0])
    return book.f.read(n)


def _record_length(book, rec):
    """某条记录占多少字节：靠记录表相邻偏移相减，不用读内容。

    最后一条没有「下一条」可减，用文件总长兜底。
    """
    start = book.records[rec][0]
    if rec + 1 < len(book.records):
        return book.records[rec + 1][0] - start
    return os.path.getsize(book.filename) - start


def _image_records(book):
    """扫一遍记录表，返回 (图片记录号列表, 编码分布, 缩略图记录号)。

    只读每条记录头 16 字节，不把整图读进内存。返回的记录号保持书内顺序，
    这个顺序就是翻页顺序，不做任何重排。
    """
    found = {}                                # 记录号 → 扩展名
    for i in range(len(book.records)):
        ext = _kind(_peek(book, i), _record_length(book, i))
        if ext is not None:
            found[i] = ext

    thumb = None
    first_image = book.mobi.get("firstImageIndex", 0)
    # EXTH 202 存的是「相对首条图片记录」的序号，换算成绝对记录号。
    # firstImageIndex 为 0 或 0xFFFFFFFF 表示这个字段不可用，直接跳过换算。
    if first_image not in (0, 0xFFFFFFFF):
        off = _exth_int(book, _EXTH_THUMB)
        if off is not None:
            thumb = first_image + off
    # 缩略图不是正文页，丢掉。但只在还剩 3 张以上时丢 ——
    # 万一某本书只有「封面 + 缩略图」，宁可多留一张，也别把书清空。
    if thumb in found and len(found) >= 3:
        del found[thumb]
        thumb = None
    return list(found), Counter(found.values()), thumb


def _info(book, hits, kinds):
    """把已扫出的结果组装成 MobiInfo，顺便补上格式与 DRM 状态。"""
    return MobiInfo(
        images=len(hits),
        kinds=kinds,
        version=book.mobi.get("fileVersion", 0),
        joint=_EXTH_KF8_BOUNDARY in book.mobi_exth,
        encrypted=book.palmdoc.get("encryptionType", 0) != 0,
        size=os.path.getsize(book.filename),
    )


def describe(info):
    """格式标签：合体书 / KF8 / MOBI6。只用于日志展示，不参与任何判断。"""
    if info.joint:
        return "MOBI6+KF8 合体"
    if info.version >= 8:
        return "KF8 (AZW3)"
    return f"MOBI{info.version}"


def probe_mobi(path):
    """只读记录头，返回 MobiInfo —— 不解码任何一张图，也不把整图读进内存。

    转换前打一行摘要用，几百条记录的文件实测几十毫秒，可以同步调用。
    """
    book = _open(path)
    try:
        hits, kinds, _thumb = _image_records(book)
        return _info(book, hits, kinds)
    finally:
        book.f.close()                     # pymobi 不提供 close，句柄要自己收


def iter_mobi_images(path, start=0, end=None, on_page=None):
    """按记录顺序流式产出 (扩展名, 原始字节)，同一张图只产出一次。

    只处理 [start, end) 张（end 省略即到末尾），供分段并行使用。
    每次只读一条记录，峰值内存 ≈ 单张图，与书的大小无关。
    on_page(序号, 总数) 每张消费完回调一次，用于上报进度。
    """
    book = _open(path)
    try:
        # 加密书的记录是密文，按魔数一定扫不出图。直接拒绝，别给「0 张图」的假结论。
        if book.palmdoc.get("encryptionType", 0):
            raise DrmError(f"{os.path.basename(path)} 有 DRM 加密"
                           f"（类型 {book.palmdoc['encryptionType']}），"
                           "本工具只搬原始字节，不解密")
        hits, _kinds, _thumb = _image_records(book)
        sel = hits[start:end]
        total = len(sel)
        seen = set()
        for n, rec in enumerate(sel):
            got = _sniff(book.loadRecord(rec))
            if got is not None:
                digest = hashlib.md5(got[1]).hexdigest()
                if digest not in seen:            # 有些书把同一张图存两遍
                    seen.add(digest)
                    yield got
            if on_page is not None:
                on_page(n, total)
    finally:
        book.f.close()
