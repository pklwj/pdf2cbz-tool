# -*- coding: utf-8 -*-
"""重构等价性验证：新模块 vs 上游单文件，产物逐条比对。"""
import os
import sys
import zipfile
import importlib.util
import tempfile
import shutil
import struct
import multiprocessing

PROJ = os.path.dirname(os.path.abspath(__file__))
ORIG = os.environ.get("PDF2CBZ_ORIG", os.path.join(PROJ, "upstream_v1.4.1.py"))
sys.path.insert(0, PROJ)


def load_upstream():
    """载入上游单文件版，用于等价性比对。找不到就返回 None（跳过该组用例）。"""
    if not ORIG or not os.path.exists(ORIG):
        return None
    import types
    if "tkinter" not in sys.modules:
        # 精简版 Python 可能没 tkinter，而上游文件顶层就 import 它：塞桩绕开
        try:
            import tkinter  # noqa: F401
        except ModuleNotFoundError:
            for _n in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"):
                sys.modules[_n] = types.ModuleType(_n)
            sys.modules["tkinter"].Tk = object
            sys.modules["tkinter"].Toplevel = object
            for _n in ("ttk", "filedialog", "messagebox"):
                setattr(sys.modules["tkinter"], _n, sys.modules["tkinter." + _n])
    spec = importlib.util.spec_from_file_location("orig_pdf2cbz", ORIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

import pymupdf
import archive
import batch
import extractor
import mobi
import epub
import pdf_images
import probe
import rename


def make_pix(color, fmt, w=80, h=80):
    px = pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(color) * (w * h), 0)
    return px.tobytes(fmt)


def make_pdf(path):
    """3 页：p1=jpegA+pngB, p2=jpegA(重复,测内容级去重), p3=pngC。"""
    a = make_pix((255, 0, 0), "jpg")
    b = make_pix((0, 128, 0), "png")
    c = make_pix((0, 0, 255), "png")
    doc = pymupdf.open()
    for streams in ([a, b], [a], [c]):
        page = doc.new_page(width=400, height=400)
        for i, s in enumerate(streams):
            page.insert_image(pymupdf.Rect(20 + i * 100, 20, 180 + i * 100, 180), stream=s)
    doc.save(path)
    doc.close()
    return a, b, c


def _img_ext(data):
    """按魔数给扩展名，造样本用。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:3] == b"GIF":
        return "gif"
    return "png"


def make_epub(path, pages, extra=(), drm=False):
    """手搓一本最小 EPUB：pages 里每张图 = 一页 XHTML，spine 顺序即页序。

    extra 是「只挂在 manifest、正文不引用」的图（封面/版权图常见），用来测补漏。
    drm=True 会把第一张图写进 encryption.xml，模拟加密资源。
    """
    items, spine, files = [], [], {}
    for i, img in enumerate(pages, 1):
        ext = _img_ext(img)
        img_path = f"OEBPS/images/i{i:03d}.{ext}"
        page_path = f"OEBPS/text/p{i:03d}.xhtml"
        files[img_path] = img
        files[page_path] = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            f'<img src="../images/i{i:03d}.{ext}" alt=""/></body></html>').encode("utf-8")
        mtype = "image/jpeg" if ext == "jpg" else f"image/{ext}"
        items.append(f'<item id="p{i}" href="text/p{i:03d}.xhtml" '
                     f'media-type="application/xhtml+xml"/>')
        items.append(f'<item id="i{i}" href="images/i{i:03d}.{ext}" media-type="{mtype}"/>')
        spine.append(f'<itemref idref="p{i}"/>')
    for j, img in enumerate(extra, 1):
        ext = _img_ext(img)
        files[f"OEBPS/images/x{j:02d}.{ext}"] = img
        mtype = "image/jpeg" if ext == "jpg" else f"image/{ext}"
        items.append(f'<item id="x{j}" href="images/x{j:02d}.{ext}" media-type="{mtype}"/>')

    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata/><manifest>' + "".join(items) + '</manifest><spine>'
           + "".join(spine) + '</spine></package>')
    container = ('<?xml version="1.0"?>'
                 '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                 'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for name, data in files.items():
            z.writestr(name, data)
        if drm:
            z.writestr("META-INF/encryption.xml",
                       '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<EncryptedData><CipherData><CipherReference '
                       'URI="OEBPS/images/i001.jpg"/></CipherData></EncryptedData></encryption>')


def make_epub_raw(path, entries):
    """按 {ZIP 内路径: 字节/文本} 原样写一本 EPUB，用来构造病态样本。"""
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)


def epub_pkg(items, spine):
    """拼一个最小 OPF 包文档。"""
    return ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata/><manifest>' + items + '</manifest><spine>' + spine
            + '</spine></package>')


def epub_container(full_path="OEBPS/content.opf"):
    """拼一个最小 container.xml。"""
    return ('<?xml version="1.0"?><container '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            f'<rootfiles><rootfile full-path="{full_path}"/></rootfiles></container>')


def zip_fingerprint(path):
    """(文件名, 大小, CRC) 列表 —— 忽略 zip 内嵌时间戳。"""
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.file_size, i.CRC) for i in z.infolist()]


def gif_1px():
    """1×1 GIF89a 的原始字节（35 字节），用来测非 JPEG 分支。"""
    return (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
            b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
            b"\x02\x02D\x01\x00;")


def make_mobi(path, images, drm=0, joint=False, thumb=None, pad_last=0,
              author=True):
    """手搓一本最小可用 MOBI，专为测分支：DRM / 无图 / 重复图 / 缩略图 / 尾部填充。

    结构：PalmDB 头 + 记录表 + 记录 0（PalmDOC 16 字节 + MOBI 头 0xE8 + EXTH + 书名）
    + 2 条占位正文记录 + 图片记录。字段偏移与 pymobi / MobileRead Wiki 一致；
    真实文件（三卷漫画书）另有端到端验证，不靠这个构造器自证。
    """
    n_rec = 3 + len(images)
    hlen = 0xE8
    r0 = bytearray(16 + hlen)
    struct.pack_into(">H", r0, 0, 1)            # compression = 不压缩
    struct.pack_into(">L", r0, 4, 100)          # textLength
    struct.pack_into(">H", r0, 8, 2)            # 正文记录数
    struct.pack_into(">L", r0, 10, 4096)        # 记录尺寸
    struct.pack_into(">H", r0, 12, drm)         # 加密类型
    r0[0x10:0x14] = b"MOBI"
    struct.pack_into(">L", r0, 0x14, hlen)
    struct.pack_into(">L", r0, 0x18, 2)          # mobiType = 书
    struct.pack_into(">L", r0, 0x1C, 65001)
    struct.pack_into(">L", r0, 0x20, 0x12345678)
    struct.pack_into(">L", r0, 0x24, 8 if joint else 6)   # fileVersion
    struct.pack_into(">L", r0, 0x50, 2)          # firstNonBookIndex
    struct.pack_into(">L", r0, 0x5C, 1033)       # locale
    struct.pack_into(">L", r0, 0x68, 6)          # minVersion
    struct.pack_into(">L", r0, 0x6C, 3)          # firstImageIndex = 第 3 条记录
    struct.pack_into(">L", r0, 0x80, 0x40)       # exthFlags：有 EXTH
    struct.pack_into(">L", r0, 0xA8, 0xFFFFFFFF)  # drmOffset
    struct.pack_into(">L", r0, 0xAC, 0xFFFFFFFF)  # drmCount
    struct.pack_into(">H", r0, 0xC0, 1)           # 首条正文记录
    struct.pack_into(">H", r0, 0xC2, n_rec - 1)   # 末条正文/资源记录

    items = [(201, struct.pack(">L", 0))]                        # 封面序号
    if author:                                                   # 作者字段（EXTH 100）
        items.insert(0, (100, b"selftest"))
    if thumb is not None:
        items.append((202, struct.pack(">L", thumb)))
    if joint:
        items.append((121, struct.pack(">L", n_rec)))            # KF8 边界
    body = b"".join(struct.pack(">LL", t, len(d) + 8) + d for t, d in items)
    exth = b"EXTH" + struct.pack(">LL", len(body) + 12, len(items)) + body

    title = "Selftest".encode("utf-8")
    struct.pack_into(">L", r0, 0x54, 16 + hlen + len(exth))       # fullNameOffset
    struct.pack_into(">L", r0, 0x58, len(title))                  # fullNameLength
    record0 = bytes(r0) + exth + title

    records = [record0, b"<html>one</html>", b"<html>two</html>"]
    for i, img in enumerate(images):
        records.append(img + (b"\x00" * pad_last if i == len(images) - 1 else b""))

    pdb = bytearray(78)
    pdb[0:len(b"Selftest")] = b"Selftest"
    pdb[60:64], pdb[64:68] = b"BOOK", b"MOBI"
    struct.pack_into(">L", pdb, 68, 1)            # uniqueIDseed
    struct.pack_into(">H", pdb, 76, len(records))

    table = bytearray(len(records) * 8)
    offset = 78 + len(table)
    for i, rec in enumerate(records):
        struct.pack_into(">L", table, i * 8, offset)
        struct.pack_into(">L", table, i * 8 + 4, i & 0xFFFFFF)    # 属性 0 + 唯一 ID
        offset += len(rec)
    with open(path, "wb") as f:
        f.write(bytes(pdb) + bytes(table) + b"".join(records))


def run():
    tmp = tempfile.mkdtemp(prefix="p2cbz_test_")
    fails = []
    tally = {"pass": 0, "fail": 0, "skip": 0}

    def check(name, cond, detail=""):
        print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + str(detail)))
        tally["pass" if cond else "fail"] += 1
        if not cond:
            fails.append(name)

    def skip(name, why=""):
        """环境缺件导致的跳过：不算通过，也不许悄悄当 PASS 掩盖缺依赖。"""
        print("SKIP  " + name + ("  <- " + why if why else ""))
        tally["skip"] += 1

    pdf = os.path.join(tmp, "Vol 01.pdf")
    a, b, c = make_pdf(pdf)

    # --- 1. 提取：jpegA 在 p1/p2 出现两次，png 两张，共 3 张唯一 ---
    got = list(extractor.iter_unique_images(pdf))
    check("extractor 去重后 3 张", len(got) == 3, len(got))
    check("extractor 保原格式(jpg/png)",
          [e for e, _ in got] == ["jpeg", "png", "png"], [e for e, _ in got])
    check("extractor 字节与源图一致", got[0][1] == a, "jpeg 不一致")

    # --- 2. 打包：压缩方式按格式选 ---
    d_new = os.path.join(tmp, "new")
    os.makedirs(d_new)
    job = batch.ConvertJob(src_path=pdf, out_dir=d_new, keep_images=False, overwrite=False)
    p, ok, msg = batch.convert_file(job)
    check("batch 单文件转换成功", ok, msg)
    cbz_new = os.path.join(d_new, "Vol 01.cbz")
    check("CBZ 落盘", os.path.exists(cbz_new))
    check("无 .part 残留", not any(n.endswith(".part") for n in os.listdir(d_new)),
          os.listdir(d_new))
    with zipfile.ZipFile(cbz_new) as z:
        types = {i.filename: i.compress_type for i in z.infolist()}
    check("jpeg 用 STORED", types.get("img_001.jpeg") == zipfile.ZIP_STORED, types)
    check("png 用 STORED", types.get("img_002.png") == zipfile.ZIP_STORED, types)
    check("bmp/tiff 走 DEFLATE", archive.zip_compress_type("bmp") == zipfile.ZIP_DEFLATED
          and archive.zip_compress_type("TIFF") == zipfile.ZIP_DEFLATED)
    check("未知格式走 DEFLATE", archive.zip_compress_type("weird") == zipfile.ZIP_DEFLATED)

    # --- 2a. 带进度队列的转换（GUI 走这条；私有回调没被覆盖过就容易误删）---
    class _SinkQueue:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    d_prog = os.path.join(tmp, "prog")
    os.makedirs(d_prog)
    q_sink = _SinkQueue()
    _pp, ok_p, _mp = batch.convert_file(
        batch.ConvertJob(pdf, d_prog, False, False, q_sink))
    check("带进度队列的转换不炸（覆盖 _throttled_page_cb）",
          ok_p and q_sink.items and q_sink.items[-1][1] == 3, q_sink.items[-3:])

    # --- 2b. 分段并行：切两段再合并，必须与单进程产物一模一样 ---
    d_split = os.path.join(tmp, "split")
    os.makedirs(d_split)
    p1 = os.path.join(d_split, "Vol 01.cbz.p00000.part")
    p2 = os.path.join(d_split, "Vol 01.cbz.p00001.part")
    n1, _ = archive.write_part(pdf, p1, 0, 2)
    n2, _ = archive.write_part(pdf, p2, 2, 3)
    cbz_split = os.path.join(d_split, "Vol 01.cbz")
    n_merged = archive.merge_parts([p1, p2], cbz_split)
    check("分段写盘张数 2+1", (n1, n2) == (2, 1), (n1, n2))
    check("合并后 3 张", n_merged == 3, n_merged)
    check("分段产物与单进程等价(名/大小/CRC)",
          zip_fingerprint(cbz_split) == zip_fingerprint(cbz_new))

    q1 = os.path.join(d_split, "Vol 01.cbz.p00000.part")
    q2 = os.path.join(d_split, "Vol 01.cbz.p00001.part")
    archive.write_part(pdf, q1, 0, 2)
    archive.write_part(pdf, q2, 2, 3)
    _p, ok_m, msg_m = batch.merge_ranges(pdf, d_split, "Vol 01", [q1, q2], False)
    check("merge_ranges 消息含张数", ok_m and "3 张唯一图片" in msg_m, msg_m)
    check("页段文件用完即删", not os.path.exists(q1) and not os.path.exists(q2))

    check("count_pages", probe.image_count(pdf) == 3, probe.image_count(pdf))
    rjob = batch.RangeJob(pdf, os.path.join(d_split, "x.cbz.p00000.part"), 0, 1)
    tag, _part, ok_r, _m, n_r, label_r, el_r = batch.run_job(rjob)
    check("run_job 分派到页段", tag == "range" and ok_r and n_r == 2, (tag, ok_r, n_r))
    check("页段回报页区间与耗时", label_r == "第 1–1 页" and el_r >= 0, (label_r, el_r))
    os.remove(_part)

    # 页进度回调：每页一次，带总页数
    seen = []
    list(extractor.iter_unique_images(pdf, on_page=lambda p, t: seen.append((p, t))))
    check("on_page 每页回调一次", seen == [(0, 3), (1, 3), (2, 3)], seen)
    check("ConvertJob 四参调用仍可用",
          batch.ConvertJob("a", "b", False, False).progress is None)

    # 图片编码探测：3 张唯一图 = 1 JPEG(DCTDecode) + 2 PNG（PyMuPDF 存裸位图，无 Filter）
    summary = probe.summary(pdf)
    check("probe_summary 认出 3 张图", "3 张图" in summary, summary)
    check("probe_summary 分类正确",
          "DCTDecode 1" in summary and "未压缩 2" in summary, summary)
    pages_p, total_p, dist_p = pdf_images.probe_pdf(pdf)
    check("probe_pdf 页数/张数", (pages_p, total_p) == (3, 3), (pages_p, total_p))
    check("probe_pdf 分布", dict(dist_p) == {"DCTDecode": 1, "未压缩": 2}, dict(dist_p))

    # --- 3. 与上游单文件等价（PDF2CBZ_ORIG 指向上游 pdf2cbz_gui.py） ---
    orig = load_upstream()
    if orig is None:
        print("SKIP  与上游单文件等价性比对（设 PDF2CBZ_ORIG=<上游 pdf2cbz_gui.py> 后可跑）")
    else:
        d_old = os.path.join(tmp, "old")
        os.makedirs(d_old)
        p2, ok2, msg2 = orig.convert_pdf_to_cbz(
            orig.ConvertJob(pdf_path=pdf, out_dir=d_old, keep_images=False, overwrite=False))
        check("上游转换成功", ok2, msg2)
        check("产物逐条等价(名/大小/CRC)",
              zip_fingerprint(cbz_new) == zip_fingerprint(os.path.join(d_old, "Vol 01.cbz")))
        check("返回消息等价", msg == msg2, f"{msg!r} != {msg2!r}")

    # --- 4. keep_images 分支 ---
    d_keep = os.path.join(tmp, "keep")
    os.makedirs(d_keep)
    job_k = batch.ConvertJob(src_path=pdf, out_dir=d_keep, keep_images=True, overwrite=False)
    pk, okk, msgk = batch.convert_file(job_k)
    imgdir = os.path.join(d_keep, "Vol 01_images")
    check("keep_images 目录生成", okk and os.path.isdir(imgdir), msgk)
    check("keep_images 图片数 3",
          len(os.listdir(imgdir)) == 3 if os.path.isdir(imgdir) else False,
          os.listdir(imgdir) if os.path.isdir(imgdir) else "无目录")
    check("keep_images 无 .tmp_imgs 残留",
          not any(n.startswith(".tmp_imgs") for n in os.listdir(d_keep)),
          os.listdir(d_keep))

    # --- 5. 覆盖保护 / 原子性 ---
    p3, ok3, msg3 = batch.convert_file(job)
    check("已存在时默认跳过", (not ok3) and "跳过" in msg3, msg3)
    job_ov = job._replace(overwrite=True)
    p4, ok4, msg4 = batch.convert_file(job_ov)
    check("勾选覆盖后重做", ok4, msg4)

    # --- 6. 纯矢量 PDF 报错路径 ---
    vec = os.path.join(tmp, "vector.pdf")
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "no images here")
    doc.save(vec)
    doc.close()
    dv = os.path.join(tmp, "vec")
    os.makedirs(dv)
    pv, okv, msgv = batch.convert_file(
        batch.ConvertJob(vec, dv, False, False))
    check("无图 PDF 报错且不留 .part",
          (not okv) and "未提取到任何图片" in msgv and os.listdir(dv) == [],
          (msgv, os.listdir(dv)))
    check("空图 PDF 的摘要提前给提示",
          "未检测到图片" in probe.summary(vec), probe.summary(vec))

    # --- 7. 重命名逻辑 ---
    cases = {"I's v01.cbz": 1, "Vol.12.cbz": 12, "Vol 03 x.cbz": 3, "第7卷.cbz": 7,
             "#09.cbz": 9, "12巻.cbz": 12, "plain name.cbz": None, "2024 yearly.pdf": None}
    bad = {k: (rename.extract_volume(k), v) for k, v in cases.items()
           if rename.extract_volume(k) != v}
    check("extract_volume 8 组用例", not bad, bad)

    nat = ["v1.cbz", "v2.cbz", "v10.cbz"]
    check("自然排序 1<2<10", sorted(nat, key=rename.nat_key) == nat,
          sorted(nat, key=rename.nat_key))

    dr = os.path.join(tmp, "ren")
    os.makedirs(dr)
    for n in ["A.cbz", "B.cbz", "C.cbz"]:
        open(os.path.join(dr, n), "wb").close()
    plan = rename.plan_renames(dr, "Manga", "seq")
    check("计划格式 'Manga v01.cbz'", plan[0][1] == "Manga v01.cbz", plan)
    check("顺序模式无冲突", rename.find_conflicts(plan, dr) == [])
    dup = [("A.cbz", "X.cbz"), ("B.cbz", "X.cbz")]
    check("重名冲突被捕获", len(rename.find_conflicts(dup, dr)) == 1,
          rename.find_conflicts(dup, dr))

    # --- 7b. 重命名执行（原先埋在 GUI 里点不到，抽到 execute_renames 后可测） ---
    de = os.path.join(tmp, "exec")
    os.makedirs(de)
    for n in ["a.cbz", "b.cbz", "c.cbz"]:
        open(os.path.join(de, n), "wb").close()
    ok_n, errs_n = rename.execute_renames(de, [("a.cbz", "A1.cbz"), ("b.cbz", "B1.cbz")])
    check("execute_renames 批量成功", (ok_n, errs_n) == (2, []), (ok_n, errs_n))
    check("execute_renames 落盘正确",
          sorted(os.listdir(de)) == ["A1.cbz", "B1.cbz", "c.cbz"], os.listdir(de))

    # 链式：A1→c.cbz 的同时 c.cbz→C1.cbz，目标名正好是另一个源名
    ok_c, errs_c = rename.execute_renames(de, [("A1.cbz", "c.cbz"), ("c.cbz", "C1.cbz")])
    check("链式改名不互相覆盖",
          (ok_c, errs_c) == (2, []) and sorted(os.listdir(de)) == ["B1.cbz", "C1.cbz", "c.cbz"],
          (ok_c, errs_c, os.listdir(de)))

    # 单文件失败：目标被预置文件占用，其余照常完成，失败项回滚原名
    de2 = os.path.join(tmp, "exec2")
    os.makedirs(de2)
    for n in ["p.cbz", "q.cbz", "r.cbz", "taken.cbz"]:
        open(os.path.join(de2, n), "wb").close()
    ok_m, errs_m = rename.execute_renames(
        de2, [("p.cbz", "P1.cbz"), ("q.cbz", "taken.cbz"), ("r.cbz", "R1.cbz")])
    check("单文件失败不影响其余", ok_m == 2 and len(errs_m) == 1, (ok_m, errs_m))
    check("失败项回滚为原名且无 .tmp 残留",
          os.path.exists(os.path.join(de2, "q.cbz"))
          and not any(n.startswith(".__rn_") for n in os.listdir(de2)),
          os.listdir(de2))

    # --- 7c. 换名重试：被占用时有限次后抛错，不会无限等 ---
    lk = os.path.join(tmp, "lock")
    os.makedirs(lk)
    srcf = os.path.join(lk, "a.part")
    dstf = os.path.join(lk, "b.cbz")
    open(srcf, "wb").close()
    open(dstf, "wb").close()
    held = open(dstf, "rb")      # Windows 下持有句柄即可阻塞换名
    try:
        raised = False
        try:
            archive.replace_with_retry(srcf, dstf, attempts=2, base_delay=0.01)
        except PermissionError:
            raised = True
        check("换名被占用时重试后抛错（不死等）", raised or os.name != "nt", "未抛错")
    finally:
        held.close()

    # --- 8. 多进程真实跑一遍（pickle + spawn 导入） ---
    pm = os.path.join(tmp, "pool")
    os.makedirs(pm)
    pdf2 = os.path.join(tmp, "Vol 02.pdf")
    make_pdf(pdf2)
    jobs = [batch.ConvertJob(pdf, pm, False, False),
            batch.ConvertJob(pdf2, pm, False, False)]
    with multiprocessing.Pool(2) as pool:
        res = list(pool.imap_unordered(batch.convert_file, jobs))
    check("多进程 worker 可 pickle 且跑通", all(r[1] for r in res), res)
    check("并行产出两个 CBZ",
          sorted(n for n in os.listdir(pm) if n.endswith(".cbz")) == ["Vol 01.cbz", "Vol 02.cbz"],
          os.listdir(pm))

    # --- 8b. 同名不同目录并发：.part 带 pid 后不再互相撞锁 ---
    ca = os.path.join(tmp, "a")
    cb = os.path.join(tmp, "b")
    shared = os.path.join(tmp, "shared")
    for d in (ca, cb, shared):
        os.makedirs(d)
    shutil.copy(pdf, os.path.join(ca, "Same.pdf"))
    shutil.copy(pdf2, os.path.join(cb, "Same.pdf"))
    cjobs = [batch.ConvertJob(os.path.join(ca, "Same.pdf"), shared, False, True),
             batch.ConvertJob(os.path.join(cb, "Same.pdf"), shared, False, True)]
    with multiprocessing.Pool(2) as pool:
        cres = list(pool.imap_unordered(batch.convert_file, cjobs))
    check("同名输出并发不再双方全败", all(ok for _, ok, _ in cres),
          [m.splitlines()[0] for _, ok, m in cres if not ok])
    check("并发后输出目录只剩一个 CBZ（后者覆盖）",
          sorted(os.listdir(shared)) == ["Same.cbz"], os.listdir(shared))

    # --- 8c. 输出重名预检（UI 在 start() 里用它提前提醒） ---
    col = batch.find_output_collisions(
        [os.path.join(ca, "Same.pdf"), os.path.join(cb, "Same.pdf"), pdf])
    check("输出重名检测出 1 组", len(col) == 1 and col[0][0] == "Same.cbz"
          and len(col[0][1]) == 2, col)
    check("无重名时返回空", batch.find_output_collisions([pdf, pdf2]) == [])

    # --- 9. 清理残留 ---
    for n in [".tmp_x", ".__rn_0_1.tmp", "bad.cbz.part", "Vol 09.cbz.999.part", "keep.cbz"]:
        open(os.path.join(pm, n), "wb").close()
    batch.cleanup_stale_tmp(pm)
    left = sorted(os.listdir(pm))
    check("残留清理只删中间产物",
          left == sorted(["Vol 01.cbz", "Vol 02.cbz", "keep.cbz"]), left)

    # --- 10. MOBI 提取：合成书覆盖分支；真实三卷漫画书另做端到端（见 README） ---
    if importlib.util.find_spec("pymobi") is None:      # 用 find_spec 探测，避免无效 import
        skip("MOBI 用例", "未安装 pymobi（pip install pymobi）")
    else:
        jpg_m = make_pix((12, 180, 60), "jpg")
        png_m = make_pix((20, 20, 200), "png")
        gif_m = gif_1px()
        thumb_m = make_pix((90, 90, 90), "jpg")

        m6 = os.path.join(tmp, "Vol 07.mobi")
        make_mobi(m6, [jpg_m, png_m, gif_m, thumb_m], thumb=3, pad_last=2)
        info6 = mobi.probe_mobi(m6)
        check("MOBI probe 认出版本", mobi.describe(info6) == "MOBI6", mobi.describe(info6))
        check("MOBI probe 丢掉缩略图", info6.images == 3, info6.images)
        check("MOBI probe 编码分布", dict(info6.kinds) == {"jpeg": 1, "png": 1, "gif": 1},
              dict(info6.kinds))
        check("MOBI probe 不算加密", info6.encrypted is False)

        got_m = list(mobi.iter_mobi_images(m6))
        check("MOBI 产出顺序与格式",
              [e for e, _ in got_m] == ["jpeg", "png", "gif"], [e for e, _ in got_m])
        check("MOBI 字节一致（尾部 NUL 已裁）",
              got_m[0][1] == jpg_m and got_m[1][1] == png_m and got_m[2][1] == gif_m)

        d_m = os.path.join(tmp, "mobi_cbz")
        os.makedirs(d_m)
        _pm, ok_m, msg_m = batch.convert_file(batch.ConvertJob(m6, d_m, False, False))
        check("MOBI 转 CBZ 成功", ok_m and "3 张唯一图片" in msg_m, msg_m)
        with zipfile.ZipFile(os.path.join(d_m, "Vol 07.cbz")) as z:
            names_m = z.namelist()
        check("MOBI CBZ 命名连续",
              names_m == ["img_001.jpeg", "img_002.png", "img_003.gif"], names_m)

        d_keepm = os.path.join(tmp, "mobi_keep")
        os.makedirs(d_keepm)
        _pk, ok_k, msg_k = batch.convert_file(batch.ConvertJob(m6, d_keepm, True, False))
        check("MOBI 保留中间图片",
              ok_k and os.path.isdir(os.path.join(d_keepm, "Vol 07_images")), msg_k)

        mbogus = os.path.join(tmp, "bogus.mobi")
        make_mobi(mbogus, [jpg_m, b"BM\xff\xff\xff\xff" + b"x" * 40])
        ib = mobi.probe_mobi(mbogus)
        nb = len(list(mobi.iter_mobi_images(mbogus)))
        check("BM 假魔数不当成图（probe 与产出一致）", ib.images == nb == 1, (ib.images, nb))

        mnoauthor = os.path.join(tmp, "noauthor.mobi")
        make_mobi(mnoauthor, [jpg_m], author=False)
        check("MOBI 缺作者字段也能打开（pymobi py3 缺陷有兜底）",
              mobi.probe_mobi(mnoauthor).images == 1,
              "打开失败")

        mdup = os.path.join(tmp, "dup.mobi")
        make_mobi(mdup, [jpg_m, jpg_m, png_m])
        check("MOBI 内容级去重", len(list(mobi.iter_mobi_images(mdup))) == 2)

        mdrm = os.path.join(tmp, "drm.mobi")
        make_mobi(mdrm, [jpg_m], drm=1)
        check("DRM 书 probe 标出加密", mobi.probe_mobi(mdrm).encrypted is True)
        raised = ""
        try:
            list(mobi.iter_mobi_images(mdrm))
        except mobi.DrmError as e:
            raised = str(e)
        check("DRM 书提取被拒", "DRM" in raised, raised or "没有抛异常")
        d_drm = os.path.join(tmp, "drm_out")
        os.makedirs(d_drm)
        _pd, ok_d, msg_d = batch.convert_file(batch.ConvertJob(mdrm, d_drm, False, False))
        check("DRM 书转换返回失败", not ok_d and "DRM" in msg_d, msg_d)

        mnone = os.path.join(tmp, "none.mobi")
        make_mobi(mnone, [])
        check("无图书 probe 张数为 0", mobi.probe_mobi(mnone).images == 0)
        d_none = os.path.join(tmp, "none_out")
        os.makedirs(d_none)
        _pn, ok_n, msg_n = batch.convert_file(batch.ConvertJob(mnone, d_none, False, False))
        check("无图书报「未提取到任何图片」",
              not ok_n and "未提取到任何图片" in msg_n, msg_n)

        mjoint = os.path.join(tmp, "joint.azw3")
        make_mobi(mjoint, [jpg_m, png_m, gif_m], joint=True)
        check("合体书格式标签",
              mobi.describe(mobi.probe_mobi(mjoint)) == "MOBI6+KF8 合体",
              mobi.describe(mobi.probe_mobi(mjoint)))
        check(".azw3 走 MOBI 路径且不分段",
              mobi.is_mobi(mjoint) and not probe.can_split(mjoint))

        d_mix = os.path.join(tmp, "mix")
        os.makedirs(d_mix)
        shutil.copy(m6, os.path.join(d_mix, "a.mobi"))
        shutil.copy(pdf, os.path.join(d_mix, "b.pdf"))
        open(os.path.join(d_mix, "c.txt"), "wb").close()
        check("目录扫描吃到 PDF + MOBI",
              [os.path.basename(x) for x in batch.scan_inputs(d_mix)] == ["a.mobi", "b.pdf"],
              os.listdir(d_mix))
        check("summary 走 MOBI 分支", "MOBI6" in probe.summary(m6), probe.summary(m6))

    # --- 11. EPUB 提取：spine 顺序、补漏、DRM、坏文件 ---
    jpg_e = make_pix((180, 40, 40), "jpg")
    png_e = make_pix((40, 40, 180), "png")
    cover_e = make_pix((40, 180, 40), "jpg")
    e1 = os.path.join(tmp, "Vol 11.epub")
    make_epub(e1, [jpg_e, png_e], extra=[cover_e])
    ie = epub.probe_epub(e1)
    got_e = list(epub.iter_epub_images(e1))
    check("EPUB probe 张数（2 页 + 1 张未引用）", ie.images == 3, ie.images)
    check("EPUB 输出顺序 = spine 页序 + 补漏",
          [d for _, d in got_e] == [jpg_e, png_e, cover_e],
          [len(d) for _, d in got_e])
    check("EPUB 字节与源一致", got_e[0][1] == jpg_e and got_e[2][1] == cover_e)
    check("EPUB 扩展名归一（jpg→jpeg）", [x for x, _ in got_e] == ["jpeg", "png", "jpeg"],
          [x for x, _ in got_e])

    d_e = os.path.join(tmp, "epub_cbz")
    os.makedirs(d_e)
    _pe, ok_e, msg_e = batch.convert_file(batch.ConvertJob(e1, d_e, False, False))
    check("EPUB 转 CBZ 成功", ok_e and "3 张唯一图片" in msg_e, msg_e)
    with zipfile.ZipFile(os.path.join(d_e, "Vol 11.cbz")) as z:
        names_e = z.namelist()
    check("EPUB CBZ 命名连续",
          names_e == ["img_001.jpeg", "img_002.png", "img_003.jpeg"], names_e)
    check("EPUB summary 走 EPUB 分支", "EPUB 3.0" in probe.summary(e1), probe.summary(e1))
    check("EPUB 不切段", probe.can_split(e1) is False)

    d_mix2 = os.path.join(tmp, "mix_epub")
    os.makedirs(d_mix2)
    shutil.copy(e1, os.path.join(d_mix2, "x.epub"))
    check("目录扫描吃到 EPUB",
          [os.path.basename(x) for x in batch.scan_inputs(d_mix2)] == ["x.epub"])

    # --- 11b. EPUB 边界：去重、页序、非严格 XHTML、DRM 判定 ---
    def page_xhtml(href):
        return ("<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'>"
                f"<body><img src='{href}'/></body></html>")

    def build_edge(name, pages, images, encrypt=None, container=None, extra=None):
        """pages 是 XHTML 文本列表；images 是 {文件名: 字节}。"""
        items = "".join(f'<item id="p{i}" href="text/p{i}.xhtml" '
                        f'media-type="application/xhtml+xml"/>'
                        for i in range(1, len(pages) + 1))
        items += "".join(f'<item id="i{j}" href="image/{n}" media-type="image/jpeg"/>'
                         for j, n in enumerate(images, 1))
        entries = {
            "META-INF/container.xml": container or epub_container(),
            "OEBPS/content.opf": epub_pkg(
                items, "".join(f'<itemref idref="p{i}"/>' for i in range(1, len(pages) + 1))),
        }
        for i, content in enumerate(pages, 1):
            entries[f"OEBPS/text/p{i}.xhtml"] = content
        for n, data in images.items():
            entries[f"OEBPS/image/{n}"] = data
        if encrypt:
            attr, uri = encrypt
            entries["META-INF/encryption.xml"] = (
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<EncryptedData><CipherData>'
                f'<CipherReference {attr}="{uri}"/></CipherData></EncryptedData></encryption>')
        entries.update(extra or {})
        path = os.path.join(tmp, name)
        make_epub_raw(path, entries)
        return path

    ja, jb = b"\xff\xd8\xff" + b"A" * 40, b"\xff\xd8\xff" + b"B" * 40

    e_dup = build_edge("dupimg.epub", [page_xhtml("../image/a.jpg"),
                                       page_xhtml("../image/a.jpg")],
                       {"a.jpg": ja})
    check("EPUB 同一张图被两页引用只取一次",
          len(list(epub.iter_epub_images(e_dup))) == 1,
          len(list(epub.iter_epub_images(e_dup))))

    e_q = build_edge("query.epub", [page_xhtml("../image/a.jpg?v=1"),
                                    page_xhtml("../image/b.jpg")],
                     {"a.jpg": ja, "b.jpg": jb})
    check("EPUB href 带 ?查询串 不乱序",
          [d for _, d in epub.iter_epub_images(e_q)] == [ja, jb])

    e_a = build_edge("anchor.epub", [page_xhtml("../image/a.jpg#top"),
                                     page_xhtml("../image/b.jpg")],
                     {"a.jpg": ja, "b.jpg": jb})
    check("EPUB href 带 #锚点 不乱序",
          [d for _, d in epub.iter_epub_images(e_a)] == [ja, jb])

    e_sloppy = build_edge("sloppy.epub",
                          ['<html><body><img src="../image/a.jpg">'], {"a.jpg": ja})
    check("EPUB XHTML 非严格 XML 时回退正则",
          len(list(epub.iter_epub_images(e_sloppy))) == 1)

    e_font = build_edge("fontdrm.epub", [page_xhtml("../image/a.jpg")], {"a.jpg": ja},
                        encrypt=("URI", "OEBPS/fonts/f.otf"))
    check("EPUB 字体混淆不算 DRM（不误拒）",
          len(list(epub.iter_epub_images(e_font))) == 1)

    e_case = build_edge("casedrm.epub", [page_xhtml("../image/a.jpg")], {"a.jpg": ja},
                        encrypt=("uri", "OEBPS/IMAGE/A.JPG"))
    case_raised = ""
    try:
        epub.probe_epub(e_case)
    except epub.EpubDrmError as e:
        case_raised = str(e)
    check("EPUB 加密 URI 大小写不一致也拒", "加密" in case_raised, case_raised or "漏判")

    decoy = epub_pkg("", "")
    e_path = build_edge("backslash.epub", [page_xhtml("../image/a.jpg")], {"a.jpg": ja},
                        container=epub_container("OEBPS\\content.opf"),
                        extra={"OEBPS/samples/template.opf": decoy})
    check("EPUB container 反斜杠 full-path 仍能找到正确 OPF",
          len(list(epub.iter_epub_images(e_path))) == 1,
          epub.probe_epub(e_path).images)

    enone = os.path.join(tmp, "textonly.epub")
    make_epub(enone, [])
    check("纯文字 EPUB 张数为 0", epub.probe_epub(enone).images == 0)
    d_en = os.path.join(tmp, "epub_none")
    os.makedirs(d_en)
    _pz, ok_n2, msg_n2 = batch.convert_file(batch.ConvertJob(enone, d_en, False, False))
    check("纯文字 EPUB 报「未提取到任何图片」",
          not ok_n2 and "未提取到任何图片" in msg_n2, msg_n2)

    edrm = os.path.join(tmp, "drm.epub")
    make_epub(edrm, [jpg_e], drm=True)
    raised_e = ""
    try:
        epub.probe_epub(edrm)
    except epub.EpubDrmError as e:
        raised_e = str(e)
    check("EPUB 加密资源被拒", "加密" in raised_e, raised_e or "没有抛异常")
    d_ed = os.path.join(tmp, "epub_drm")
    os.makedirs(d_ed)
    _pdr, ok_dr, msg_dr = batch.convert_file(batch.ConvertJob(edrm, d_ed, False, False))
    check("EPUB 加密书转换失败且说明原因", not ok_dr and "加密" in msg_dr, msg_dr)

    ebad = os.path.join(tmp, "bad.epub")
    with open(ebad, "wb") as f:
        f.write(b"not a zip at all")
    raised_b = ""
    try:
        epub.probe_epub(ebad)
    except epub.EpubError as e:
        raised_b = str(e)
    check("坏 EPUB 报明确错误", "不是有效的 EPUB" in raised_b, raised_b or "没有抛异常")

    shutil.rmtree(tmp, ignore_errors=True)
    tail = "，%d 项跳过" % tally["skip"] if tally["skip"] else ""
    print("\n==== 结果：%d 项通过，%d 项失败%s ====" % (tally["pass"], tally["fail"], tail))
    if fails:
        print("失败项：", fails)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run())
